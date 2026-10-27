#!/usr/bin/env bash
# Update a running deployment to the latest code, with a backup first and automatic rollback.
#
#   ./scripts/update.sh                         # git pull, back up, build, restart, wait until ready
#   ./scripts/update.sh --no-pull               # deploy the code already checked out
#   ./scripts/update.sh --no-backup             # skip the backup (rollback then uses alembic downgrade)
#   ./scripts/update.sh --images TAG            # deploy existing images TAG instead of building
#   ./scripts/update.sh --rollback              # go back to the version that ran before the last update
#   ./scripts/update.sh --rollback --restore-db # ...and load the pre-update backup instead of downgrading
#
# Before building, the running images are tagged rollback-<time>, the database revision is
# recorded and a backup is taken. Migrations run when the new API container starts. If it does
# not become ready, the API is stopped, the pre-update backup is restored when the schema changed
# (alembic downgrade when there is no backup), and the previous images are started again.
# --rollback downgrades the schema with the newer image; --restore-db loses data written since.
# ARGUS_READY_TIMEOUT (default 240) is how many seconds the new version gets to become ready.

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

pull=1 do_backup=1 rollback=0 restore_mode=downgrade build_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull) pull=0; shift ;;
    --no-backup) do_backup=0; shift ;;
    --rollback) rollback=1; shift ;;
    --restore-db) restore_mode=restore; shift ;;
    --ca) build_args+=(--ca "${2:?--ca needs a file}"); shift 2 ;;
    --images) build_args+=(--from-images "${2:?--images needs a tag}"); shift 2 ;;
    -h | --help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
[ "$restore_mode" = downgrade ] || [ "$rollback" = 1 ] || die "--restore-db only applies to --rollback"
require_docker
require_env
lock_deploy
current="$(current_version)"

# start VERSION SECONDS: start VERSION; fails if Compose has not finished within SECONDS.
start() {
  log "starting version $1"
  ARGUS_VERSION="$1" dc_timeout "$2" up -d --no-build --remove-orphans
}

# revert_schema NEWER REVISION DUMP MODE: bring the database back to REVISION before an older
# image starts. Its entrypoint runs `alembic upgrade head`, which fails on a revision it does not
# know, and the API would crash-loop. MODE restore loads DUMP (the exact pre-update state);
# MODE downgrade, or a missing/unusable DUMP, runs `alembic downgrade REVISION` with the image
# NEWER, which knows both revisions. Does nothing when the database is already at REVISION, so it
# is safe to repeat.
revert_schema() {
  local newer="$1" rev="$2" dump="$3" mode="$4" now
  if [ -z "$rev" ]; then
    log "no database revision recorded for this rollback: leaving the schema as it is"
    return 0
  fi
  ensure_db || return 1
  now="$(db_revision)" || { warn "cannot read the database revision"; return 1; }
  if [ "$now" = "$rev" ]; then
    log "database schema already at revision $rev"
    return 0
  fi
  log "database schema is at revision $now; the rollback target needs $rev"
  if [ "$mode" = restore ]; then
    if [ -n "$dump" ] && verify_dump "$dump" && restore_db "$dump"; then
      now="$(db_revision)" || return 1
      [ "$now" = "$rev" ] && return 0
      warn "after the restore the database is at revision $now, not $rev"
      return 1
    fi
    warn "could not restore the pre-update backup ${dump:-(none taken)}; trying alembic downgrade"
  fi
  case "$rev" in
    *,*) warn "the database had several Alembic heads ($rev): cannot downgrade automatically"; return 1 ;;
  esac
  log "downgrading the schema to $rev with version $newer"
  ARGUS_VERSION="$newer" dc run --rm --no-deps -T -e ARGUS_RUN_MIGRATIONS=false api \
    alembic downgrade "$rev" || return 1
  now="$(db_revision)" || return 1
  [ "$now" = "$rev" ] || { warn "after the downgrade the database is at revision $now, not $rev"; return 1; }
}

# rollback_to TARGET NEWER REVISION DUMP MODE: stop the API (so a crash-looping container cannot
# migrate again), put the schema back (see revert_schema) and start TARGET.
rollback_to() {
  local target="$1"
  log "rolling back to $target"
  dc stop api web || true
  revert_schema "$2" "$3" "$4" "$5" || return 1
  start "$target" 180 && wait_healthy 180
}

if [ "$rollback" = 1 ]; then
  if [ -f "$STATE_FILE" ]; then
    target="$(stateval version)" newer="$(stateval from)" rev="$(stateval db_revision)" dump="$(stateval db_dump)"
  elif [ -f "$PREV_FILE" ]; then
    target="$(cat "$PREV_FILE")" newer="" rev="" dump=""
    warn "no rollback state recorded (update made by an older script): the schema is not reverted"
  else
    die "no previous version recorded"
  fi
  [ -n "$target" ] || die "the rollback state names no version"
  if [ "$restore_mode" = restore ] && [ -z "$dump" ]; then
    die "no pre-update backup recorded for this rollback; run without --restore-db to downgrade"
  fi
  forward="$(snapshot_images "$current")"
  rollback_to "$target" "${newer:-$current}" "$rev" "$dump" "$restore_mode" \
    || die "rollback to $target failed; see the logs above (backups: ./scripts/restore.sh --list)"
  # Swap, so another --rollback returns to the version just left. Rolling forward needs no schema
  # change: the newer image migrates the database itself when it starts.
  write_state "version=$forward" "from=$target" "db_revision=" "db_dump="
  echo "$forward" >"$PREV_FILE"
  echo "$target" >"$VERSION_FILE"
  log "rolled back to $target"
  exit 0
fi

if [ "$pull" = 1 ]; then
  git -C "$ROOT" diff --quiet HEAD -- || die "the checkout has local changes; commit or stash them, or use --no-pull"
  log "pulling the latest code"
  git -C "$ROOT" pull --ff-only
fi

pre_rev=""
if db_running; then
  pre_rev="$(db_revision)" || die "cannot read the database revision"
  log "database schema at revision $pre_rev"
fi
dump=""
if [ "$do_backup" = 1 ]; then
  backup || die "the backup failed: not updating (use --no-backup to update without one)"
  dump="$LAST_BACKUP"
else
  warn "--no-backup: if this update fails after migrating, the schema is reverted with alembic downgrade"
fi

# Pin the running images to a tag the build cannot overwrite (the build may reuse $current).
prev="$(snapshot_images "$current")"

"$ROOT/scripts/build.sh" ${build_args[@]+"${build_args[@]}"}
next="$(cat "$ROOT/deploy/.built-version")"

# Record the rollback before starting the new version, so --rollback works even if this script
# is interrupted from here on. Keep the old record in case this update is rolled back.
if [ -f "$STATE_FILE" ]; then cp -f "$STATE_FILE" "$STATE_FILE.before-update"; fi
write_state "version=$prev" "from=$next" "db_revision=$pre_rev" "db_dump=$dump"

ready_timeout="${ARGUS_READY_TIMEOUT:-240}"
if start "$next" "$ready_timeout" && wait_healthy "$ready_timeout"; then
  rm -f "$STATE_FILE.before-update"
  echo "$prev" >"$PREV_FILE"
  echo "$next" >"$VERSION_FILE"
  prune_snapshots
  log "updated $current -> $next at $(web_url)"
  exit 0
fi

warn "version $next is not healthy: rolling back to $current ($prev)"
if rollback_to "$prev" "$next" "$pre_rev" "$dump" restore; then
  echo "$prev" >"$VERSION_FILE"
  if [ -f "$STATE_FILE.before-update" ]; then
    # The earlier record again, but its "from" tag may have been overwritten by this build:
    # name the snapshot of the images that run now instead.
    mv -f "$STATE_FILE.before-update" "$STATE_FILE"
    write_state "version=$(stateval version)" "from=$prev" \
      "db_revision=$(stateval db_revision)" "db_dump=$(stateval db_dump)"
  else
    rm -f "$STATE_FILE"
  fi
  die "update failed and was rolled back to $current; see the logs above"
fi
die "rollback to $current is not healthy either; retry with ./scripts/update.sh --rollback --restore-db (pre-update backup: ${dump:-none})"

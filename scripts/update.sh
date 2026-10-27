#!/usr/bin/env bash
# Update a running deployment to the latest code, with a backup first and automatic rollback.
#
#   ./scripts/update.sh               # git pull, back up, build, restart, wait until ready
#   ./scripts/update.sh --no-pull     # deploy the code already checked out
#   ./scripts/update.sh --no-backup   # skip the database/artifact backup
#   ./scripts/update.sh --rollback    # go back to the version that ran before the last update
#
# Migrations run when the new API container starts. If it does not become ready, the previous
# images are started again. A schema migration is not undone automatically: restore the backup
# printed above (pg_restore) if the old version cannot read the new schema.

source "$(dirname "$0")/lib.sh"

pull=1 do_backup=1 rollback=0 build_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull) pull=0; shift ;;
    --no-backup) do_backup=0; shift ;;
    --rollback) rollback=1; shift ;;
    --ca) build_args+=(--ca "${2:?--ca needs a file}"); shift 2 ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
require_docker
require_env
PREV_FILE="$ROOT/deploy/.previous-version"
current="$(current_version)"

start() {
  log "starting version $1"
  ARGUS_VERSION="$1" dc up -d --no-build --remove-orphans
}

if [ "$rollback" = 1 ]; then
  [ -f "$PREV_FILE" ] || die "no previous version recorded"
  target="$(cat "$PREV_FILE")"
  start "$target"
  wait_healthy 180 || die "version $target is not healthy either"
  echo "$current" >"$PREV_FILE"
  echo "$target" >"$VERSION_FILE"
  log "rolled back to $target"
  exit 0
fi

if [ "$pull" = 1 ]; then
  git -C "$ROOT" diff --quiet HEAD -- || die "the checkout has local changes; commit or stash them, or use --no-pull"
  log "pulling the latest code"
  git -C "$ROOT" pull --ff-only
fi

[ "$do_backup" = 1 ] && backup

"$ROOT/scripts/build.sh" ${build_args[@]+"${build_args[@]}"}
next="$(cat "$ROOT/deploy/.built-version")"

start "$next"
if wait_healthy 240; then
  echo "$current" >"$PREV_FILE"
  echo "$next" >"$VERSION_FILE"
  log "updated $current -> $next at $(web_url)"
else
  warn "version $next is not healthy: rolling back to $current"
  start "$current"
  wait_healthy 180 || die "rollback to $current is not healthy either; restore the latest backup in $BACKUP_DIR"
  die "update failed and was rolled back to $current; see the logs above"
fi

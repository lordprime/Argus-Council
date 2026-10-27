#!/usr/bin/env bash
# Restore a backup made by backup.sh or update.sh into the running deployment.
#
#   ./scripts/restore.sh --list              # list the backups in $ARGUS_BACKUP_DIR (default ./backups)
#   ./scripts/restore.sh                     # restore the newest backup (asks for confirmation)
#   ./scripts/restore.sh 20261027T120000Z    # restore the backup with this timestamp
#   ./scripts/restore.sh path/to/argus-db-<ts>.dump
#   ./scripts/restore.sh --db-only ...       # leave the artifact volume as it is
#   ./scripts/restore.sh --yes ...           # do not ask for confirmation (for automation)
#
# Each file is checked against its .sha256 and read to the end first; a truncated or changed
# backup is refused. The api and web containers are stopped while the database is replaced (the
# public schema is dropped and the dump loaded in one transaction) and started again afterwards,
# also when the restore fails. The artifact tarball, when present, replaces the argus_argusdata volume.

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

list=0 yes=0 db_only=0 target=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list | -l) list=1; shift ;;
    --yes | -y) yes=1; shift ;;
    --db-only) db_only=1; shift ;;
    -h | --help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) [ -z "$target" ] || die "only one backup can be restored at a time"; target="$1"; shift ;;
  esac
done

human_size() { du -h "$1" 2>/dev/null | cut -f1; }
sum_state() {
  local rc=0
  check_sha256 "$1" || rc=$?
  case "$rc" in 0) printf 'ok' ;; 2) printf 'none' ;; *) printf 'BAD' ;; esac
}

if [ "$list" = 1 ]; then
  stamps="$(backup_stamps)"
  [ -n "$stamps" ] || { log "no backups in $BACKUP_DIR"; exit 0; }
  printf '%-20s  %8s  %8s  %s\n' TIMESTAMP DATABASE ARTIFACTS CHECKSUMS
  while IFS= read -r ts; do
    db="$BACKUP_DIR/argus-db-$ts.dump" tgz="$BACKUP_DIR/argus-data-$ts.tgz" data="-" sums="db:$(sum_state "$db")"
    if [ -f "$tgz" ]; then
      data="$(human_size "$tgz")" sums="$sums data:$(sum_state "$tgz")"
    fi
    printf '%-20s  %8s  %8s  %s\n' "$ts" "$(human_size "$db")" "$data" "$sums"
  done <<<"$stamps"
  exit 0
fi

# Resolve the dump (and its artifact tarball) to restore.
if [ -z "$target" ] || [ "$target" = latest ]; then
  target="$(backup_stamps | head -n1)"
  [ -n "$target" ] || die "no backups in $BACKUP_DIR (see --list)"
fi
if [ -f "$target" ]; then
  dump="$(cd "$(dirname "$target")" && pwd)/$(basename "$target")"
  ts="$(basename "$dump" .dump)"
  ts="${ts#argus-db-}"
  tarball="$(dirname "$dump")/argus-data-$ts.tgz"
else
  ts="$target"
  dump="$BACKUP_DIR/argus-db-$ts.dump"
  tarball="$BACKUP_DIR/argus-data-$ts.tgz"
fi
[ -s "$dump" ] || die "no database dump at $dump (see --list)"
verify_checksum "$dump" || die "refusing to restore $dump"
restore_data=0
if [ "$db_only" = 0 ]; then
  if [ -e "$tarball" ]; then
    verify_tarball "$tarball" || die "refusing to restore $tarball (use --db-only to restore only the database)"
    restore_data=1
  else
    warn "no artifact tarball for $ts: only the database will be restored"
  fi
fi

require_docker
require_env
lock_deploy

if [ "$yes" = 0 ]; then
  [ -t 0 ] || die "refusing to restore without confirmation: run interactively or pass --yes"
  printf 'This REPLACES the current database%s with backup %s.\n' \
    "$([ "$restore_data" = 1 ] && printf ' and all stored artifacts')" "$ts"
  printf 'Everything written since that backup is lost. Type "restore" to continue: '
  read -r answer
  [ "$answer" = restore ] || die "aborted"
fi

log "making sure the database is running"
ensure_db || die "postgres did not become ready"
verify_dump_archive "$dump" || die "refusing to restore $dump"

log "stopping api and web"
dc stop api web
restart() {
  log "starting api and web"
  ARGUS_VERSION="$(current_version)" dc up -d --no-build api web
}
add_exit_hook restart

restore_db "$dump" || die "the database restore failed; the database was left unchanged (single transaction)"
log "database at revision $(db_revision || echo unknown)"

if [ "$restore_data" = 1 ]; then
  log "restoring artifacts from $tarball into argus_argusdata"
  docker run --rm -i --network none --entrypoint sh -v argus_argusdata:/data \
    "argus-council/api:$(current_version)" -c 'find /data -mindepth 1 -delete && tar xzf - -C /data' \
    <"$tarball" || die "artifact restore failed; the database was restored"
fi

remove_exit_hook restart
restart
wait_healthy 180 || die "restored, but the stack is not healthy (see the logs above)"
log "restored backup $ts"

#!/usr/bin/env bash
# Restore a backup made by update.sh / backup() into the running deployment.
#
#   ./scripts/restore.sh --list              # list the backups in $ARGUS_BACKUP_DIR (default ./backups)
#   ./scripts/restore.sh                     # restore the newest backup (asks for confirmation)
#   ./scripts/restore.sh 20261027T120000Z    # restore the backup with this timestamp
#   ./scripts/restore.sh path/to/argus-db-<ts>.dump
#   ./scripts/restore.sh --db-only ...       # leave the artifact volume as it is
#   ./scripts/restore.sh --yes ...           # do not ask for confirmation (for automation)
#
# The api and web containers are stopped while the database is replaced (pg_restore --clean
# --if-exists, in a single transaction) and started again afterwards, also when the restore fails.
# The artifact tarball, when present, replaces the contents of the argus_argusdata volume.

source "$(dirname "$0")/lib.sh"

list=0 yes=0 db_only=0 target=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list | -l) list=1; shift ;;
    --yes | -y) yes=1; shift ;;
    --db-only) db_only=1; shift ;;
    -h | --help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) [ -z "$target" ] || die "only one backup can be restored at a time"; target="$1"; shift ;;
  esac
done

human_size() { du -h "$1" 2>/dev/null | cut -f1; }

if [ "$list" = 1 ]; then
  stamps="$(backup_stamps)"
  [ -n "$stamps" ] || { log "no backups in $BACKUP_DIR"; exit 0; }
  printf '%-18s  %8s  %8s\n' TIMESTAMP DATABASE ARTIFACTS
  while IFS= read -r ts; do
    data="-"
    [ -f "$BACKUP_DIR/argus-data-$ts.tgz" ] && data="$(human_size "$BACKUP_DIR/argus-data-$ts.tgz")"
    printf '%-18s  %8s  %8s\n' "$ts" "$(human_size "$BACKUP_DIR/argus-db-$ts.dump")" "$data"
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
restore_data=0
if [ "$db_only" = 0 ]; then
  if [ -s "$tarball" ]; then
    restore_data=1
  else
    warn "no artifact tarball for $ts: only the database will be restored"
  fi
fi

require_docker
require_env

if [ "$yes" = 0 ]; then
  [ -t 0 ] || die "refusing to restore without confirmation: run interactively or pass --yes"
  printf 'This REPLACES the current database%s with backup %s.\n' \
    "$([ "$restore_data" = 1 ] && printf ' and all stored artifacts')" "$ts"
  printf 'Everything written since that backup is lost. Type "restore" to continue: '
  read -r answer
  [ "$answer" = restore ] || die "aborted"
fi

log "making sure the database is running"
dc up -d postgres
for _ in $(seq 1 60); do
  dc exec -T postgres pg_isready -U argus -d argus >/dev/null 2>&1 && break
  sleep 1
done
dc exec -T postgres pg_isready -U argus -d argus >/dev/null 2>&1 || die "postgres did not become ready"

log "stopping api and web"
dc stop api web
restart() {
  log "starting api and web"
  ARGUS_VERSION="$(current_version)" dc up -d --no-build api web
}
trap restart EXIT

log "restoring the database from $dump"
dc exec -T postgres pg_restore -U argus -d argus --clean --if-exists --no-owner --single-transaction \
  <"$dump" || die "pg_restore failed; the database was left unchanged (single transaction)"

if [ "$restore_data" = 1 ]; then
  log "restoring artifacts from $tarball into argus_argusdata"
  docker run --rm -i --network none --entrypoint sh -v argus_argusdata:/data \
    "argus-council/api:$(current_version)" -c 'find /data -mindepth 1 -delete && tar xzf - -C /data' \
    <"$tarball" || die "artifact restore failed; the database was restored"
fi

trap - EXIT
restart
wait_healthy 180 || die "restored, but the stack is not healthy (see the logs above)"
log "restored backup $ts"

#!/usr/bin/env bash
# Back up the running deployment: the artifact volume and a Postgres dump, into $ARGUS_BACKUP_DIR
# (default ./backups), each with a .sha256 file. Safe to run from cron.
#
#   ./scripts/backup.sh          # prints the path of the new database dump on the last line
#
# Files are written as .partial and renamed only after they were read back completely, so an
# interrupted backup never looks valid. Keeps the newest ARGUS_BACKUP_KEEP (default 14) backups.
# Restore with ./scripts/restore.sh.

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

case "${1:-}" in
  -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  '') ;;
  *) die "unknown option: $1 (see --help)" ;;
esac
require_docker
require_env
lock_deploy
db_running || die "the database is not running: nothing to back up"
backup || die "backup failed"
printf '%s\n' "$LAST_BACKUP"

#!/usr/bin/env bash
# End-to-end check of backup.sh, update.sh and restore.sh against the real Compose stack (CI).
#
#   ./scripts/tests/e2e.sh
#
# Expects the stack from deploy/docker-compose.yml to be running on the images
# argus-council/{api,web}:ci, as the CI docker job starts it. Steps:
#   1. backup.sh, and the checksums verify;
#   2. update.sh to the same images (no build), smoke test;
#   3. update.sh to an API image that adds a migration and then fails to start: the update must
#      fail, restore the pre-update dump (schema back at the old head, probe table gone) and run
#      the previous images again;
#   4. restore.sh of the backup from step 1: accounts created since are gone, smoke test.
# Leaves the stack running on the original images.
set -euo pipefail

cd "$(dirname "$0")/../.." || exit 1
export ARGUS_READY_TIMEOUT="${ARGUS_READY_TIMEOUT:-150}"
# Step 4 counts the accounts the smoke tests create, so they must stay.
export SMOKE_KEEP_ACCOUNT=1
URL="${E2E_URL:-http://127.0.0.1:8080}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

dc() { docker compose -f deploy/docker-compose.yml --env-file deploy/.env "$@"; }
sql() { dc exec -T postgres psql -X -q -U argus -d argus -tAc "$1"; }
step() { printf '\n\033[1m### %s\033[0m\n' "$*"; }
fail() { printf 'e2e: FAIL: %s\n' "$*" >&2; exit 1; }

echo ci >deploy/.version

step "1. backup"
dump="$(./scripts/backup.sh | tail -n1)"
[ -s "$dump" ] || fail "backup.sh did not print a dump path"
tgz="${dump/argus-db-/argus-data-}"
tgz="${tgz%.dump}.tgz"
(cd "$(dirname "$dump")" && sha256sum -c "${dump##*/}.sha256" "${tgz##*/}.sha256") || fail "checksums"
head="$(sql 'SELECT version_num FROM alembic_version')"
users_at_backup="$(sql 'SELECT count(*) FROM users')"
echo "schema head $head, $users_at_backup users"

step "2. update to the same images"
./scripts/update.sh --no-pull --images ci
./scripts/smoke.sh "$URL"

step "3. failed update with a migration, then automatic rollback"
cat >"$WORK/zz_ci_rollback_probe.py" <<PY
"""CI only: a migration in a release that then fails to start."""

import sqlalchemy as sa
from alembic import op

revision = "ci_rollback_probe"
down_revision = "$head"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("ci_rollback_probe", sa.Column("id", sa.Integer, primary_key=True))


def downgrade() -> None:
    op.drop_table("ci_rollback_probe")
PY
cat >"$WORK/Dockerfile" <<'DOCKERFILE'
FROM argus-council/api:ci
COPY zz_ci_rollback_probe.py /app/alembic/versions/
CMD ["python", "-c", "raise SystemExit('ci: this release is broken on purpose')"]
DOCKERFILE
docker build -q -t argus-council/api:ci-broken "$WORK"
docker tag argus-council/web:ci argus-council/web:ci-broken

rc=0
./scripts/update.sh --no-pull --images ci-broken 2>&1 | tee "$WORK/update.log" || rc=$?
[ "$rc" = 1 ] || fail "the broken update should exit 1, got $rc"
grep -q "update failed and was rolled back" "$WORK/update.log" || fail "no rollback reported"
grep -q "restoring the database from" "$WORK/update.log" || fail "the pre-update dump was not restored"
[ "$(sql 'SELECT version_num FROM alembic_version')" = "$head" ] || fail "schema is not back at $head"
[ "$(sql "SELECT to_regclass('public.ci_rollback_probe') IS NULL")" = t ] || fail "probe table survived"
api_ref="$(docker inspect --format '{{.Config.Image}}' "$(dc ps -q api)")"
echo "the API runs $api_ref"
[ "$(docker image inspect --format '{{.Id}}' "$api_ref")" = "$(docker image inspect --format '{{.Id}}' argus-council/api:ci)" ] \
  || fail "the API does not run the previous image"
./scripts/smoke.sh "$URL"

step "4. restore the backup from step 1"
[ "$(sql 'SELECT count(*) FROM users')" -gt "$users_at_backup" ] || fail "smoke tests created no accounts"
./scripts/restore.sh --yes "$dump"
[ "$(sql 'SELECT count(*) FROM users')" = "$users_at_backup" ] || fail "accounts created after the backup survived"
[ "$(sql 'SELECT version_num FROM alembic_version')" = "$head" ] || fail "schema is not at $head after restore"
./scripts/smoke.sh "$URL"

printf '\ne2e: OK\n'

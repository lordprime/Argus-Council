#!/usr/bin/env bash
# End-to-end smoke test against a running Argus API.
#
# Registers a throw-away account with a random password, uses it, then deletes it again
# (set SMOKE_KEEP_ACCOUNT=1 to keep it, as the backup/restore test does). On a fresh install
# the first account to register becomes the admin: run the smoke test only after creating your
# own admin (or set ARGUS_BOOTSTRAP_ADMIN_*), otherwise the smoke account takes that role.
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
# Unique per run: CI runs this twice within the same second.
email="smoke-$(date +%s)-$$-$RANDOM@example.com"
# Random per run: a fixed, published password would leave a known login behind.
pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

curl -fsS "$BASE/health/ready" | grep -q '"status":"ok"'
curl -fsS -X POST "$BASE/api/v1/auth/register" -H 'content-type: application/json' \
  -d "{\"email\":\"$email\",\"password\":\"$pw\"}" >/dev/null
token=$(curl -fsS -X POST "$BASE/api/v1/auth/login" -H 'content-type: application/json' \
  -d "{\"email\":\"$email\",\"password\":\"$pw\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -fsS -X POST "$BASE/api/v1/councils" -H "authorization: Bearer $token" -H 'content-type: application/json' \
  -d '{"name":"smoke","mode":"research","size":4}' | grep -q '"chair"'

if [ "${SMOKE_KEEP_ACCOUNT:-0}" != 1 ]; then
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$BASE/api/v1/me" -H "authorization: Bearer $token" \
    -H 'content-type: application/json' -d "{\"password\":\"$pw\"}")
  if [ "$code" = 409 ]; then
    echo "smoke: WARNING: $email is the only admin, so it was kept (its password is random and unknown)." >&2
    echo "smoke: set ARGUS_BOOTSTRAP_ADMIN_EMAIL/PASSWORD, restart the API, then remove that account." >&2
  elif [ "$code" != 204 ]; then
    echo "smoke: could not delete the smoke account $email (HTTP $code)" >&2
    exit 1
  fi
fi
echo "smoke: OK"

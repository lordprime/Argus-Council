#!/usr/bin/env bash
# End-to-end smoke test against a running Argus API.
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
email="smoke-$(date +%s)@example.com"
pw="smoke-test-password-123"

curl -fsS "$BASE/health/ready" | grep -q '"status":"ok"'
curl -fsS -X POST "$BASE/api/v1/auth/register" -H 'content-type: application/json' \
  -d "{\"email\":\"$email\",\"password\":\"$pw\"}" >/dev/null
token=$(curl -fsS -X POST "$BASE/api/v1/auth/login" -H 'content-type: application/json' \
  -d "{\"email\":\"$email\",\"password\":\"$pw\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -fsS -X POST "$BASE/api/v1/councils" -H "authorization: Bearer $token" -H 'content-type: application/json' \
  -d '{"name":"smoke","mode":"research","size":4}' | grep -q '"chair"'
echo "smoke: OK"

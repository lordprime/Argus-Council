#!/usr/bin/env bash
# Write the API's OpenAPI document (FastAPI's app.openapi()) for the documentation site.
#
#   ./scripts/gen-openapi.sh                 # writes docs/api/openapi.json
#   ./scripts/gen-openapi.sh path/to/out.json
#   ./scripts/gen-openapi.sh --check         # exit 1 when docs/api/openapi.json is out of date
#
# Needs uv; the backend's locked dependencies are installed into backend/.venv on first use.
# No database, Redis or provider keys are needed: the schema is built from the routes alone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
check=0 out="$ROOT/docs/api/openapi.json"
case "${1:-}" in
  -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  --check) check=1 ;;
  "") ;;
  *) out="$1" ;;
esac

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
(
  cd "$ROOT/backend"
  ARGUS_ENV=development ARGUS_LOG_LEVEL=WARNING uv run --frozen --quiet python - <<'PY'
import json
import sys

from argus.main import create_app

json.dump(create_app().openapi(), sys.stdout, indent=2, ensure_ascii=False)
sys.stdout.write("\n")
PY
) >"$tmp"

if [ "$check" = 1 ]; then
  if cmp -s "$tmp" "$out"; then
    echo "openapi: $out is up to date"
    exit 0
  fi
  echo "openapi: $out is out of date; run ./scripts/gen-openapi.sh" >&2
  exit 1
fi

mkdir -p "$(dirname "$out")"
mv "$tmp" "$out"
trap - EXIT
echo "openapi: wrote $out"

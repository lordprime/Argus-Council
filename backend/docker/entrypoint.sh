#!/bin/sh
set -eu

if [ "${ARGUS_RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "argus: applying database migrations"
  alembic upgrade head
fi

exec "$@"

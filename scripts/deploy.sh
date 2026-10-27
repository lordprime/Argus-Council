#!/usr/bin/env bash
# Deploy Argus Council with Docker Compose. Safe to re-run.
#
#   ./scripts/deploy.sh             # create deploy/.env if needed, build, start, wait until ready
#   ./scripts/deploy.sh --smoke     # also run the end-to-end smoke test afterwards
#   ./scripts/deploy.sh --no-build  # start the images already built (see build.sh)
#   ./scripts/deploy.sh --ca FILE   # trust an extra CA while building
#
# On first run it copies deploy/.env.example to deploy/.env and generates any empty secret
# (ARGUS_SECRET_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD, ARGUS_SANDBOX_TOKEN). Add provider keys there.

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

build=1 smoke=0 build_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) build=0; shift ;;
    --smoke) smoke=1; shift ;;
    --ca) build_args+=(--ca "${2:?--ca needs a file}"); shift 2 ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
require_docker

secret() {
  if command -v openssl >/dev/null; then openssl rand -hex 32
  else python3 -c 'import secrets; print(secrets.token_hex(32))'; fi
}

if [ ! -f "$ENV_FILE" ]; then
  log "creating deploy/.env from deploy/.env.example"
  cp "$ROOT/deploy/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
for key in ARGUS_SECRET_KEY POSTGRES_PASSWORD REDIS_PASSWORD ARGUS_SANDBOX_TOKEN; do
  if [ -z "$(envval "$key" "")" ]; then
    log "generating $key"
    if grep -qE "^$key=" "$ENV_FILE"; then
      sed -i.bak "s|^$key=.*|$key=$(secret)|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
    else
      echo "$key=$(secret)" >>"$ENV_FILE"
    fi
  fi
done

if [ "$build" = 1 ]; then
  "$ROOT/scripts/build.sh" ${build_args[@]+"${build_args[@]}"}
  version="$(cat "$ROOT/deploy/.built-version")"
else
  version="${ARGUS_VERSION:-$(current_version)}"
fi

log "starting the stack (version $version)"
ARGUS_VERSION="$version" dc up -d --no-build --remove-orphans
wait_healthy 240 || die "deployment is not healthy; see the logs above (./scripts/update.sh can roll back)"
echo "$version" >"$VERSION_FILE"

if [ "$smoke" = 1 ]; then
  log "running the smoke test"
  "$ROOT/scripts/smoke.sh" "$(web_url)"
fi

log "Argus Council $version is up at $(web_url)"
log "the first account you register becomes the admin; set ARGUS_ALLOW_REGISTRATION=false afterwards"

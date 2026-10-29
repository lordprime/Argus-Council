#!/usr/bin/env bash
# Deploy or update Argus Council with Docker Compose: the one script for installs and updates.
#
#   ./scripts/deploy.sh             # build, run every pending migration, start, wait until ready
#   ./scripts/deploy.sh --pull      # git pull the latest code first (an update in one command)
#   ./scripts/deploy.sh --smoke     # also run the end-to-end smoke test afterwards
#   ./scripts/deploy.sh --no-build  # start the images already built (see build.sh)
#   ./scripts/deploy.sh --no-backup # existing deployment: skip the pre-update backup
#   ./scripts/deploy.sh --ca FILE   # trust an extra CA while building
#
# On first run it copies deploy/.env.example to deploy/.env and generates any empty secret
# (ARGUS_SECRET_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD, ARGUS_SANDBOX_TOKEN). Add provider keys there.
# On an existing deployment it hands over to update.sh: backup, build, migrate, start, and an
# automatic rollback (schema included) if the new version does not become ready.

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

build=1 smoke=0 pull=0 backup_flag=() build_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) build=0; shift ;;
    --smoke) smoke=1; shift ;;
    --pull) pull=1; shift ;;
    --no-backup) backup_flag=(--no-backup); shift ;;
    --ca) build_args+=(--ca "${2:?--ca needs a file}"); shift 2 ;;
    -h | --help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
for key in ARGUS_SECRET_KEY POSTGRES_PASSWORD REDIS_PASSWORD ARGUS_SANDBOX_TOKEN ARGUS_RUNNER_TOKEN; do
  if [ -z "$(envval "$key" "")" ]; then
    log "generating $key"
    if grep -qE "^$key=" "$ENV_FILE"; then
      sed -i.bak "s|^$key=.*|$key=$(secret)|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
    else
      echo "$key=$(secret)" >>"$ENV_FILE"
    fi
  fi
done

if [ -f "$VERSION_FILE" ]; then
  # Already deployed: update in place (backup, build, migrate, start, roll back on failure).
  update_args=(${backup_flag[@]+"${backup_flag[@]}"} ${build_args[@]+"${build_args[@]}"})
  [ "$pull" = 1 ] || update_args+=(--no-pull)
  [ "$build" = 1 ] || update_args+=(--images "${ARGUS_VERSION:-$(current_version)}")
  log "existing deployment ($(cat "$VERSION_FILE")): updating"
  "$ROOT/scripts/update.sh" ${update_args[@]+"${update_args[@]}"} || exit 1
else
  if [ "$pull" = 1 ]; then
    git -C "$ROOT" diff --quiet HEAD -- || die "the checkout has local changes; commit or stash them first"
    log "pulling the latest code"
    git -C "$ROOT" pull --ff-only
  fi
  lock_deploy
  if [ "$build" = 1 ]; then
    "$ROOT/scripts/build.sh" ${build_args[@]+"${build_args[@]}"}
    version="$(cat "$ROOT/deploy/.built-version")"
  else
    version="${ARGUS_VERSION:-$(current_version)}"
  fi
  migrate_db "$version" || die "database migrations failed; nothing was started (see the logs above)"
  log "starting the stack (version $version)"
  ARGUS_VERSION="$version" dc up -d --no-build --remove-orphans
  wait_healthy 240 || die "deployment is not healthy; see the logs above"
  echo "$version" >"$VERSION_FILE"
fi
version="$(cat "$VERSION_FILE")"
if [ "$smoke" = 1 ]; then
  log "running the smoke test"
  "$ROOT/scripts/smoke.sh" "$(web_url)"
fi

log "Argus Council $version is up at $(web_url)"
log "the first account you register becomes the admin; set ARGUS_ALLOW_REGISTRATION=false afterwards"

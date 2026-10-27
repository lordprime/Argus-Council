#!/usr/bin/env bash
# Shared helpers for build.sh, deploy.sh and update.sh. Sourced, not run.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/docker-compose.yml"
ENV_FILE="$ROOT/deploy/.env"
VERSION_FILE="$ROOT/deploy/.version"
BACKUP_DIR="${ARGUS_BACKUP_DIR:-$ROOT/backups}"
export DOCKER_BUILDKIT=1

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_docker() {
  command -v docker >/dev/null || die "docker is not installed"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose ...)"
  docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon (is it running, and may this user use it?)"
}

require_env() {
  [ -f "$ENV_FILE" ] || die "missing deploy/.env: run ./scripts/deploy.sh first (it creates one)"
}

# dc ...: docker compose for this stack (project "argus", see the compose file).
dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

# envval KEY DEFAULT: a value from deploy/.env, or DEFAULT when unset or empty.
envval() {
  local v=""
  [ -f "$ENV_FILE" ] && v="$(grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
  printf '%s' "${v:-$2}"
}

# The version tag for images built from the current checkout: short commit, plus -dirty if modified.
git_version() {
  if git -C "$ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
    local v; v="$(git -C "$ROOT" rev-parse --short HEAD)"
    git -C "$ROOT" diff --quiet HEAD -- 2>/dev/null || v="$v-dirty"
    printf '%s' "$v"
  else
    date -u +%Y%m%d%H%M%S
  fi
}

current_version() { [ -f "$VERSION_FILE" ] && cat "$VERSION_FILE" || printf 'latest'; }

web_url() {
  local bind; bind="$(envval ARGUS_WEB_BIND 0.0.0.0)"
  [ "$bind" = "0.0.0.0" ] && bind=127.0.0.1
  printf 'http://%s:%s' "$bind" "$(envval ARGUS_WEB_PORT 8080)"
}

# wait_healthy [SECONDS]: wait until the UI answers and the API reports ready through it.
wait_healthy() {
  local limit="${1:-180}" url; url="$(web_url)"
  log "waiting for $url to become ready (up to ${limit}s)"
  for _ in $(seq 1 "$limit"); do
    if curl -fsS -m 3 "$url/health/ready" 2>/dev/null | grep -q '"status":"ok"' \
      && curl -fsS -m 3 -o /dev/null "$url/"; then
      log "ready: $url"
      return 0
    fi
    sleep 1
  done
  warn "the stack did not become ready in ${limit}s"
  dc ps || true
  dc logs --tail 40 api || true
  return 1
}

# backup: Postgres dump plus the artifact volume, into $BACKUP_DIR. Skipped when nothing runs yet.
backup() {
  if ! dc ps --status running --services 2>/dev/null | grep -qx postgres; then
    log "no running database: skipping backup"
    return 0
  fi
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  local ts; ts="$(date -u +%Y%m%dT%H%M%SZ)"
  log "backing up the database to $BACKUP_DIR/argus-db-$ts.dump"
  dc exec -T postgres pg_dump -U argus -d argus -Fc >"$BACKUP_DIR/argus-db-$ts.dump"
  log "backing up artifacts to $BACKUP_DIR/argus-data-$ts.tgz"
  if ! docker run --rm --network none --entrypoint tar -v argus_argusdata:/data:ro \
    "argus-council/api:$(current_version)" czf - -C /data . >"$BACKUP_DIR/argus-data-$ts.tgz"; then
    warn "artifact backup failed; the database dump is still in place"
    rm -f "$BACKUP_DIR/argus-data-$ts.tgz"
  fi
}

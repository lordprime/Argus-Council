#!/usr/bin/env bash
# Shared helpers for build.sh, deploy.sh, update.sh, backup.sh and restore.sh. Sourced, not run.
# shellcheck disable=SC2034 # variables defined here are used by the scripts that source this file

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/docker-compose.yml"
ENV_FILE="$ROOT/deploy/.env"
VERSION_FILE="$ROOT/deploy/.version"
PREV_FILE="$ROOT/deploy/.previous-version"
# What --rollback needs to undo the last update: image tag, database revision and pre-update dump.
STATE_FILE="$ROOT/deploy/.rollback-state"
BACKUP_DIR="${ARGUS_BACKUP_DIR:-$ROOT/backups}"
export DOCKER_BUILDKIT=1

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Exit hooks, run newest first when the script exits (also through die).
_EXIT_HOOKS=()
_run_exit_hooks() {
  local i
  for ((i = ${#_EXIT_HOOKS[@]} - 1; i >= 0; i--)); do
    if [ -n "${_EXIT_HOOKS[$i]}" ]; then eval "${_EXIT_HOOKS[$i]}" || true; fi
  done
}
trap _run_exit_hooks EXIT
add_exit_hook() { _EXIT_HOOKS+=("$1"); }
remove_exit_hook() {
  local i
  for ((i = 0; i < ${#_EXIT_HOOKS[@]}; i++)); do
    if [ "${_EXIT_HOOKS[$i]}" = "$1" ]; then _EXIT_HOOKS[i]=""; fi
  done
}

# lock_deploy: only one of update/restore/backup may change the deployment at a time.
lock_deploy() {
  local lock="$ROOT/deploy/.lock" pid
  if ! mkdir "$lock" 2>/dev/null; then
    pid="$(cat "$lock/pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      die "another update, backup or restore is running (pid $pid)"
    fi
    warn "removing a stale lock left by pid ${pid:-unknown}"
    rm -rf "$lock"
    mkdir "$lock" || die "cannot take the lock $lock"
  fi
  echo "$$" >"$lock/pid"
  add_exit_hook "rm -rf '$lock'"
}

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

# dc_timeout SECONDS ...: dc, killed after SECONDS when `timeout` exists. `up` waits for
# depends_on health and can wait indefinitely on an API that crash-loops.
dc_timeout() {
  local t="$1"; shift
  if command -v timeout >/dev/null; then
    timeout "$t" docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
  else
    dc "$@"
  fi
}

# envval KEY DEFAULT: a value from deploy/.env, or DEFAULT when unset or empty.
envval() {
  local v=""
  [ -f "$ENV_FILE" ] && v="$(grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
  printf '%s' "${v:-$2}"
}

# set_envval KEY VALUE: set KEY to VALUE in deploy/.env, replacing its line(s), else appending one.
# awk with the values from the environment: VALUE may hold any of / | & \ : that would break sed.
set_envval() {
  local tmp
  [ -f "$ENV_FILE" ] || : >"$ENV_FILE"
  tmp="$(mktemp "$ENV_FILE.XXXXXX")" || return 1
  if K="$1" V="$2" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; n = length(k) + 1 }
    substr($0, 1, n) == (k "=") { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" >"$tmp"; then
    mv -f "$tmp" "$ENV_FILE" # mktemp's mode is 0600, as deploy/.env should be
  else
    rm -f "$tmp"
    return 1
  fi
}

# secret: 32 random bytes as hex, for passwords and tokens.
secret() {
  if command -v openssl >/dev/null; then openssl rand -hex 32
  else python3 -c 'import secrets; print(secrets.token_hex(32))'; fi
}

# ensure_secret KEY: generate KEY in deploy/.env when it is empty or missing; never replaces one.
ensure_secret() {
  [ -z "$(envval "$1" '')" ] || return 0
  local s
  s="$(secret)" || s=""
  [ -n "$s" ] || die "cannot generate $1 (needs openssl or python3)"
  log "generating $1"
  set_envval "$1" "$s"
}

# The scripts start local images, argus-council/<svc>:<version>: built from this checkout, or
# pulled from a release and tagged so (build.sh --release). The compose file defaults to the
# registry's names (ghcr.io/...), so point it at the local ones; the environment beats deploy/.env.
export ARGUS_IMAGE_REPO=argus-council
# Where build.sh --release pulls from (the images release.yml publishes).
RELEASE_REPO="${ARGUS_RELEASE_REPO:-$(envval ARGUS_RELEASE_REPO ghcr.io/lordprime/argus-council)}"

# stateval KEY: a value from $STATE_FILE, empty when unset.
stateval() {
  [ -f "$STATE_FILE" ] || return 0
  grep -E "^$1=" "$STATE_FILE" | tail -n1 | cut -d= -f2- || true
}

# write_state KEY=VALUE...: replace $STATE_FILE atomically.
write_state() {
  printf '%s\n' "$@" >"$STATE_FILE.tmp"
  mv -f "$STATE_FILE.tmp" "$STATE_FILE"
}

# The version tag for images built from the current checkout: the short commit. A checkout with
# changes (including untracked files, such as a new migration) gets -dirty-<UTC time>, so a
# rebuild never reuses, and overwrites, the tag of an image that is running.
git_version() {
  if git -C "$ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
    local v; v="$(git -C "$ROOT" rev-parse --short HEAD)"
    if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal 2>/dev/null || echo unknown)" ]; then
      v="$v-dirty-$(date -u +%Y%m%d%H%M%S)"
    fi
    printf '%s' "$v"
  else
    date -u +%Y%m%d%H%M%S
  fi
}

current_version() {
  if [ -f "$VERSION_FILE" ]; then cat "$VERSION_FILE"; else printf 'latest'; fi
}

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

# ---------------------------------------------------------------- images

image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

# profile_on PROFILE: COMPOSE_PROFILES (the environment, else deploy/.env) lists PROFILE.
profile_on() {
  case ",${COMPOSE_PROFILES:-$(envval COMPOSE_PROFILES '')}," in
    *,"$1",*) return 0 ;;
    *) return 1 ;;
  esac
}

# cli_agents_on: the opt-in agent runner (CLI agents) is part of this deployment.
cli_agents_on() { profile_on cli-agents; }

# browser_on: the opt-in live browser (browser and egress services) is part of this deployment.
browser_on() { profile_on browser; }

BROWSER_SOCKET_URL=unix:/run/argus-browser/browser.sock

# sync_feature_env: derive the settings of the opt-in features from COMPOSE_PROFILES, so the
# profile is the only server-side switch (deploy.sh --with/--without edits it; a hand edit counts
# too). An installed feature gets what it needs, a URL or token already set is kept; a removed one
# is turned off and its token left alone. Logs each change; running it again changes nothing.
sync_feature_env() {
  if browser_on; then
    if [ -z "$(envval ARGUS_BROWSER_URL '')" ]; then
      log "browser installed: setting ARGUS_BROWSER_URL=$BROWSER_SOCKET_URL"
      set_envval ARGUS_BROWSER_URL "$BROWSER_SOCKET_URL"
    fi
    ensure_secret ARGUS_BROWSER_TOKEN
  elif [ -n "$(envval ARGUS_BROWSER_URL '')" ]; then
    log "browser not installed: clearing ARGUS_BROWSER_URL"
    set_envval ARGUS_BROWSER_URL ""
  fi
  if cli_agents_on; then
    if [ "$(envval ARGUS_CLI_AGENTS_ENABLED '')" != true ]; then
      log "cli-agents installed: setting ARGUS_CLI_AGENTS_ENABLED=true"
      set_envval ARGUS_CLI_AGENTS_ENABLED true
    fi
    ensure_secret ARGUS_RUNNER_TOKEN
  elif [ "$(envval ARGUS_CLI_AGENTS_ENABLED false)" != false ]; then
    # A missing line is already off: the compose file defaults it to false.
    log "cli-agents not installed: setting ARGUS_CLI_AGENTS_ENABLED=false"
    set_envval ARGUS_CLI_AGENTS_ENABLED false
  fi
}

# remove_disabled_features: stop and remove the containers of an opt-in feature that is no longer
# in COMPOSE_PROFILES. `up --remove-orphans` leaves them running (they are still services of the
# compose file, only in an inactive profile), so they are named here with their profile enabled
# for this one command. Volumes are kept (the runner's holds the users' CLI sign-ins), so
# installing the feature again brings it back as it was. Best effort: never fails a deployment.
remove_disabled_features() {
  local profile svc svcs=() present=()
  for profile in browser cli-agents; do
    if profile_on "$profile"; then continue; fi
    case "$profile" in
      browser) svcs=(browser egress) ;;
      cli-agents) svcs=(runner) ;;
    esac
    present=()
    for svc in "${svcs[@]}"; do
      if [ -n "$(dc --profile "$profile" ps -a -q "$svc" 2>/dev/null || true)" ]; then present+=("$svc"); fi
    done
    [ ${#present[@]} -gt 0 ] || continue
    log "$profile is not installed: removing its containers (${present[*]}), keeping their volumes"
    dc --profile "$profile" rm -s -f "${present[@]}" \
      || warn "could not remove ${present[*]}; try: docker compose --profile $profile rm -s -f ${present[*]}"
  done
  return 0
}

# image_services: the services built from this repository: api (also runs the sandbox and the
# browser's egress proxy), web, runner when CLI agents are enabled, and browser when the live
# browser is.
image_services() {
  printf '%s\n' api web
  if cli_agents_on; then printf '%s\n' runner; fi
  if browser_on; then printf '%s\n' browser; fi
}

# optional_service SERVICE: an image that is only there when its profile is on (runner, browser).
optional_service() { [ "$1" = runner ] || [ "$1" = browser ]; }

# snapshot_images VERSION: give the images that run now (or, when none run, the ones tagged
# VERSION) a new tag rollback-<UTC time>, and print that tag. Rolling back to it starts exactly
# these images even after a rebuild has moved VERSION to new ones. Prints VERSION when there is
# nothing to snapshot. api and web are required; the optional runner and browser are kept when
# they exist.
snapshot_images() {
  local version="$1" base tag n=1 svc cid img i svcs=() imgs=()
  while IFS= read -r svc; do
    img=""
    cid="$(dc ps -a -q "$svc" 2>/dev/null | head -n1 || true)"
    [ -n "$cid" ] && img="$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true)"
    if [ -z "$img" ] && image_exists "argus-council/$svc:$version"; then
      img="argus-council/$svc:$version"
    fi
    if [ -z "$img" ]; then
      if optional_service "$svc"; then
        warn "no $svc image to keep for a rollback (its profile was just enabled?)"
        continue
      fi
      warn "no $svc image to keep for a rollback; the rollback target stays $version"
      printf '%s' "$version"
      return 0
    fi
    svcs+=("$svc") imgs+=("$img")
  done < <(image_services)
  base="rollback-$(date -u +%Y%m%dT%H%M%SZ)"
  tag="$base"
  while image_exists "argus-council/api:$tag" || image_exists "argus-council/web:$tag"; do
    n=$((n + 1)); tag="$base-$n"
  done
  for i in "${!svcs[@]}"; do
    docker tag "${imgs[$i]}" "argus-council/${svcs[$i]}:$tag" >&2
  done
  log "kept the images of $version as $tag (the rollback target)" >&2
  printf '%s' "$tag"
}

# prune_snapshots: drop rollback-* tags except the newest ARGUS_SNAPSHOT_KEEP (default 3) and any
# tag the version, previous-version or rollback-state files still point to. Best effort.
prune_snapshots() {
  local keep="${ARGUS_SNAPSHOT_KEEP:-3}" svc tag n in_use
  in_use=" $(current_version) $(cat "$PREV_FILE" 2>/dev/null || true) $(stateval version) "
  for svc in api web runner browser; do
    n=0
    while IFS= read -r tag; do
      n=$((n + 1))
      [ "$n" -gt "$keep" ] || continue
      case "$in_use" in *" $tag "*) continue ;; esac
      docker image rm "argus-council/$svc:$tag" >/dev/null 2>&1 || true
    done < <(docker image ls "argus-council/$svc" --format '{{.Tag}}' 2>/dev/null | grep '^rollback-' | sort -r || true)
  done
}

# ---------------------------------------------------------------- database

# (Not `dc ps | grep -q`: with pipefail, grep exiting early can fail the pipeline with SIGPIPE.)
db_running() {
  local services
  services="$(dc ps --status running --services 2>/dev/null)" || return 1
  grep -qx postgres <<<"$services"
}

# psql_q ARGS...: psql as argus on the argus database inside the postgres container.
psql_q() { dc exec -T postgres psql -X -q -U argus -d argus -v ON_ERROR_STOP=1 "$@"; }

# ensure_db: start postgres if needed and wait until it accepts connections.
ensure_db() {
  dc up -d postgres >/dev/null
  for _ in $(seq 1 60); do
    dc exec -T postgres pg_isready -U argus -d argus >/dev/null 2>&1 && return 0
    sleep 1
  done
  warn "postgres did not become ready"
  return 1
}

# db_revision: the Alembic revision(s) of the database, comma-separated, or "base" when no
# migration has run yet. Fails when the database cannot be queried.
db_revision() {
  local exists revs
  exists="$(psql_q -tAc "SELECT to_regclass('public.alembic_version') IS NOT NULL")" || return 1
  if [ "$exists" != t ]; then
    printf 'base'
    return 0
  fi
  revs="$(psql_q -tAc "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version")" || return 1
  printf '%s' "${revs:-base}"
}

# migrate_db VERSION: apply every pending Alembic migration with the images of VERSION, as its own
# step, before that version's API starts (the API entrypoint would otherwise migrate on boot, where a
# failure only shows up as a crash loop). Logs the revision before and after. Safe to repeat.
migrate_db() {
  local version="$1" before after
  ensure_db || return 1
  before="$(db_revision)" || { warn "cannot read the database revision"; return 1; }
  log "applying database migrations with version $version (schema at $before)"
  if ! ARGUS_VERSION="$version" dc run --rm --no-deps -T -e ARGUS_RUN_MIGRATIONS=false api \
    alembic upgrade head; then
    warn "database migrations failed with version $version"
    return 1
  fi
  after="$(db_revision)" || return 1
  if [ "$before" = "$after" ]; then
    log "database schema up to date at revision $after"
  else
    log "database schema migrated $before -> $after"
  fi
}

# restore_db DUMP: replace the whole database with DUMP, in one transaction. The public schema is
# dropped first, so tables that a newer migration created and the dump does not have are removed
# too (pg_restore --clean only drops what the dump contains). If anything fails, the transaction
# is rolled back and the database is left as it was.
restore_db() {
  local dump="$1"
  log "restoring the database from $dump"
  (
    printf '%s\n' 'BEGIN;' \
      'DROP SCHEMA IF EXISTS public CASCADE;' \
      'CREATE SCHEMA public AUTHORIZATION pg_database_owner;' \
      'GRANT USAGE ON SCHEMA public TO PUBLIC;' \
      "COMMENT ON SCHEMA public IS 'standard public schema';"
    if dc exec -T postgres pg_restore --no-owner -f - <"$dump"; then
      printf '%s\n' 'COMMIT;'
    else
      # psql would otherwise run what it already received; without COMMIT the server rolls back.
      printf '%s\n' 'ROLLBACK;'
      exit 1
    fi
  ) | psql_q >/dev/null
}

# ---------------------------------------------------------------- backups

sha256_file() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# check_sha256 FILE: compare FILE with FILE.sha256. Returns 0 on a match, 1 on a mismatch and
# 2 when there is no checksum file (backups made by older versions of these scripts).
check_sha256() {
  local f="$1" want got
  [ -f "$f.sha256" ] || return 2
  want="$(cut -d' ' -f1 <"$f.sha256")"
  got="$(sha256_file "$f")"
  [ -n "$want" ] && [ "$want" = "$got" ]
}

# verify_checksum FILE: warn and fail on a checksum mismatch; only warn when there is no checksum.
verify_checksum() {
  local rc=0
  check_sha256 "$1" || rc=$?
  case "$rc" in
    0) return 0 ;;
    2) warn "no checksum for $1 (made by an older version of these scripts)"; return 0 ;;
    *) warn "checksum mismatch: $1 is corrupt or was changed after the backup"; return 1 ;;
  esac
}

# verify_dump_archive FILE: pg_restore reads FILE to the end, which fails on a truncated or
# corrupt dump (pg_restore --list only reads the table of contents). Needs postgres running.
verify_dump_archive() {
  if ! dc exec -T postgres pg_restore -f /dev/null <"$1" >/dev/null 2>&1; then
    warn "$1 is not a complete pg_dump archive (truncated or corrupt)"
    return 1
  fi
}

# verify_dump FILE: checksum, then a full read of the archive.
verify_dump() {
  [ -s "$1" ] || { warn "$1 is missing or empty"; return 1; }
  verify_checksum "$1" && verify_dump_archive "$1"
}

# verify_tarball FILE: checksum, then a full gzip and tar read.
verify_tarball() {
  [ -s "$1" ] || { warn "$1 is missing or empty"; return 1; }
  verify_checksum "$1" || return 1
  if ! gzip -t "$1" 2>/dev/null || ! tar tzf "$1" >/dev/null 2>&1; then
    warn "$1 is not a complete gzip tarball (truncated or corrupt)"
    return 1
  fi
}

# _finish_backup PARTIAL FINAL: write FINAL.sha256, then move PARTIAL into place. The checksum
# goes first, so a FINAL file without its checksum can only come from an older script version.
_finish_backup() {
  local partial="$1" final="$2"
  printf '%s  %s\n' "$(sha256_file "$partial")" "${final##*/}" >"$final.sha256"
  mv -f "$partial" "$final"
}

# backup: the artifact volume, then a Postgres dump, into $BACKUP_DIR, each with a .sha256 file.
# Sets LAST_BACKUP to the dump's path (empty when nothing runs yet, which is not an error).
# Each file is written as .partial, verified, and only then renamed, so a failed or interrupted
# backup never leaves a file that looks like a valid backup. Returns non-zero when the database
# dump fails; a failed artifact backup only warns.
backup() {
  LAST_BACKUP=""
  if ! db_running; then
    log "no running database: skipping backup"
    return 0
  fi
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  local base ts n=1 dump tgz
  base="$(date -u +%Y%m%dT%H%M%SZ)"
  ts="$base"
  while [ -e "$BACKUP_DIR/argus-db-$ts.dump" ] || [ -e "$BACKUP_DIR/argus-data-$ts.tgz" ]; do
    n=$((n + 1)); ts="$base-$n"
  done
  dump="$BACKUP_DIR/argus-db-$ts.dump"
  tgz="$BACKUP_DIR/argus-data-$ts.tgz"

  # Artifacts first: blobs are content-addressed and only ever added, so every blob that the
  # (later) dump references is in the tarball. Blobs newer than the dump are harmless extras.
  log "backing up artifacts to $tgz"
  if docker run --rm --network none --entrypoint tar -v argus_argusdata:/data:ro \
    "argus-council/api:$(current_version)" czf - -C /data . >"$tgz.partial" \
    && gzip -t "$tgz.partial" 2>/dev/null; then
    _finish_backup "$tgz.partial" "$tgz"
  else
    warn "artifact backup failed; continuing with the database dump"
    rm -f "$tgz.partial"
  fi

  log "backing up the database to $dump"
  if ! dc exec -T postgres pg_dump -U argus -d argus -Fc >"$dump.partial"; then
    rm -f "$dump.partial"
    warn "pg_dump failed: no database backup was written"
    return 1
  fi
  if ! verify_dump_archive "$dump.partial"; then
    rm -f "$dump.partial"
    return 1
  fi
  _finish_backup "$dump.partial" "$dump"
  LAST_BACKUP="$dump"
  log "backup $ts complete ($(du -h "$dump" | cut -f1) database)"
  prune_backups
}

# backup_stamps: timestamps of the database dumps in $BACKUP_DIR, newest first.
backup_stamps() {
  local f
  [ -d "$BACKUP_DIR" ] || return 0
  for f in "$BACKUP_DIR"/argus-db-*.dump; do
    [ -e "$f" ] || continue
    f="${f##*/argus-db-}"
    printf '%s\n' "${f%.dump}"
  done | sort -r
}

# prune_backups: keep the newest ARGUS_BACKUP_KEEP (default 14) dump/artifact pairs; 0 keeps all.
# Also removes .partial files left by interrupted backups (callers hold the deploy lock).
prune_backups() {
  local keep="${ARGUS_BACKUP_KEEP:-$(envval ARGUS_BACKUP_KEEP 14)}" n=0 ts f
  for f in "$BACKUP_DIR"/*.partial; do
    if [ -e "$f" ]; then rm -f "$f"; fi
  done
  case "$keep" in '' | *[!0-9]*) warn "ARGUS_BACKUP_KEEP must be a number (got '$keep'): not pruning"; return 0 ;; esac
  [ "$keep" -gt 0 ] || return 0
  while IFS= read -r ts; do
    n=$((n + 1))
    [ "$n" -gt "$keep" ] || continue
    log "pruning old backup $ts (keeping the newest $keep)"
    rm -f "$BACKUP_DIR/argus-db-$ts.dump" "$BACKUP_DIR/argus-db-$ts.dump.sha256" \
      "$BACKUP_DIR/argus-data-$ts.tgz" "$BACKUP_DIR/argus-data-$ts.tgz.sha256"
  done < <(backup_stamps)
}

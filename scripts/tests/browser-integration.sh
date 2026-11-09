#!/usr/bin/env bash
# Real-browser integration test: the browser and egress containers (compose profile `browser`)
# against two local test sites, run by backend/tests/integration/test_browser_real.py.
#
#   ./scripts/tests/browser-integration.sh               # build browser + api images, run, clean up
#   ./scripts/tests/browser-integration.sh --no-build    # use $ARGUS_IMAGE_REPO/{api,browser}:$ARGUS_VERSION
#                                                        #   (default argus-council/...:browser-test)
#   ./scripts/tests/browser-integration.sh --keep        # leave the stack and temp dir up afterwards
#   ./scripts/tests/browser-integration.sh -- -k frames  # extra arguments go to pytest
#
# Needs Docker with Compose v2, uv, and internet access (base images; a throwaway Debian container
# installs openssl and certutil to make the test CA). The stack runs as compose project
# `argus-browser-test`, separate from a deployed `argus` stack, with its own networks and volumes:
#   - `site.argus.test` (a public-range address on an internal network that only egress shares),
#     served over http and https with a throwaway CA that only the test browser trusts, and also
#     as `frames.argus-other.test` (another site, embedded in a cross-site iframe);
#   - `canary.argus.test` (a private address egress can route to): it must never be reached.
# The test reaches the browser service over its Unix socket (bind-mounted from a temp dir) and runs
# commands in the browser container (download and agent-browser policy checks) with `exec`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build=1 keep=0 pytest_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) build=0; shift ;;
    --keep) keep=1; shift ;;
    --) shift; pytest_args=("$@"); break ;;
    -h | --help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv is not installed (https://docs.astral.sh/uv/)" >&2; exit 1; }

# A short path: the service's socket lives here, and Unix socket paths are limited to 107 bytes.
TEST_DIR="$(mktemp -d "${ARGUS_TEST_TMP:-/tmp}/argus-browser-test.XXXXXX")"
mkdir -p "$TEST_DIR"/{sock,site,canary,tls}
# The browser service (uid 10003) creates its socket here; the sites (this user) write their logs.
chmod 0755 "$TEST_DIR"
chmod 1777 "$TEST_DIR/sock"

# Compose interpolates the whole file: the services this test does not start still need values.
# They go in an env file too, so the compose command printed with --keep (and the one
# scripts/tests/browser-diagnose.sh uses) works from any shell, not only this one.
ARGUS_TEST_UID="$(id -u)"
ARGUS_TEST_GID="$(id -g)"
export ARGUS_TEST_DIR="$TEST_DIR" ARGUS_TEST_UID ARGUS_TEST_GID
export COMPOSE_PROFILES=browser ARGUS_IMAGE_REPO="${ARGUS_IMAGE_REPO:-argus-council}"
export ARGUS_VERSION="${ARGUS_VERSION:-browser-test}"
export POSTGRES_PASSWORD=unused REDIS_PASSWORD=unused ARGUS_SECRET_KEY=unused ARGUS_SANDBOX_TOKEN=unused
ARGUS_BROWSER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export ARGUS_BROWSER_TOKEN
(
  umask 077
  for var in ARGUS_TEST_DIR ARGUS_TEST_UID ARGUS_TEST_GID COMPOSE_PROFILES ARGUS_IMAGE_REPO ARGUS_VERSION \
    POSTGRES_PASSWORD REDIS_PASSWORD ARGUS_SECRET_KEY ARGUS_SANDBOX_TOKEN ARGUS_BROWSER_TOKEN; do
    printf '%s=%s\n' "$var" "${!var}"
  done >"$TEST_DIR/compose.env"
)

DC=(docker compose -p argus-browser-test --env-file "$TEST_DIR/compose.env"
  -f "$ROOT/deploy/docker-compose.yml" -f "$ROOT/deploy/docker-compose.build.yml"
  -f "$ROOT/deploy/docker-compose.browser-test.yml")

cleanup() {
  local rc=$?
  if [ "$rc" != 0 ]; then
    log "failed: browser and egress logs follow"
    "${DC[@]}" logs --no-color --tail 200 egress browser 2>/dev/null || true
  fi
  if [ "$keep" = 1 ]; then
    log "kept the stack and $TEST_DIR"
    log "diagnose: ./scripts/tests/browser-diagnose.sh $TEST_DIR"
    log "clean up: $(printf '%q ' "${DC[@]}")down -v --remove-orphans && rm -rf $(printf '%q' "$TEST_DIR")"
  else
    "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$TEST_DIR" 2>/dev/null || log "could not remove all of $TEST_DIR (files owned by container users)"
  fi
  exit "$rc"
}
trap cleanup EXIT

log "making a throwaway CA, the test sites' certificate and an NSS database that trusts the CA"
docker run --rm -e HOST_UID="$ARGUS_TEST_UID" -e HOST_GID="$ARGUS_TEST_GID" \
  -v "$TEST_DIR/tls:/out:z" debian:bookworm-slim sh -euc '
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends openssl libnss3-tools >/dev/null
    cd /out
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=Argus browser test CA" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -keyout ca-key.pem -out ca.pem 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -subj "/CN=site.argus.test" -keyout site-key.pem -out site.csr 2>/dev/null
    printf "%s\n" "subjectAltName=DNS:site.argus.test,DNS:frames.argus-other.test,DNS:canary.argus.test" \
      "extendedKeyUsage=serverAuth" "basicConstraints=CA:FALSE" >ext.cnf
    openssl x509 -req -in site.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 2 \
      -extfile ext.cnf -out site.pem 2>/dev/null
    mkdir nssdb
    certutil -N -d sql:nssdb --empty-password
    certutil -A -d sql:nssdb -n "Argus browser test CA" -t "C,," -i ca.pem
    rm -f ca-key.pem site.csr ext.cnf ca.srl
    chown -R "$HOST_UID:$HOST_GID" /out
    chmod -R a+rX /out'

if [ "$build" = 1 ]; then
  log "building $ARGUS_IMAGE_REPO/{api,browser}:$ARGUS_VERSION from this checkout"
  "${DC[@]}" build egress browser
fi

log "starting browser, egress and the test sites"
"${DC[@]}" up -d --no-build --wait --wait-timeout 180 browser egress site canary

# Chrome reads trusted roots from $HOME/.pki/nssdb (HOME=/tmp, a tmpfs): only this test's Chrome
# trusts the throwaway CA, and only until the container stops.
"${DC[@]}" exec -T browser sh -c 'mkdir -p "$HOME/.pki" && cp -R /argus-test-tls/nssdb "$HOME/.pki/nssdb"'

exec_prefix="$(printf '%q ' "${DC[@]}" exec -T browser)"
log "running the integration test"
cd "$ROOT/backend" || exit 1
ARGUS_TEST_BROWSER_URL="unix:$TEST_DIR/sock/browser.sock" \
ARGUS_TEST_BROWSER_TOKEN="$ARGUS_BROWSER_TOKEN" \
ARGUS_TEST_SITE_URL="https://site.argus.test" \
ARGUS_TEST_OTHER_SITE_URL="https://frames.argus-other.test" \
ARGUS_TEST_SITE_LOG="$TEST_DIR/site" \
ARGUS_TEST_CANARY_URL="https://canary.argus.test" \
ARGUS_TEST_CANARY_LOG="$TEST_DIR/canary" \
ARGUS_TEST_BROWSER_EXEC="$exec_prefix" \
  uv run pytest -m browser tests/integration/test_browser_real.py -v ${pytest_args[@]+"${pytest_args[@]}"} \
  || rc=$?

# Chrome's own background traffic: every session above started a Chrome and opened pages, and
# none of them may have reached Google's services through egress (www.google.com, network time,
# autofill, captive portal checks, ...). Only the test sites belong in egress's allow lines.
log "checking egress for Chrome background traffic to Google services"
google="$("${DC[@]}" logs --no-color egress 2>/dev/null \
  | grep 'egress.allow' | grep -oE '(host=|"host": ?")[^ ",]*(google\.com|gstatic\.com|googleapis\.com)' \
  | sort | uniq -c || true)"
if [ -n "$google" ]; then
  echo "FAIL: Chrome reached Google services through egress:" >&2
  echo "$google" >&2
  rc="${rc:-1}"
else
  echo "ok: no egress.allow for google.com, gstatic.com or googleapis.com hosts"
fi
exit "${rc:-0}"

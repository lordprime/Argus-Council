#!/usr/bin/env bash
# Build the Argus Council images (api, which also runs the sandbox and the egress proxy, web, the
# CLI agents runner when COMPOSE_PROFILES includes cli-agents, and the live browser when it
# includes browser).
#
#   ./scripts/build.sh                     # tag with the current commit and :latest
#   ./scripts/build.sh --version 1.2.0     # explicit tag
#   ./scripts/build.sh --ca corp-ca.crt    # trust an extra CA during the build (egress proxies)
#   ./scripts/build.sh --no-cache --pull web
#   ./scripts/build.sh --from-images ci    # no build: tag existing images (built elsewhere, pulled)
#   ./scripts/build.sh --release 0.1.4     # no build: pull a published release (or `latest`)
#
# Options: --version TAG, --ca FILE (or ARGUS_EXTRA_CA), --no-cache, --pull, --from-images TAG,
# --release VERSION (from ARGUS_RELEASE_REPO, default ghcr.io/lordprime/argus-council), and the
# services to build (api, web, runner, browser; default api and web, plus runner with CLI agents
# and browser with the live browser).

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

version="" ca="${ARGUS_EXTRA_CA:-}" from="" release="" services=() opts=()
while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="${2:?--version needs a tag}"; shift 2 ;;
    --ca) ca="${2:?--ca needs a file}"; shift 2 ;;
    --from-images) from="${2:?--from-images needs a tag}"; shift 2 ;;
    --release) release="${2:?--release needs a version}"; shift 2 ;;
    --no-cache | --pull) opts+=("$1"); shift ;;
    api | web | runner | browser) services+=("$1"); shift ;;
    -h | --help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
if [ ${#services[@]} -eq 0 ]; then
  while IFS= read -r svc; do services+=("$svc"); done < <(image_services)
fi
[ -z "$release" ] || [ -z "$from" ] || die "--release and --from-images cannot be combined"
require_docker

if [ -n "$release" ]; then
  release="${release#v}"
  case "$release" in *[!0-9A-Za-z.-]*) die "not a release version: $release" ;; esac
  for svc in "${services[@]}"; do
    ref="$RELEASE_REPO/$svc:$release"
    log "pulling $ref"
    for attempt in 1 2 3; do
      if docker pull -q "$ref" >/dev/null; then break; fi
      [ "$attempt" = 3 ] && die "pulling $ref failed 3 times (does release $release exist, and can this host reach ${RELEASE_REPO%%/*}?)"
      warn "pulling $ref failed (attempt $attempt); retrying in $((attempt * 10))s"
      sleep $((attempt * 10))
    done
  done
  if [ -z "$version" ]; then
    # Record a fixed version, not `latest`: a rollback must find the same images again.
    version="$release"
    if [ "$release" = latest ]; then
      version="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
        "$RELEASE_REPO/api:latest" 2>/dev/null || true)"
      case "$version" in '' | '<no value>' | *[!0-9A-Za-z.-]*) version="latest-$(date -u +%Y%m%d%H%M%S)" ;; esac
    fi
  fi
  for svc in "${services[@]}"; do
    if image_exists "argus-council/$svc:$version"; then
      warn "argus-council/$svc:$version exists and will point at the pulled release"
    fi
    docker tag "$RELEASE_REPO/$svc:$release" "argus-council/$svc:$version"
    docker tag "$RELEASE_REPO/$svc:$release" "argus-council/$svc:latest"
  done
  log "pulled release $release (${services[*]}) as version $version"
  echo "$version" >"$ROOT/deploy/.built-version"
  exit 0
fi

version="${version:-$(git_version)}"
# Rebuilding an existing tag moves it to the new images. update.sh tags the running images
# rollback-<time> first, so its rollback target survives; a manual rebuild has no such copy.
for svc in "${services[@]}"; do
  if image_exists "argus-council/$svc:$version"; then
    warn "argus-council/$svc:$version exists and will point at the new build"
  fi
done

if [ -n "$from" ]; then
  for svc in "${services[@]}"; do
    image_exists "argus-council/$svc:$from" || die "no image argus-council/$svc:$from"
    log "tagging argus-council/$svc:$from as $version"
    docker tag "argus-council/$svc:$from" "argus-council/$svc:$version"
    docker tag "argus-council/$svc:$from" "argus-council/$svc:latest"
  done
  echo "$version" >"$ROOT/deploy/.built-version"
  exit 0
fi

if [ -n "$ca" ]; then
  [ -f "$ca" ] || die "CA file not found: $ca"
  opts+=(--secret "id=extra_ca,src=$ca")
fi

for svc in "${services[@]}"; do
  dockerfile=()
  case "$svc" in
    api) context="$ROOT/backend" ;;
    # Never reuse a cached runtime stage: its `apk upgrade` would replay an old run and miss OS fixes.
    web) context="$ROOT/frontend" dockerfile=(--no-cache-filter runtime) ;;
    runner) context="$ROOT/backend" dockerfile=(-f "$ROOT/backend/docker/runner.Dockerfile") ;;
    browser) context="$ROOT/backend" dockerfile=(-f "$ROOT/backend/docker/browser.Dockerfile") ;;
  esac
  log "building argus-council/$svc:$version"
  # Retry: base-image pulls fail transiently (registry rate limits, network blips).
  for attempt in 1 2 3; do
    if docker build ${opts[@]+"${opts[@]}"} ${dockerfile[@]+"${dockerfile[@]}"} -t "argus-council/$svc:$version" -t "argus-council/$svc:latest" "$context"; then
      break
    fi
    [ "$attempt" = 3 ] && die "building $svc failed 3 times"
    warn "building $svc failed (attempt $attempt); retrying in $((attempt * 30))s"
    sleep $((attempt * 30))
  done
done

log "built ${services[*]} as version $version"
echo "$version" >"$ROOT/deploy/.built-version"

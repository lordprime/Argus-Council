#!/usr/bin/env bash
# Build the Argus Council images (api, which also runs the sandbox, and web).
#
#   ./scripts/build.sh                     # tag with the current commit and :latest
#   ./scripts/build.sh --version 1.2.0     # explicit tag
#   ./scripts/build.sh --ca corp-ca.crt    # trust an extra CA during the build (egress proxies)
#   ./scripts/build.sh --no-cache --pull web
#   ./scripts/build.sh --from-images ci    # no build: tag existing images (built elsewhere, pulled)
#
# Options: --version TAG, --ca FILE (or ARGUS_EXTRA_CA), --no-cache, --pull, --from-images TAG,
# and the services to build (api, web; default both).

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

version="" ca="${ARGUS_EXTRA_CA:-}" from="" services=() opts=()
while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="${2:?--version needs a tag}"; shift 2 ;;
    --ca) ca="${2:?--ca needs a file}"; shift 2 ;;
    --from-images) from="${2:?--from-images needs a tag}"; shift 2 ;;
    --no-cache | --pull) opts+=("$1"); shift ;;
    api | web) services+=("$1"); shift ;;
    -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
[ ${#services[@]} -gt 0 ] || services=(api web)
version="${version:-$(git_version)}"
require_docker
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
  case "$svc" in
    api) context="$ROOT/backend" ;;
    web) context="$ROOT/frontend" ;;
  esac
  log "building argus-council/$svc:$version"
  # Retry: base-image pulls fail transiently (registry rate limits, network blips).
  for attempt in 1 2 3; do
    if docker build ${opts[@]+"${opts[@]}"} -t "argus-council/$svc:$version" -t "argus-council/$svc:latest" "$context"; then
      break
    fi
    [ "$attempt" = 3 ] && die "building $svc failed 3 times"
    warn "building $svc failed (attempt $attempt); retrying in $((attempt * 30))s"
    sleep $((attempt * 30))
  done
done

log "built ${services[*]} as version $version"
echo "$version" >"$ROOT/deploy/.built-version"

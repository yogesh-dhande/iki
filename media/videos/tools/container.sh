#!/usr/bin/env bash
# Runs a command in the videos' render image (Dockerfile), on this checkout: every render
# and still comes out of the same image, so they match from machine to machine.
#   tools/container.sh node tools/render.ts entities
# IKI_VIDEO_NATIVE=1 runs it on this machine instead: quicker to start, but its text may
# be antialiased differently, so never commit a render made that way.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
if [[ "${IKI_VIDEO_NATIVE:-}" == 1 ]]; then
  cd "$here" && exec "$@"
fi
repo=$(cd "$here/../.." && pwd)

# The image is named for its Dockerfile, so a changed Dockerfile builds a new one.
tag=$(git hash-object "$here/Dockerfile" | cut -c1-12)
image="iki-videos:$tag"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "Building the videos' render image ($image)."
  docker build --quiet --tag "$image" - <"$here/Dockerfile" >/dev/null
fi

# The checkout is mounted as it is; the packages are installed for Linux into volumes,
# which hide this machine's own node_modules from the container (and keep them intact).
# Files it writes are handed back to the caller's user.
docker run --rm --init \
  --volume "$repo:/repo" \
  --volume iki-videos-node-modules:/repo/node_modules \
  --volume iki-videos-package-node-modules:/repo/media/videos/node_modules \
  --volume iki-videos-pnpm-store:/pnpm-store \
  --workdir /repo/media/videos \
  --env FFMPEG=ffmpeg \
  --env CI="${CI:-}" \
  --env HOST_IDS="$(id -u):$(id -g)" \
  "$image" \
  bash -c '
    set -euo pipefail
    pnpm install --frozen-lockfile --filter @ikisci/videos... --store-dir /pnpm-store \
      --config.confirmModulesPurge=false >/tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
    status=0
    "$@" || status=$?
    chown -R "$HOST_IDS" out 2>/dev/null || true
    exit "$status"
  ' container "$@"

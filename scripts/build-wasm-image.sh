#!/bin/sh
# Print the immutable ID of the server image's pinned linux/amd64 Wasm build.
# The input marker belongs to a plugin and may still name the previous build.
set -eu
cd "$(dirname "$0")/.."
image=$(docker buildx build --platform linux/amd64 --target wasm --file server/Dockerfile --load --quiet .)
platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")
if [ "$platform" != linux/amd64 ]; then
  echo "Expected the linux/amd64 Wasm builder, got $platform." >&2
  exit 1
fi
printf '%s\n' "$image"

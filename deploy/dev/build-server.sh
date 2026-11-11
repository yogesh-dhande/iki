#!/usr/bin/env bash
# Tilt's build of the dev server image (see the Tiltfile): Iki's server image,
# then the dev cluster's plugins on top, as a deployment's own image is built.
#   deploy/dev/build-server.sh IMAGE_REF
set -euo pipefail
cd "$(dirname "$0")/../.."
ref="$1"
docker build -f server/Dockerfile -t iki-server-base:dev .
rm -rf build/dev/server
uv run --frozen iki deploy build deploy/dev --output build/dev/server/plugins --allow-local
docker build -f deploy/plugins.Dockerfile --build-arg IKI_SERVER_IMAGE=iki-server-base:dev \
  -t "$ref" build/dev/server

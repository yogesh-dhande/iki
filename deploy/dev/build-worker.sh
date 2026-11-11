#!/usr/bin/env bash
# Tilt's build of the dev worker image (see the Tiltfile): Iki's worker image,
# then the dev cluster's plugins and their environments on top, as a
# deployment's own worker image is built.
#   deploy/dev/build-worker.sh IMAGE_REF
set -euo pipefail
cd "$(dirname "$0")/../.."
ref="$1"
docker build -f worker/Dockerfile -t iki-worker-base:dev .
rm -rf build/dev/worker build/dev/worker-server
uv run --frozen iki deploy build deploy/dev --output build/dev/worker-server/plugins \
  --workers build/dev/worker --allow-local
docker build -f deploy/worker.Dockerfile --build-arg IKI_WORKER_IMAGE=iki-worker-base:dev \
  -t "$ref" build/dev/worker

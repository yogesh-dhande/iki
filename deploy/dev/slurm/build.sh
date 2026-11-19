#!/usr/bin/env bash
# Builds the test Slurm cluster's image, and an iki-worker for it (static, linux/amd64),
# for server/tests/test_slurm_e2e.py. Prints the variables the tests read.
#   deploy/dev/slurm/build.sh [<directory for the worker binary>]
set -euo pipefail
root="$(cd "$(dirname "$0")/../../.." && pwd)"
out="${1:-$root/.pytest_cache/slurm-e2e}"
image="iki-slurm-e2e:dev"
builder="iki-worker-build:slurm-e2e"

docker build --platform linux/amd64 -t "$image" "$root/deploy/dev/slurm" >&2
# The worker image's build stage: the same static binary as releases.
docker build --platform linux/amd64 -f "$root/worker/Dockerfile" --target build \
  -t "$builder" "$root" >&2

mkdir -p "$out"
container="$(docker create --platform linux/amd64 "$builder")"
trap 'docker rm -f "$container" >/dev/null' EXIT
docker cp "$container:/usr/local/bin/iki-worker" "$out/iki-worker" >&2

echo "IKI_SLURM_E2E_IMAGE=$image"
echo "IKI_SLURM_E2E_WORKER=$out/iki-worker"

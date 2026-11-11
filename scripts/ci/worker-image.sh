#!/usr/bin/env bash
# Builds the iki-worker image, then a deployment's worker image on top of it
# from the test plugins, the way a deployment builds its own, and checks it:
#   scripts/ci/worker-image.sh [BASE_TAG]
set -euo pipefail
cd "$(dirname "$0")/../.."
base="${1:-iki-worker:ci}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

docker build -f worker/Dockerfile -t "$base" .

# A deployment of the test plugins, with their migrations, as a config repository has.
mkdir -p "$work/plugins" "$work/deployment/settings"
cp -R spec/tests/plugins/dna spec/tests/plugins/protein "$work/plugins/"
export NO_COLOR=1
uv run --frozen iki plugin makemigrations "$work/plugins/dna" --no-input >/dev/null
uv run --frozen iki plugin makemigrations "$work/plugins/protein" \
  --with "$work/plugins/dna" --no-input >/dev/null
printf 'ncbi_email: ci@example.org\n' > "$work/deployment/settings/dna.yaml"
cat > "$work/deployment/plugins.lock.yaml" <<EOF
apiVersion: iki/v1
kind: PluginLock
plugins:
  - { name: dna, local: ../plugins/dna, state: active, settings: settings/dna.yaml }
  - { name: protein, local: ../plugins/protein, state: active }
EOF
uv run --frozen iki deploy build "$work/deployment" --output "$work/server" \
  --workers "$work/worker" --allow-local
docker build -f deploy/worker.Dockerfile --build-arg IKI_WORKER_IMAGE="$base" \
  -t iki-worker-deployment:ci "$work/worker"

check() { docker run --rm --entrypoint sh iki-worker-deployment:ci -c "$1"; }
test "$(check 'id -u')" = "10001"
echo "ok: runs as uid 10001"
check 'iki-worker --version' | grep -q '^iki-worker '
echo "ok: $(check 'iki-worker --version')"
check 'micromamba --version' >/dev/null
echo "ok: micromamba for conda environments"
prefix=/opt/iki/envs/protein/protein-tools
check "test \"\$($prefix/bin/python -c 'import sys; print(sys.prefix)')\" = $prefix"
echo "ok: protein.protein-tools is built, with its own Python"
check 'test -f /opt/iki/plugins/plugins.json && test -f /opt/iki/plugins/protein/scripts/properties.py'
echo "ok: the plugins' code and plugins.json"
if check 'touch /opt/iki/plugins/x || touch /opt/iki/envs/x' 2>/dev/null; then
  echo "FAIL: the worker can change plugin code or environments" >&2
  exit 1
fi
echo "ok: plugin code and environments are read-only to the worker"

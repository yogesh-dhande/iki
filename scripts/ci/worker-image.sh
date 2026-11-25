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

cat > "$work/input.json" <<'EOF'
{
  "apiVersion": "iki/v1", "kind": "RunInput",
  "run": {
    "id": "00000000-0000-7000-8000-000000000001", "hrid": "RUN-000001", "attempt": 1,
    "action": "acme/protein.compute_properties",
    "plugin": {"name": "acme/protein", "version": "1.0.0", "sha": "dev"},
    "project": "00000000-0000-7000-8000-000000000002"
  },
  "command": ["python", "./scripts/properties.py"], "environment": "acme/protein.protein-tools",
  "params": {},
  "inputs": {"protein": [{"id": "p1", "hrid": "PRO-000001", "type": "acme/protein.Protein",
                          "schema_version": 1, "revision": 1, "values": {"sequence": "A"}}]}
}
EOF

build_and_run() (
  mask="$1"
  umask "$mask"
  context="$work/$mask"
  # A deployment of the test plugins, with their migrations, as a config repository has.
  mkdir -p "$context/plugins" "$context/deployment/settings"
  cp -R spec/tests/plugins/dna spec/tests/plugins/protein "$context/plugins/"
  export NO_COLOR=1
  uv run --frozen iki plugin makemigrations "$context/plugins/dna" --no-input >/dev/null
  uv run --frozen iki plugin makemigrations "$context/plugins/protein" \
    --with "$context/plugins/dna" --no-input >/dev/null
  printf 'ncbi_email: ci@example.org\n' > "$context/deployment/settings/dna.yaml"
  cat > "$context/deployment/plugins.lock.yaml" <<EOF
apiVersion: iki/v1
kind: PluginLock
plugins:
  - { name: acme/dna, local: ../plugins/dna, state: active, settings: settings/dna.yaml }
  - { name: acme/protein, local: ../plugins/protein, state: active }
EOF
  uv run --frozen iki deploy build "$context/deployment" --output "$context/server" \
    --workers "$context/worker" --allow-local --test-only
  docker build -f deploy/worker.Dockerfile --build-arg IKI_WORKER_IMAGE="$base" \
    -t iki-worker-deployment:ci "$context/worker"

  docker run --rm -i --network none --entrypoint sh iki-worker-deployment:ci -ec '
    test "$(id -u):$(id -g)" = 10001:10001
    iki-worker --version >&2
    micromamba --version >&2
    prefix=/opt/iki/envs/acme__protein/protein-tools
    export PATH="$prefix/bin:$PATH"
    test "$(python -c "import sys; print(sys.prefix)")" = "$prefix"
    plugin=/opt/iki/plugins/acme__protein
    cat /opt/iki/plugins/plugins.json "$plugin/scripts/properties.py" >/dev/null
    bad=$(find /opt/iki/plugins /opt/iki/envs /opt/iki/python \
      \( ! -uid 0 -o ! -gid 0 -o -type d ! -perm 0755 \
         -o -type f ! -perm 0644 ! -perm 0755 \) -print -quit)
    test -z "$bad" || { echo "unexpected ownership or mode: $bad" >&2; exit 1; }
    test "$(stat -c %a "$plugin/scripts/properties.py")" = 644
    test "$(stat -Lc %a "$(command -v python)")" = 755
    cat >/tmp/input.json
    iki-worker run --local /tmp/input.json --plugin "$plugin"
  ' < "$work/input.json" > "$context/output.json"
  jq -e '.updates.protein == [{"entity": "p1", "values": {"molecular_weight": 89.09}}]' \
    "$context/output.json" >/dev/null
  echo "ok: deployment built under umask $mask ran the plugin action as UID 10001"
)

build_and_run 022
build_and_run 077

#!/usr/bin/env bash
# Creates (or reuses) the local kind cluster and installs what Tilt doesn't
# manage: the CloudNativePG operator and generated development secrets.
# Safe to run repeatedly.
set -euo pipefail

CLUSTER=iki
CONTEXT="kind-${CLUSTER}"
NAMESPACE=iki
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# CloudNativePG operator chart, pinned by version and OCI digest. Helm refuses
# to install it if the digest doesn't match.
CNPG_CHART="oci://ghcr.io/cloudnative-pg/charts/cloudnative-pg:0.29.1@sha256:3245fa051bb21d0dd9246272f57a2760b4234817a164a3167d2a1e7fb9f63fd9"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

for tool in docker kind kubectl helm tilt openssl; do
  command -v "$tool" >/dev/null || die "$tool is not installed (see deploy/dev/README.md)"
done
docker info >/dev/null 2>&1 || die "Docker is not running"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "Reusing kind cluster '$CLUSTER'"
else
  log "Creating kind cluster '$CLUSTER'"
  kind create cluster --config "$HERE/kind-config.yaml" --wait 120s
fi
kubectl config use-context "$CONTEXT" >/dev/null
kc() { kubectl --context "$CONTEXT" "$@"; }

log "Installing the CloudNativePG operator"
helm upgrade --install cnpg "$CNPG_CHART" \
  --kube-context "$CONTEXT" \
  --namespace cnpg-system --create-namespace \
  --wait --timeout 5m >/dev/null

kc apply -f "$HERE/manifests/namespace.yaml" >/dev/null

# Random development credentials, created once per cluster and never written
# to the repo. `make dev-creds` prints them.
rand() { openssl rand -base64 "$1" | tr -d '/+=\n' | cut -c1-"$2"; }
ensure_secret() {
  local name="$1"; shift
  if kc -n "$NAMESPACE" get secret "$name" >/dev/null 2>&1; then
    return
  fi
  log "Generating secret $name"
  kc -n "$NAMESPACE" create secret generic "$name" "$@" >/dev/null
}

ensure_secret iki-secret-key --from-literal=secret-key="$(rand 64 64)"
ensure_secret keycloak-dev \
  --from-literal=KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  --from-literal=KC_BOOTSTRAP_ADMIN_PASSWORD="$(rand 24 24)" \
  --from-literal=IKI_DEV_OIDC_CLIENT_SECRET="$(rand 32 32)" \
  --from-literal=IKI_DEV_USER_PASSWORD="$(rand 18 18)"
ensure_secret grafana-dev --from-literal=admin-password="$(rand 24 24)"
if ! kc -n "$NAMESPACE" get secret seaweedfs-s3 >/dev/null 2>&1; then
  access_key="iki$(rand 24 17)"
  secret_key="$(rand 48 40)"
  s3_json=$(printf '{"identities":[{"name":"iki","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}' \
    "$access_key" "$secret_key")
  ensure_secret seaweedfs-s3 \
    --from-literal=access-key="$access_key" \
    --from-literal=secret-key="$secret_key" \
    --from-literal=s3.json="$s3_json"
fi

log "Cluster ready. Next: 'tilt up' (interactive) or 'tilt ci' (headless)."

#!/usr/bin/env bash
# Creates (or reuses) the local kind cluster and installs what Tilt doesn't
# manage: the CloudNativePG operator, the CSI snapshot driver, and generated
# development secrets.
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

# Volume snapshots (the CSI host-path driver), for managed volumes: server-side
# apply, since the CRDs are too big for the last-applied annotation.
log "Installing the CSI host-path driver and snapshot controller"
# The snapshot class needs its CRD established first: apply, wait, apply again.
kc apply --server-side --force-conflicts -f "$HERE/manifests/csi-hostpath.yaml" >/dev/null 2>&1 || true
kc wait --for=condition=Established --timeout=60s \
  crd/volumesnapshotclasses.snapshot.storage.k8s.io \
  crd/volumesnapshots.snapshot.storage.k8s.io \
  crd/volumesnapshotcontents.snapshot.storage.k8s.io >/dev/null
kc apply --server-side --force-conflicts -f "$HERE/manifests/csi-hostpath.yaml" >/dev/null
kc -n kube-system rollout status statefulset/csi-hostpathplugin --timeout=180s >/dev/null
kc -n kube-system rollout status deploy/snapshot-controller --timeout=180s >/dev/null

# The shared file system's and bucket's directories for Iki
# (manifests/shared-filesystem.yaml, manifests/bucket.yaml): on the node,
# writable by runs' user, as a deployment's administrator makes them.
log "Making the shared file system's and bucket's directories for Iki"
for d in iki-shared iki-bucket; do
  docker exec "${CLUSTER}-control-plane" sh -c \
    "mkdir -p /var/local/$d/iki && chown 10001:10001 /var/local/$d/iki && chmod 0750 /var/local/$d/iki"
done

# Keycloak's URL is http://keycloak.iki.localhost:8080 for browsers (which
# resolve *.localhost to loopback, where Tilt forwards the port) and for pods
# (CoreDNS rewrites the name to the Keycloak Service). Same URL, same issuer.
KEYCLOAK_REWRITE="rewrite name exact keycloak.iki.localhost keycloak.iki.svc.cluster.local answer auto"
corefile=$(kc -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}')
if ! grep -qF "$KEYCLOAK_REWRITE" <<<"$corefile"; then
  log "Pointing keycloak.iki.localhost at the in-cluster Keycloak (CoreDNS)"
  patched=$(awk -v rule="    $KEYCLOAK_REWRITE" '{print} /^\.:53 \{/ {print rule}' <<<"$corefile")
  kc -n kube-system create configmap coredns --from-literal=Corefile="$patched" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
  kc -n kube-system rollout restart deploy/coredns >/dev/null
  kc -n kube-system rollout status deploy/coredns --timeout=120s >/dev/null
fi

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
# The app's database role, iki_app: CloudNativePG reads the basic-auth Secret
# to create it; Iki reads the URI. (The schema owner's Secret, iki-db-app,
# comes from CloudNativePG itself.)
if ! kc -n "$NAMESPACE" get secret iki-db-runtime >/dev/null 2>&1; then
  app_password="$(rand 32 32)"
  log "Generating secrets iki-db-runtime-role and iki-db-runtime"
  kc -n "$NAMESPACE" create secret generic iki-db-runtime-role --type=kubernetes.io/basic-auth \
    --from-literal=username=iki_app --from-literal=password="$app_password" >/dev/null
  kc -n "$NAMESPACE" label secret iki-db-runtime-role cnpg.io/reload=true >/dev/null
  kc -n "$NAMESPACE" create secret generic iki-db-runtime \
    --from-literal=uri="postgresql://iki_app:${app_password}@iki-db-rw.${NAMESPACE}:5432/iki" >/dev/null
fi
# Fernet key: URL-safe base64 of 32 random bytes.
ensure_secret iki-encryption-keys --from-literal=keys="$(openssl rand -base64 32 | tr '+/' '-_')"
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

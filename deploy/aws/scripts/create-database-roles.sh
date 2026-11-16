#!/usr/bin/env bash
# Creates Iki's two database roles on RDS (ADR 0025), once, before the chart is first
# installed: `iki`, which owns the schema, and `iki_app`, which the migrate Job grants
# what the app may do. Their passwords are the ones put-secrets.sh put in Secrets
# Manager. Also stores RDS's CA as the iki-database-ca Secret, so Iki verifies the
# database's certificate (sslmode=verify-full).
#
#   deploy/aws/scripts/create-database-roles.sh <deployment> <master-secret-arn>
#
# <master-secret-arn> is deploy/aws/production's database_master_secret_arn output.
# It runs psql in a pod in namespace iki (the database admits only the cluster), as RDS's
# master user, whose password stays in a Secret the pod reads and that's deleted after.
# Safe to run again: it only creates what's missing. Needs aws, kubectl, jq and curl, with
# kubectl pointed at the cluster.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  sed -n '8,9p' "$0" >&2
  exit 2
fi
deployment=$1
master=$2
namespace=iki
region=$(aws configure get region || true)
region=${AWS_REGION:-${region:?Set AWS_REGION}}
pod=iki-create-database-roles

secret() { aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text; }
# postgres://user:password@host:5432/iki: put-secrets.sh's passwords are hex, so this is safe.
password_of() { sed -E 's#^postgres://[^:]+:([0-9a-f]+)@.*#\1#'; }
host_of() { sed -E 's#^postgres://[^@]+@([^:/]+).*#\1#'; }

owner_uri=$(secret "$deployment/database-migrator")
app_uri=$(secret "$deployment/database")
host=$(host_of <<<"$owner_uri")

kubectl get namespace "$namespace" >/dev/null 2>&1 || kubectl create namespace "$namespace"
kubectl label --overwrite namespace "$namespace" pod-security.kubernetes.io/enforce=restricted >/dev/null

# RDS's CA for this region, which signed the database's certificate.
curl -fsSL "https://truststore.pki.rds.amazonaws.com/$region/$region-bundle.pem" |
  kubectl -n "$namespace" create secret generic iki-database-ca --from-file=ca.crt=/dev/stdin \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "Secret iki-database-ca: RDS's CA for $region."

cleanup() {
  kubectl -n "$namespace" delete pod "$pod" --ignore-not-found --wait=false >/dev/null
  kubectl -n "$namespace" delete secret "$pod" --ignore-not-found >/dev/null
}
trap cleanup EXIT

master_json=$(secret "$master")
kubectl -n "$namespace" create secret generic "$pod" \
  --from-literal=PGUSER="$(jq -r .username <<<"$master_json")" \
  --from-literal=PGPASSWORD="$(jq -r .password <<<"$master_json")" \
  --from-literal=OWNER_PASSWORD="$(password_of <<<"$owner_uri")" \
  --from-literal=APP_PASSWORD="$(password_of <<<"$app_uri")" >/dev/null

# Created only if missing; the database handed to its owner, so migrations own the
# schema; pgaudit's extension, for the logging the parameter group turns on.
read -r -d '' sql <<'SQL' || true
\set ON_ERROR_STOP on
\getenv owner_password OWNER_PASSWORD
\getenv app_password APP_PASSWORD
SELECT format('CREATE ROLE iki LOGIN PASSWORD %L', :'owner_password')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'iki') \gexec
SELECT format('CREATE ROLE iki_app LOGIN PASSWORD %L', :'app_password')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'iki_app') \gexec
GRANT iki TO CURRENT_USER;
ALTER DATABASE iki OWNER TO iki;
REVOKE iki FROM CURRENT_USER;
CREATE EXTENSION IF NOT EXISTS pgaudit;
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname IN ('iki', 'iki_app');
SQL

# The pod as PodSecurity "restricted" asks, verifying the database's certificate.
image=public.ecr.aws/docker/library/postgres:17
kubectl -n "$namespace" run "$pod" --restart=Never --quiet --image="$image" \
  --overrides="$(jq -n --arg pod "$pod" --arg image "$image" --arg host "$host" --arg sql "$sql" '{spec: {
    automountServiceAccountToken: false,
    securityContext: {runAsNonRoot: true, runAsUser: 70, seccompProfile: {type: "RuntimeDefault"}},
    volumes: [{name: "ca", secret: {secretName: "iki-database-ca"}}],
    containers: [{
      name: $pod, image: $image,
      command: ["sh", "-c", "printf %s \"$SQL\" | psql -X"],
      envFrom: [{secretRef: {name: $pod}}],
      env: [
        {name: "PGHOST", value: $host}, {name: "PGDATABASE", value: "iki"},
        {name: "PGSSLMODE", value: "verify-full"}, {name: "PGSSLROOTCERT", value: "/ca/ca.crt"},
        {name: "SQL", value: $sql}
      ],
      volumeMounts: [{name: "ca", mountPath: "/ca", readOnly: true}],
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
    }]}}')" >/dev/null

kubectl -n "$namespace" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$pod" --timeout=180s >/dev/null || {
  kubectl -n "$namespace" logs "$pod" >&2 || true
  echo "Creating the roles failed: the log above says why." >&2
  exit 1
}
kubectl -n "$namespace" logs "$pod"
echo "Roles iki and iki_app are ready. Install the chart next."

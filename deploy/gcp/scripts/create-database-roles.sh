#!/usr/bin/env bash
# Creates Iki's two database roles on Cloud SQL (ADR 0025), once, before the chart is
# first installed: `iki`, which owns the schema, and `iki_app`, which the migrate Job
# grants what the app may do. Their passwords are the ones put-secrets.sh put in Secret
# Manager. Also stores Cloud SQL's CA as the iki-database-ca Secret, so Iki verifies the
# database's certificate (sslmode=verify-full, against its DNS name).
#
#   deploy/gcp/scripts/create-database-roles.sh <project> <deployment> <instance>
#
# All three are what deploy/gcp/production's create_database_roles output names. It runs
# psql in a pod in namespace iki (the database has only a private address), as Cloud SQL's
# postgres user, whose password stays in a Secret the pod reads and that's deleted after.
# Safe to run again: it only creates what's missing. Needs gcloud, kubectl and jq, with
# kubectl pointed at the cluster.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  sed -n '8,9p' "$0" >&2
  exit 2
fi
project=$1
deployment=$2
instance=$3
namespace=iki
pod=iki-create-database-roles

secret() { gcloud secrets versions access latest --secret "$1" --project "$project"; }
# postgres://user:password@host:5432/iki: put-secrets.sh's passwords are hex, so this is safe.
password_of() { sed -E 's#^postgres://[^:]+:([0-9a-f]+)@.*#\1#'; }
host_of() { sed -E 's#^postgres://[^@]+@([^:/]+).*#\1#'; }

owner_uri=$(secret "$deployment-database-migrator")
app_uri=$(secret "$deployment-database")
host=$(host_of <<<"$owner_uri")

kubectl get namespace "$namespace" >/dev/null 2>&1 || kubectl create namespace "$namespace"
kubectl label --overwrite namespace "$namespace" pod-security.kubernetes.io/enforce=restricted >/dev/null

# The CAs that sign the instance's certificate (Google's managed CA service): every one
# current, so a rotation in a maintenance window doesn't break connections.
gcloud sql ssl server-ca-certs list --instance "$instance" --project "$project" --format='value(cert)' |
  kubectl -n "$namespace" create secret generic iki-database-ca --from-file=ca.crt=/dev/stdin \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "Secret iki-database-ca: Cloud SQL's CAs for $instance."

cleanup() {
  kubectl -n "$namespace" delete pod "$pod" --ignore-not-found --wait=false >/dev/null
  kubectl -n "$namespace" delete secret "$pod" --ignore-not-found >/dev/null
}
trap cleanup EXIT

kubectl -n "$namespace" create secret generic "$pod" \
  --from-literal=PGUSER=postgres \
  --from-literal=PGPASSWORD="$(secret "$deployment-database-admin")" \
  --from-literal=OWNER_PASSWORD="$(password_of <<<"$owner_uri")" \
  --from-literal=APP_PASSWORD="$(password_of <<<"$app_uri")" >/dev/null

# Created only if missing; the database handed to its owner, so migrations own the
# schema; pgaudit's extension, for the logging the instance's flags turn on. Cloud SQL's
# postgres user isn't a superuser, but may create roles and grant itself theirs.
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

# The pod as PodSecurity "restricted" asks, verifying the database's certificate. Its image
# comes through Google's mirror of Docker Hub, which the VPC reaches privately.
image=mirror.gcr.io/library/postgres:17
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

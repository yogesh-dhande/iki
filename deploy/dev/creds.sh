#!/usr/bin/env bash
# Prints the generated development credentials for the local cluster.
set -euo pipefail
kc() { kubectl --context kind-iki -n iki "$@"; }
get() { kc get secret "$1" -o "jsonpath={.data.$2}" | base64 -d; }

cat <<INFO
Iki            http://localhost:8000
Keycloak       http://keycloak.iki.localhost:8080   admin / $(get keycloak-dev KC_BOOTSTRAP_ADMIN_PASSWORD)
  issuer       http://keycloak.iki.localhost:8080/realms/iki
  realm "iki"  users alice, bob        password $(get keycloak-dev IKI_DEV_USER_PASSWORD)
  client       iki-web                 secret   $(get keycloak-dev IKI_DEV_OIDC_CLIENT_SECRET)
Grafana        http://localhost:3300   admin / $(get grafana-dev admin-password)
S3 (SeaweedFS) http://localhost:8333   bucket iki
  access key   $(get seaweedfs-s3 access-key)
  secret key   $(get seaweedfs-s3 secret-key)
Postgres       migrator (schema owner)  $(get iki-db-app uri 2>/dev/null || echo "(not created yet)")
               app (rows only)          $(get iki-db-runtime uri 2>/dev/null || echo "(not created yet)")
INFO

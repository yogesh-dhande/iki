#!/usr/bin/env bash
# Gives the dev cluster's workers a registration token (see the Tiltfile), the
# way an operator bootstraps a pool: made inside the cluster and stored in the
# Secret the chart reads. Does nothing if the Secret exists; delete it to make
# a new token.
set -euo pipefail
kc() { kubectl --context kind-iki -n iki "$@"; }
if kc get secret iki-worker-registration >/dev/null 2>&1; then
  echo "The workers' registration token is in the iki-worker-registration Secret."
  exit 0
fi
kc exec deploy/iki-web -c web -- iki-server create_registration_token \
    --tier backend --label "Dev backend pool" \
  | kc create secret generic iki-worker-registration --from-file=token=/dev/stdin

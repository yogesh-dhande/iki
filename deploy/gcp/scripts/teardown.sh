#!/usr/bin/env bash
# Tears a deploy/gcp deployment down, all of it: Iki and the cluster's controllers, so the
# load balancer and anything else they made in Google Cloud goes; deploy/gcp/production,
# with deletion protection turned off first; then deploy/gcp/bootstrap's state bucket.
# For a test or a decommissioned deployment: what it deletes can't be restored, except
# Cloud KMS keys' versions, which wait 30 days (key rings and keys can't be deleted at all
# in Google Cloud, only their versions destroyed).
#
#   deploy/gcp/scripts/teardown.sh <project> <deployment> [--bootstrap]
#
# Run it from the repository's root, with deploy/gcp/production initialized
# (tofu init -backend-config=backend.hcl) and its terraform.tfvars in place. --bootstrap
# also destroys deploy/gcp/bootstrap and its state bucket: do that last, once nothing
# else keeps its state there. It asks before it starts; IKI_TEARDOWN_YES=1 doesn't.
# Needs gcloud, tofu, kubectl, helm and jq. Safe to run again after a failure: each step
# skips what's already gone.
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 || (${3:-} != "" && ${3:-} != --bootstrap) ]]; then
  sed -n '9p' "$0" >&2
  exit 2
fi
project=$1
deployment=$2
bootstrap=${3:-}
root=deploy/gcp/production
log() { printf '\n== %s\n' "$*"; }
# Given gcloud an access token in a file (CLOUDSDK_AUTH_ACCESS_TOKEN_FILE), OpenTofu takes
# the same one, read again for each run, so a token refreshed meanwhile is the one used.
tofu() {
  if [[ -n ${CLOUDSDK_AUTH_ACCESS_TOKEN_FILE:-} ]]; then
    GOOGLE_OAUTH_ACCESS_TOKEN=$(<"$CLOUDSDK_AUTH_ACCESS_TOKEN_FILE") command tofu "$@"
  else
    command tofu "$@"
  fi
}
out() { tofu -chdir="$root" output -raw "$1" 2>/dev/null; }

[[ -d $root/.terraform ]] || { echo "Initialize $root first: tofu -chdir=$root init -backend-config=backend.hcl" >&2; exit 1; }
if [[ $(out project_id || true) != "" && $(out project_id) != "$project" ]]; then
  echo "$root's state is for project $(out project_id), not $project." >&2
  exit 1
fi

if [[ ${IKI_TEARDOWN_YES:-} != 1 ]]; then
  read -r -p "Delete deployment $deployment in project $project, and everything in it, for good? Type its name: " answer
  [[ $answer == "$deployment" ]] || { echo "Nothing deleted."; exit 1; }
fi

# --- What the cluster made in Google Cloud ---------------------------------------------------

# The Gateway's load balancer, NEGs and firewall rules are GKE's, not OpenTofu's: they go
# when the Gateway and Services do, while the cluster still runs.
cluster=$(out cluster_name || true)
if [[ -n $cluster ]] && gcloud container clusters describe "$cluster" --location "$(out region)" --project "$project" >/dev/null 2>&1; then
  log "Iki and the cluster's controllers"
  # kubectl as it's set up, if that's this cluster's DNS endpoint; get_credentials otherwise.
  endpoint=$(gcloud container clusters describe "$cluster" --location "$(out region)" --project "$project" \
    --format='value(controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint)')
  if [[ -z $endpoint || $(kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}' 2>/dev/null) != "https://$endpoint" ]]; then
    eval "$(out get_credentials)"
  fi
  kubectl -n iki delete gateway --all --ignore-not-found --wait=true --timeout=10m || true
  kubectl -n iki delete httproute,gcpgatewaypolicy,gcpbackendpolicy,healthcheckpolicy --all --ignore-not-found || true
  helm uninstall iki --namespace iki --wait --timeout 10m 2>/dev/null || true
  helm uninstall external-secrets --namespace external-secrets --wait --timeout 5m 2>/dev/null || true
  # Run pods' namespaces, and anything else Iki's provisioner left.
  for ns in iki iki-runs external-secrets; do
    kubectl delete namespace "$ns" --ignore-not-found --wait=true --timeout=10m || true
  done
  # GKE deletes a Gateway's load balancer in the background; wait for it, so the network
  # can go after.
  for _ in $(seq 1 60); do
    left=$(gcloud compute forwarding-rules list --project "$project" --format='value(name)' \
      --filter="name~^gkegw1-.*-iki-iki-" 2>/dev/null)
    [[ -z $left ]] && break
    sleep 10
  done
fi

# The VM tier's machines, disks and images, named for the deployment (ADR 0109), which a
# run or setup build left behind. Nothing else in the project is named <deployment>-...
log "The VM tier's leftovers"
gcloud compute instances list --project "$project" --filter="name~^$deployment-" --format='value(name,zone.basename())' |
  while read -r name zone; do gcloud compute instances delete "$name" --zone "$zone" --project "$project" --quiet; done
gcloud compute disks list --project "$project" --filter="name~^$deployment-" --format='value(name,zone.basename())' |
  while read -r name zone; do gcloud compute disks delete "$name" --zone "$zone" --project "$project" --quiet; done
gcloud compute images list --project "$project" --no-standard-images --filter="name~^$deployment-" --format='value(name)' |
  while read -r name; do gcloud compute images delete "$name" --project "$project" --quiet; done

# --- deploy/gcp/production -------------------------------------------------------------------

# Deletes every version of every object in a bucket, soft delete off first so none waits
# there (and is billed) for its retention period.
empty_bucket() {
  gcloud storage buckets describe "gs://$1" --project "$project" >/dev/null 2>&1 || return 0
  gcloud storage buckets update "gs://$1" --clear-soft-delete --project "$project" >/dev/null
  if [[ -n $(gcloud storage ls --all-versions "gs://$1/**" --project "$project" 2>/dev/null | head -1) ]]; then
    gcloud storage rm --all-versions --recursive "gs://$1/**" --project "$project" --quiet
  fi
}

# Destroys a root's KMS keys' versions, which OpenTofu can't: each key's rotation stops
# (so no new version is made, and billed), and each version waits 30 days, then goes.
destroy_key_versions() { # <key ring's ID: projects/<p>/locations/<l>/keyRings/<r>>
  gcloud kms keyrings describe "$1" >/dev/null 2>&1 || return 0
  for key in $(gcloud kms keys list --keyring "$1" --format='value(name)'); do
    gcloud kms keys update "$key" --remove-rotation-schedule >/dev/null
    for version in $(gcloud kms keys versions list --key "$key" --format='value(name)' \
      --filter='state=ENABLED OR state=DISABLED'); do
      gcloud kms keys versions destroy "$version" --quiet >/dev/null
    done
    echo "${key##*/keyRings/}: rotation off, every version scheduled for destruction."
  done
}

# Each key ring's ID, from a root's state, kept beside it: OpenTofu forgets them before it
# destroys, and a teardown run again still finds them.
keep_key_rings() { # <root> <address pattern>
  for address in $(tofu -chdir="$1" state list | grep -E "$2"); do
    tofu -chdir="$1" state show -no-color "$address" | sed -nE 's/^ +id += "(.*)"$/\1/p' >>"$1/.teardown-key-rings"
  done
  [[ -f $1/.teardown-key-rings ]] && sort -u -o "$1/.teardown-key-rings" "$1/.teardown-key-rings"
  return 0
}

if [[ -n $(tofu -chdir="$root" state list 2>/dev/null) ]]; then
  log "Deletion protection off: the cluster, the database and the secrets"
  tofu -chdir="$root" apply -input=false -auto-approve -var deletion_protection=false \
    -target=module.cluster.google_container_cluster.this \
    -target=module.database.google_sql_database_instance.this \
    -target=module.database.google_sql_database_instance.dr \
    -target=module.secrets.google_secret_manager_secret.this

  log "The files bucket's objects"
  bucket=$(tofu -chdir="$root" state show -no-color module.storage.google_storage_bucket.files 2>/dev/null |
    sed -nE 's/^ +name += "(.*)"$/\1/p' | head -1)
  [[ -n $bucket ]] && empty_bucket "$bucket"

  # Keys and key rings can't be deleted: OpenTofu forgets them (their versions are
  # destroyed below), and destroys the rest.
  log "deploy/gcp/production"
  keep_key_rings "$root" '^module\.kms\.google_kms_key_ring\.'
  for address in $(tofu -chdir="$root" state list | grep -E '^module\.kms\.google_kms_(crypto_key|key_ring)\.'); do
    tofu -chdir="$root" state rm "$address" >/dev/null
  done
  if ! tofu -chdir="$root" destroy -input=false -auto-approve -var deletion_protection=false; then
    # The peering with Google's managed services is abandoned, not deleted (Google deletes
    # it once Cloud SQL lets go, which can take days): delete it from the VPC's side, so
    # the network can go now, and try again.
    gcloud compute networks peerings delete servicenetworking-googleapis-com \
      --network "$deployment" --project "$project" --quiet 2>/dev/null || true
    tofu -chdir="$root" destroy -input=false -auto-approve -var deletion_protection=false
  fi
fi

# A deleted Cloud SQL instance keeps its backups (retain_backups_on_delete), and its final
# one, which are billed until they expire.
log "The database's retained backups"
for instance in "$deployment" "$deployment-dr"; do
  gcloud sql backups list --project "$project" --filter="instance=$instance" --format='value(name)' 2>/dev/null |
    while read -r backup; do
      gcloud sql backups delete "$backup" --project "$project" --quiet
    done
done

log "deploy/gcp/production's keys"
if [[ -f $root/.teardown-key-rings ]]; then
  while read -r ring; do destroy_key_versions "$ring"; done <"$root/.teardown-key-rings"
fi

# --- deploy/gcp/bootstrap --------------------------------------------------------------------

if [[ $bootstrap == --bootstrap ]]; then
  log "deploy/gcp/bootstrap"
  boot=deploy/gcp/bootstrap
  state_bucket="$deployment-tofu-state-$project"
  # Its state bucket and key are kept from OpenTofu (prevent_destroy): forget them, destroy
  # the rest (the APIs stay on: disable_on_destroy is false), then delete the bucket here.
  if [[ -n $(tofu -chdir="$boot" state list 2>/dev/null) ]]; then
    keep_key_rings "$boot" '^google_kms_key_ring\.state$'
    location=$(sed -nE 's#.*/locations/([^/]+)/.*#\1#p' "$boot/.teardown-key-rings" | head -1)
    for address in google_storage_bucket.state google_kms_crypto_key.state google_kms_key_ring.state; do
      tofu -chdir="$boot" state rm "$address" >/dev/null 2>&1 || true
    done
    tofu -chdir="$boot" destroy -input=false -auto-approve \
      -var project_id="$project" -var name="$deployment" -var location="$location"
  fi
  empty_bucket "$state_bucket"
  gcloud storage buckets delete "gs://$state_bucket" --project "$project" --quiet 2>/dev/null || true
  if [[ -f $boot/.teardown-key-rings ]]; then
    while read -r ring; do destroy_key_versions "$ring"; done <"$boot/.teardown-key-rings"
  fi
fi

log "Done"
echo "Left: Cloud KMS key rings and keys named $deployment*, each version scheduled for destruction"
echo "in 30 days (they can't be deleted); the log bucket, which Cloud Logging deletes after 7 days;"
echo "and custom IAM roles, which IAM deletes after 7 days (their IDs can't be reused for 37)."

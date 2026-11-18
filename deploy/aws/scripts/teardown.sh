#!/usr/bin/env bash
# Tears a deploy/aws deployment down, all of it: Iki and the cluster's controllers, so the
# load balancer and anything else they made in AWS goes; the VM tier's machines, volumes
# and images; deploy/aws/production, with deletion protection turned off first; then
# deploy/aws/bootstrap's state bucket. For a test or a decommissioned deployment: what it
# deletes can't be restored, except what AWS keeps for a while (below).
#
#   deploy/aws/scripts/teardown.sh <deployment> [--bootstrap] [--purge]
#
# Run it from the repository's root, with deploy/aws/production initialized
# (tofu init -backend-config=backend.hcl) and its terraform.tfvars in place, as someone
# who administers the account. --bootstrap also destroys deploy/aws/bootstrap and its
# state bucket: do that last, once nothing else keeps its state there. Without --purge,
# what a deleted deployment can still be recovered from is kept: the database's final
# snapshot and its automated backups (in both regions, until their retention ends), and
# its secrets (for their recovery window). --purge deletes those too. KMS keys always wait
# out their deletion window (30 days): AWS deletes them then. It asks before it starts;
# IKI_TEARDOWN_YES=1 doesn't. Needs aws, tofu, kubectl, helm and jq. Safe to run again
# after a failure: each step skips what's already gone.
set -euo pipefail

usage() { sed -n '8p' "$0" >&2; exit 2; }
[[ $# -ge 1 && $# -le 3 ]] || usage
deployment=$1
shift
bootstrap='' purge=''
for flag in "$@"; do
  case $flag in
    --bootstrap) bootstrap=1 ;;
    --purge) purge=1 ;;
    *) usage ;;
  esac
done
root=deploy/aws/production
log() { printf '\n== %s\n' "$*"; }
# An output of deploy/aws/production's state, or nothing once it's destroyed.
out() { tofu -chdir="$root" output -json 2>/dev/null | jq -er --arg k "$1" '.[$k].value // empty'; }
# A variable as terraform.tfvars sets it, or its default.
var() { echo "var.$1" | tofu -chdir="$root" console 2>/dev/null | tr -d '"'; }

[[ -d $root/.terraform ]] || { echo "Initialize $root first: tofu -chdir=$root init -backend-config=backend.hcl" >&2; exit 1; }
if [[ $(var name) != "$deployment" ]]; then
  echo "$root's variables are for deployment $(var name), not $deployment." >&2
  exit 1
fi
region=$(var region)
dr_region=$(var dr_region)
account=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=$region AWS_PAGER=''

if [[ ${IKI_TEARDOWN_YES:-} != 1 ]]; then
  read -r -p "Delete deployment $deployment in account $account ($region, $dr_region), and everything in it, for good? Type its name: " answer
  [[ $answer == "$deployment" ]] || { echo "Nothing deleted."; exit 1; }
fi

# The load balancers the AWS Load Balancer Controller made for the cluster.
cluster_load_balancers() {
  aws resourcegroupstaggingapi get-resources --resource-type-filters elasticloadbalancing:loadbalancer \
    --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$deployment" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null | tr '\t' '\n' | grep . || true
}

# --- What the cluster made in AWS ------------------------------------------------------------

# The ALB, its target groups and security groups are the AWS Load Balancer Controller's,
# not OpenTofu's: they go when Iki's Ingress does, while the controller still runs.
if aws eks describe-cluster --name "$deployment" >/dev/null 2>&1; then
  log "Iki and the cluster's controllers"
  if [[ $(kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}' 2>/dev/null) != \
    "$(aws eks describe-cluster --name "$deployment" --query cluster.endpoint --output text)" ]]; then
    aws eks update-kubeconfig --name "$deployment" --alias "$deployment" >/dev/null
  fi
  # The ALB refuses deletion while its Ingress says so (deletion_protection.enabled=true):
  # the Ingress says otherwise first, so the controller doesn't turn it back on.
  attributes=$(kubectl -n iki get ingress iki-web -o \
    'jsonpath={.metadata.annotations.alb\.ingress\.kubernetes\.io/load-balancer-attributes}' 2>/dev/null || true)
  if [[ -n $attributes ]]; then
    kubectl -n iki annotate ingress iki-web --overwrite \
      "alb.ingress.kubernetes.io/load-balancer-attributes=${attributes//deletion_protection.enabled=true/deletion_protection.enabled=false}"
  fi
  for arn in $(cluster_load_balancers); do
    aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$arn" \
      --attributes Key=deletion_protection.enabled,Value=false >/dev/null
  done
  kubectl -n iki delete ingress --all --ignore-not-found --wait=true --timeout=10m || true
  helm uninstall iki --namespace iki --wait --timeout 10m 2>/dev/null || true
  # Run pods' namespaces, and anything else Iki's provisioner left, while External
  # Secrets still runs: its ExternalSecrets carry a finalizer only it removes.
  for ns in iki iki-runs; do
    kubectl delete namespace "$ns" --ignore-not-found --wait=true --timeout=10m || true
  done
  helm uninstall external-secrets --namespace external-secrets --wait --timeout 5m 2>/dev/null || true
  kubectl delete namespace external-secrets --ignore-not-found --wait=true --timeout=10m || true
  for _ in $(seq 1 60); do
    [[ -z $(cluster_load_balancers) ]] && break
    sleep 10
  done
  for release in external-dns cluster-autoscaler aws-load-balancer-controller; do
    helm uninstall "$release" --namespace kube-system --wait --timeout 5m 2>/dev/null || true
  done
fi
# What the controller would have deleted had it had the chance.
for arn in $(cluster_load_balancers); do
  aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$arn" \
    --attributes Key=deletion_protection.enabled,Value=false >/dev/null || true
  aws elbv2 delete-load-balancer --load-balancer-arn "$arn" || true
done
for arn in $(aws resourcegroupstaggingapi get-resources --resource-type-filters elasticloadbalancing:targetgroup \
  --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$deployment" --query 'ResourceTagMappingList[].ResourceARN' --output text); do
  aws elbv2 delete-target-group --target-group-arn "$arn" || true
done

# --- The VM tier's machines, volumes and images (ADR 0074) ------------------------------------

log "The VM tier's leftovers"
mine=(--filters "Name=tag:iki.ikisci.org/deployment,Values=$deployment")
machines=$(aws ec2 describe-instances "${mine[@]}" "Name=tag-key,Values=iki.ikisci.org/launch" \
  "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)
if [[ -n $machines ]]; then
  # shellcheck disable=SC2086 # one ID each
  aws ec2 terminate-instances --instance-ids $machines >/dev/null
  # shellcheck disable=SC2086
  aws ec2 wait instance-terminated --instance-ids $machines
fi
for volume in $(aws ec2 describe-volumes "${mine[@]}" "Name=tag-key,Values=iki.ikisci.org/volume" \
  "Name=status,Values=available" --query 'Volumes[].VolumeId' --output text); do
  aws ec2 delete-volume --volume-id "$volume"
done
for image in $(aws ec2 describe-images --owners self "${mine[@]}" --query 'Images[].ImageId' --output text); do
  snapshots=$(aws ec2 describe-images --image-ids "$image" --query 'Images[].BlockDeviceMappings[].Ebs.SnapshotId' --output text)
  aws ec2 deregister-image --image-id "$image"
  for snapshot in $snapshots; do aws ec2 delete-snapshot --snapshot-id "$snapshot"; done
done

# --- deploy/aws/production -------------------------------------------------------------------

# Deletes every version of every object in a bucket, and every delete marker.
empty_bucket() { # <bucket> <region>
  aws s3api head-bucket --bucket "$1" --region "$2" >/dev/null 2>&1 || return 0
  while true; do
    batch=$(aws s3api list-object-versions --bucket "$1" --region "$2" --max-items 500 --output json |
      jq -c '{Objects: [(.Versions // [])[], (.DeleteMarkers // [])[] | {Key, VersionId}], Quiet: true}')
    [[ $(jq '.Objects | length' <<<"$batch") -gt 0 ]] || return 0
    aws s3api delete-objects --bucket "$1" --region "$2" --delete "$batch" >/dev/null
  done
}

# Network interfaces and security groups the cluster left in the VPC, which keep its
# subnets and groups from being deleted.
clear_vpc() {
  local vpc
  vpc=$(out vpc_id || true)
  [[ -n $vpc ]] || return 0
  for eni in $(aws ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$vpc" "Name=status,Values=available" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
    aws ec2 delete-network-interface --network-interface-id "$eni" || true
  done
  for group in $(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" "Name=tag:elbv2.k8s.aws/cluster,Values=$deployment" \
    --query 'SecurityGroups[].GroupId' --output text); do
    aws ec2 delete-security-group --group-id "$group" || true
  done
}

state=$(tofu -chdir="$root" state list 2>/dev/null || true)
if [[ -n $state ]]; then
  if grep -q '^module\.database\.aws_db_instance\.this$' <<<"$state"; then
    log "The database's deletion protection off"
    # Its resource ID first: its retained backups are found by it once it's gone.
    aws rds describe-db-instances --db-instance-identifier "$deployment" \
      --query 'DBInstances[0].DbiResourceId' --output text >"$root/.teardown-database" 2>/dev/null || true
    tofu -chdir="$root" apply -input=false -auto-approve -var deletion_protection=false \
      -target=module.database.aws_db_instance.this
  fi

  log "The buckets' objects"
  for bucket in "$deployment-files-$account:$region" "$deployment-s3-logs-$account:$region" "$deployment-files-dr-$account:$dr_region"; do
    empty_bucket "${bucket%%:*}" "${bucket##*:}"
  done

  # A secret replicated to another region can't be deleted until its replica goes.
  log "The secrets' replicas"
  for secret in $(aws secretsmanager list-secrets --filters "Key=name,Values=$deployment/" \
    --query 'SecretList[?ReplicationStatus].Name' --output text); do
    regions=$(aws secretsmanager describe-secret --secret-id "$secret" --query 'ReplicationStatus[].Region' --output text)
    # shellcheck disable=SC2086 # one region each
    [[ -z $regions ]] || aws secretsmanager remove-regions-from-replication --secret-id "$secret" --remove-replica-regions $regions >/dev/null
  done

  log "deploy/aws/production"
  clear_vpc
  if ! tofu -chdir="$root" destroy -input=false -auto-approve -var deletion_protection=false; then
    # The cluster's network interfaces take minutes to be let go after its nodes are.
    sleep 120
    clear_vpc
    tofu -chdir="$root" destroy -input=false -auto-approve -var deletion_protection=false
  fi
fi

# --- What a deleted deployment can be recovered from ------------------------------------------

snapshot="$deployment-final"
resource=$(cat "$root/.teardown-database" 2>/dev/null || true)
if [[ -n $purge ]]; then
  log "The database's final snapshot and retained backups"
  if aws rds describe-db-snapshots --db-snapshot-identifier "$snapshot" >/dev/null 2>&1; then
    aws rds wait db-snapshot-available --db-snapshot-identifier "$snapshot" || true
    aws rds delete-db-snapshot --db-snapshot-identifier "$snapshot" >/dev/null
  fi
  if [[ -n $resource && $resource != None ]]; then
    aws rds delete-db-instance-automated-backup --dbi-resource-id "$resource" >/dev/null 2>&1 || true
  fi
  for arn in $(aws rds describe-db-instance-automated-backups --region "$dr_region" \
    --query "DBInstanceAutomatedBackups[?DBInstanceIdentifier=='$deployment'].DBInstanceAutomatedBackupsArn" --output text); do
    aws rds delete-db-instance-automated-backup --region "$dr_region" --db-instance-automated-backups-arn "$arn" >/dev/null || true
  done

  log "The secrets, without waiting out their recovery window"
  for secret in $(aws secretsmanager list-secrets --include-planned-deletion --filters "Key=name,Values=$deployment/" \
    --query 'SecretList[].Name' --output text); do
    aws secretsmanager delete-secret --secret-id "$secret" --force-delete-without-recovery >/dev/null
  done

  # Logs written after OpenTofu deleted their group make it again (RDS's, as the
  # database stops), and the run's logs nothing else keeps.
  log "Log groups named for the deployment"
  for where in "$region" "$dr_region"; do
    for prefix in "/aws/rds/instance/$deployment/" "/aws/eks/$deployment/" "/aws/vpc/$deployment/"; do
      for group in $(aws logs describe-log-groups --region "$where" --log-group-name-prefix "$prefix" \
        --query 'logGroups[].logGroupName' --output text); do
        aws logs delete-log-group --region "$where" --log-group-name "$group"
      done
    done
  done
fi

# --- deploy/aws/bootstrap --------------------------------------------------------------------

if [[ -n $bootstrap ]]; then
  log "deploy/aws/bootstrap"
  boot=deploy/aws/bootstrap
  state_bucket="$deployment-tofu-state-$account"
  # Its state bucket is kept from OpenTofu (prevent_destroy): forget it, destroy the rest
  # (its key is scheduled for deletion), then delete the bucket here.
  if [[ -n $(tofu -chdir="$boot" state list 2>/dev/null) ]]; then
    tofu -chdir="$boot" state rm aws_s3_bucket.state >/dev/null 2>&1 || true
    tofu -chdir="$boot" destroy -input=false -auto-approve -var name="$deployment" -var region="$region"
  fi
  empty_bucket "$state_bucket" "$region"
  aws s3api delete-bucket --bucket "$state_bucket" --region "$region" 2>/dev/null || true
fi

log "Done"
echo "Left: KMS keys whose descriptions start $deployment, pending deletion: AWS deletes them in 30 days."
if [[ -z $purge ]]; then
  echo "Kept, to recover from: the database's final snapshot $snapshot and its automated backups"
  echo "(here and in $dr_region, until their retention ends), and the secrets $deployment/*, for"
  echo "their recovery window. Run again with --purge to delete them now."
fi

#!/usr/bin/env bash
# Recovering from a lost region (docs/disaster-recovery.md): copies the lost deployment's
# secrets, from their replicas in this region, into the deployment recovering it, so it
# keeps the same keys. Its database is restored from the lost one's backups, so the
# roles' passwords still hold; the database's URIs are given the new database's host.
# Values go from Secrets Manager to Secrets Manager through files only this user can
# read: never through OpenTofu's state, a terminal, or a command line. A secret that
# already has a value is left alone, so running it again is safe.
#
#   deploy/aws/scripts/recover-secrets.sh <lost-deployment> <deployment> <database-host>
#
# <lost-deployment> is the lost deployment's name (iki-production); <deployment> this
# one's (iki-production-r1); <database-host> its database module's `address` output.
# AWS credentials and the region, the one recovering, come from the environment, as for
# the aws CLI. Needs aws.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  sed -n '10,15p' "$0" >&2
  exit 2
fi
lost=$1
deployment=$2
host=$3
[[ $lost != "$deployment" ]] || { echo "The two deployments must have different names." >&2; exit 2; }

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

exists() { aws secretsmanager describe-secret --secret-id "$1" >/dev/null 2>&1; }

has_value() {
  local count
  # The backticks are JMESPath's literal, not the shell's.
  # shellcheck disable=SC2016
  count=$(aws secretsmanager list-secret-version-ids --secret-id "$1" \
    --query 'length(Versions[?contains(VersionStages, `AWSCURRENT`)])' --output text)
  [[ $count != 0 ]]
}

# postgres://user:password@host:5432/iki, with the host swapped for the new database's.
rehost() { sed -E "s#^(postgres://[^@]+@)[^:/]+(:[0-9]+/)#\1${host}\2#"; }

copied=0
for name in secret-key encryption-keys bundle-signing-key database database-migrator run-secrets email backend-worker-token; do
  from="$lost/$name" to="$deployment/$name" file="$work/$name"
  if ! exists "$from"; then
    echo "$from: none to copy."
    continue
  fi
  if ! exists "$to"; then
    echo "$to doesn't exist: add $name to optional_secrets and apply, then run this again." >&2
    exit 1
  fi
  if has_value "$to"; then
    echo "$to already has a value: left as it is."
    continue
  fi
  aws secretsmanager get-secret-value --secret-id "$from" --query SecretString --output text >"$file.raw"
  # --output text ends the value with a newline the value didn't have.
  printf '%s' "$(cat "$file.raw")" >"$file"
  case $name in
    database | database-migrator) rehost <"$file" >"$file.new" && mv "$file.new" "$file" ;;
  esac
  aws secretsmanager put-secret-value --secret-id "$to" --secret-string "file://$file" >/dev/null
  rm -f "$file" "$file.raw"
  echo "$to: copied from $from."
  copied=$((copied + 1))
done

echo "Done: $copied copied. Next: create-database-roles.sh $deployment <database_master_secret_arn>."

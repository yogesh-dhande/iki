#!/usr/bin/env bash
# Recovering from a lost region (deploy/gcp/production/README.md): copies the lost
# deployment's secrets, from their replicas in this region, into the deployment recovering
# it, so it keeps the same keys. Its database is the lost one's promoted replica, so the
# roles' passwords still hold; the database's URIs are given its host. Values go from
# Secret Manager to Secret Manager through files only this user can read: never through
# OpenTofu's state, a terminal, or a command line. A secret that already has a value is
# left alone, so running it again is safe.
#
#   deploy/gcp/scripts/recover-secrets.sh <project> <lost-deployment> <deployment> <database-host>
#
# <lost-deployment> is the lost deployment's name (iki-production); <deployment> this one's
# (iki-production-r1); <database-host> its database_address output. Two aren't copied, and
# put-secrets.sh, run next, makes them anew: the HMAC key, which is the lost deployment's
# server's (revoke it when fencing that), and the postgres user's password, which it sets.
# Needs gcloud.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  sed -n '10,16p' "$0" >&2
  exit 2
fi
project=$1
lost=$2
deployment=$3
host=$4
[[ $lost != "$deployment" ]] || { echo "The two deployments must have different names." >&2; exit 2; }

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

exists() { gcloud secrets describe "$1" --project "$project" >/dev/null 2>&1; }

has_value() {
  [[ -n $(gcloud secrets versions list "$1" --project "$project" --filter=state=ENABLED \
    --limit=1 --format='value(name)') ]]
}

# postgres://user:password@host:5432/iki, with the host swapped for the new database's.
rehost() { sed -E "s#^(postgres://[^@]+@)[^:/]+(:[0-9]+/)#\1${host}\2#"; }

copied=0
for name in secret-key encryption-keys bundle-signing-key database database-migrator run-secrets email backend-worker-token; do
  from="$lost-$name" to="$deployment-$name" file="$work/$name"
  if ! exists "$from" || ! has_value "$from"; then
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
  # --out-file writes the value as it is, with no newline added.
  gcloud secrets versions access latest --secret "$from" --project "$project" --out-file="$file"
  case $name in
    database | database-migrator) rehost <"$file" >"$file.new" && mv "$file.new" "$file" ;;
  esac
  gcloud secrets versions add "$to" --project "$project" --data-file="$file" >/dev/null
  rm -f "$file"
  echo "$to: copied from $from."
  copied=$((copied + 1))
done

echo "Done: $copied copied. Next: tofu output -raw put_secrets, and run it."

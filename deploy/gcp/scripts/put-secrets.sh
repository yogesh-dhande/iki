#!/usr/bin/env bash
# Generates Iki's secrets and writes them straight to Secret Manager (ADR 0107): they
# never pass through OpenTofu's state, a terminal, or a command line. A secret that
# already has a value is left alone, so running it again is safe.
#
#   deploy/gcp/scripts/put-secrets.sh <project> <deployment> <instance> <database-host>
#
# All four are what deploy/gcp/production's put_secrets output names. Google credentials
# come from gcloud (gcloud auth login). Needs gcloud, curl, jq and openssl.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  sed -n '6,9p' "$0" >&2
  exit 2
fi
project=$1
deployment=$2
instance=$3
host=$4

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Random bytes as base64url: Fernet keys (with padding) and Iki's Ed25519 seed (without)
# are both read this way.
random_b64url() { openssl rand -base64 "$1" | tr -d '\n' | tr '+/' '-_'; }

has_value() {
  [[ -n $(gcloud secrets versions list "$1" --project "$project" --filter=state=ENABLED \
    --limit=1 --format='value(name)') ]]
}

put() {
  local name="$deployment-$1" file="$work/$1"
  cat >"$file"
  # A generator that failed wrote nothing: put no empty version.
  [[ -s $file ]] || { echo "$name: nothing to put, so nothing written." >&2; exit 1; }
  gcloud secrets versions add "$name" --project "$project" --data-file="$file" >/dev/null
  rm -f "$file"
  echo "$name: written."
}

# Each secret, unless it already has a value: generate, then put.
fill() {
  local key=$1
  shift
  if has_value "$deployment-$key"; then
    echo "$deployment-$key already has a value: left as it is."
    return
  fi
  "$@" | put "$key"
}

# Google's APIs, called with curl, read the access token from a file, not the command line.
authorize() { printf 'Authorization: Bearer %s\n' "$(gcloud auth print-access-token)" >"$work/auth"; }

# Django's SECRET_KEY.
secret_key() { random_b64url 64 | tr -d '='; }
# One Fernet key: rotate by prepending another, then iki-server rotate_encryption.
fernet_key() { random_b64url 32; }
# The Ed25519 seed bundles are signed with. Its public key: iki-server bundle_key --public.
signing_seed() { random_b64url 32 | tr -d '='; }
# Iki's two database roles (ADR 0025): the schema's owner and the app's. Create them with
# these passwords before the first install: create-database-roles.sh. The chart adds
# sslmode=verify-full itself.
role_uri() { printf 'postgres://%s:%s@%s:5432/iki' "$1" "$(openssl rand -hex 32)" "$host"; }

fill secret-key secret_key
fill encryption-keys fernet_key
fill bundle-signing-key signing_seed
fill database-migrator role_uri iki
fill database role_uri iki_app
# Plugins' secrets: none yet. Add them as keys such as dna.NCBI_API_KEY.
fill run-secrets printf '{}'

# Cloud SQL's postgres user, which create-database-roles.sh signs in as: its password is
# set here, through the SQL Admin API with the body in a file, then kept.
if has_value "$deployment-database-admin"; then
  echo "$deployment-database-admin already has a value: left as it is."
else
  password=$(openssl rand -hex 32)
  jq -n --arg p "$password" '{name: "postgres", password: $p}' >"$work/user.json"
  authorize
  curl -fsS -X PUT -H @"$work/auth" -H 'Content-Type: application/json' --data @"$work/user.json" \
    "https://sqladmin.googleapis.com/v1/projects/$project/instances/$instance/users?name=postgres" >/dev/null
  rm -f "$work/user.json"
  printf '%s' "$password" | put database-admin
  unset password
fi

echo "Done. The optional secrets (email, backend-worker-token) you put yourself, when you need them:"
echo "  gcloud secrets versions add $deployment-email --project $project --data-file=<file>"

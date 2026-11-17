#!/usr/bin/env bash
# Generates Iki's secrets and writes them straight to Secrets Manager (ADR 0097): they
# never pass through OpenTofu's state, a terminal, or a command line. A secret that
# already has a value is left alone, so running it again is safe.
#
#   deploy/aws/scripts/put-secrets.sh <deployment> <database-host>
#
# <deployment> is the modules' name (iki-production); <database-host> the database
# module's `address` output. AWS credentials and region come from the environment, as
# for the aws CLI. Needs aws and openssl.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  sed -n '6,11p' "$0" >&2
  exit 2
fi
deployment=$1
host=$2

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# 32 random bytes as base64url without padding: Fernet keys (with padding) and Iki's
# Ed25519 seed (without) are both read this way.
random_b64url() { openssl rand -base64 "$1" | tr -d '\n' | tr '+/' '-_'; }

has_value() {
  local count
  # The backticks are JMESPath's literal, not the shell's.
  # shellcheck disable=SC2016
  count=$(aws secretsmanager list-secret-version-ids --secret-id "$1" \
    --query 'length(Versions[?contains(VersionStages, `AWSCURRENT`)])' --output text)
  [[ $count != 0 ]]
}

put() {
  local name="$deployment/$1" file="$work/$1"
  if has_value "$name"; then
    echo "$name already has a value: left as it is."
    return
  fi
  cat > "$file"
  aws secretsmanager put-secret-value --secret-id "$name" --secret-string "file://$file" >/dev/null
  rm -f "$file"
  echo "$name: written."
}

# Django's SECRET_KEY.
random_b64url 64 | tr -d '=' | put secret-key
# One Fernet key: rotate by prepending another, then iki-server rotate_encryption.
random_b64url 32 | put encryption-keys
# The Ed25519 seed bundles are signed with. Its public key: iki-server bundle_key --public.
random_b64url 32 | tr -d '=' | put bundle-signing-key
# Iki's two database roles (ADR 0025): the schema's owner and the app's. Create them
# with these passwords before the first install, as the production runbook shows. The
# chart adds sslmode=verify-full itself.
printf 'postgres://iki:%s@%s:5432/iki' "$(openssl rand -hex 32)" "$host" | put database-migrator
printf 'postgres://iki_app:%s@%s:5432/iki' "$(openssl rand -hex 32)" "$host" | put database
# Plugins' secrets: none yet. Add them as keys such as iki__dna.NCBI_API_KEY (the plugin's label: iki/dna is iki__dna).
printf '{}' | put run-secrets

echo "Done. The optional secrets (email, backend-worker-token) you put yourself, when you need them."

#!/usr/bin/env bash
# Prints the server's /api/v1 OpenAPI document, or /worker/v1's with --worker.
# Settings need their required values to load; printing the schema touches
# neither the database nor the keys, so throwaway values are generated for
# this one command.
set -euo pipefail
cd "$(dirname "$0")/.."
key() { python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }
IKI_SECRET_KEY="openapi-only-$(key)" \
  IKI_ENCRYPTION_KEYS="$(key)" \
  IKI_ALLOWED_HOSTS=localhost \
  IKI_ENVIRONMENT=build \
  IKI_DATABASE_URL=postgres://build@localhost/build \
  uv run --quiet iki-server openapi "$@"

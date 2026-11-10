#!/usr/bin/env bash
# Fails if web/packages/sdk/src/schema.ts isn't what openapi-typescript makes
# from the committed openapi.json. (A server test checks that openapi.json
# matches the API.) Fix with: make client
set -euo pipefail
cd "$(dirname "$0")/../web/packages/sdk"
generated="$(mktemp -t iki-schema.XXXXXX.ts)"
trap 'rm -f "$generated"' EXIT
pnpm exec openapi-typescript openapi.json -o "$generated" --silent >/dev/null
if ! diff -q "$generated" src/schema.ts >/dev/null; then
  echo "web/packages/sdk/src/schema.ts is out of date. Run: make client" >&2
  diff -u src/schema.ts "$generated" | head -40 >&2
  exit 1
fi
echo "The API client is up to date."

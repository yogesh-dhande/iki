#!/usr/bin/env bash
# Compile the deployment's plugins for one of Tilt's two independent image builds.
set -euo pipefail
cd "$(dirname "$0")/../.."
image=$1
shift
config=deploy/dev
case "${IKI_DEV_FIXTURES:-0}" in
  0) ;;
  1)
    config="build/dev/$image-config"
    uv run --frozen python deploy/dev/fixtures/config.py "$config"
    set -- "$@" --test-only
    ;;
  *) echo "IKI_DEV_FIXTURES must be 0 (product) or 1 (CI fixtures)." >&2; exit 1 ;;
esac
exec uv run --frozen iki deploy build "$config" --allow-local "$@"

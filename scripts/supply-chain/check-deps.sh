#!/usr/bin/env bash
# Scans every lockfile for known vulnerabilities (OSV database), for licenses
# outside the allowlist, and for releases less than 3 days old. Used by
# `make check-deps` and CI.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

licenses=$(grep -v '^\s*#' scripts/supply-chain/allowed-licenses.txt | grep -v '^\s*$' | paste -sd, -)
lockfiles=(--lockfile uv.lock --lockfile pnpm-lock.yaml --lockfile worker/Cargo.lock)
# What first-party plugins' environments install.
for requirements in plugins/*/requirements.txt; do
  lockfiles+=(--lockfile "$requirements")
done

echo "==> Known vulnerabilities (OSV)"
osv-scanner scan source --config osv-scanner.toml "${lockfiles[@]}"

echo "==> Licenses (allowlist: scripts/supply-chain/allowed-licenses.txt)"
osv-scanner scan source --config osv-scanner.toml --licenses="$licenses" "${lockfiles[@]}"

echo "==> Rust: advisories, licenses, banned crates and sources (cargo-deny)"
(cd worker && cargo deny --locked check)

echo "==> Releases at least 3 days old (uv.lock, Cargo.lock, plugins' pins; pnpm refuses them itself)"
uv run --no-project --quiet python scripts/supply-chain/release-age.py

#!/usr/bin/env bash
# Scans every lockfile for known vulnerabilities (OSV database) and for
# licenses outside the allowlist. Used by `make check-deps` and CI.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

licenses=$(grep -v '^\s*#' scripts/supply-chain/allowed-licenses.txt | grep -v '^\s*$' | paste -sd, -)
lockfiles=(--lockfile uv.lock --lockfile pnpm-lock.yaml --lockfile worker/Cargo.lock)

echo "==> Known vulnerabilities (OSV)"
osv-scanner scan source --config osv-scanner.toml "${lockfiles[@]}"

echo "==> Licenses (allowlist: scripts/supply-chain/allowed-licenses.txt)"
osv-scanner scan source --config osv-scanner.toml --licenses="$licenses" "${lockfiles[@]}"

echo "==> Rust: advisories, licenses, banned crates and sources (cargo-deny)"
(cd worker && cargo deny --locked check)

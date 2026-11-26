#!/usr/bin/env bash
# Builds the WebAssembly modules in Docker, as the server image does, into iki-spec:
# iki-wasm, which `iki deploy build` checks plugins' rules and guards with (ADR 0127); the
# two worker modules and the standard library report beside them; and the modules
# first-party plugins' actions call, which `iki deploy build` puts in their bundles
# (ADR 0200). The dev cluster's builds run it first, so they need no Rust here.
#   deploy/dev/build-wasm.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
image=$(scripts/build-wasm-image.sh)
# The server's and the worker's builds may run it at once: each writes its own copy,
# then moves it into place.
container="$(docker create --platform linux/amd64 "$image")"
tmp="$(mktemp -d spec/src/iki_spec/iki_wasm.XXXXXX)"
trap 'docker rm "$container" >/dev/null; rm -rf "$tmp"' EXIT
for file in /src/worker/target/wasm32-unknown-unknown/release/iki_wasm.wasm \
  /src/worker/target/wasm32-unknown-unknown/release/iki_previews.wasm \
  /src/worker/oligo-measurement.wasm /src/worker/dna-annotation.wasm \
  /src/worker/rust-standard-library.html /src/plugins/plasmid/data/plasmid-tools.wasm \
  /src/plugins/dna/data/oligo-checks.wasm; do
  name="${file##*/}"
  docker cp "$container:$file" - | tar -xO > "$tmp/$name"
  chmod 644 "$tmp/$name"
done
# Verify the staged canonical bytes before replacing any installed resource.
scripts/verify-server-wasm.sh --portable-only "$tmp"
mkdir -p spec/src/iki_spec/modules
# Browsers alone run the previews' readers: the web app's image build has its own copy.
rm "$tmp/iki_previews.wasm"
mv "$tmp/plasmid-tools.wasm" "$tmp/oligo-checks.wasm" spec/src/iki_spec/modules/
mv "$tmp"/* spec/src/iki_spec/

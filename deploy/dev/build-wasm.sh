#!/usr/bin/env bash
# Builds the modules in Docker, as the server image does. `iki deploy build`
# checks rules with iki-wasm (ADR 0127) and stages the two worker modules.
#   deploy/dev/build-wasm.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
image=$(scripts/build-wasm-image.sh)
# The server's and the worker's builds may run it at once: each writes its own copy,
# then moves it into place.
container="$(docker create --platform linux/amd64 "$image")"
tmp="$(mktemp -d spec/src/iki_spec/iki_wasm.XXXXXX)"
trap 'docker rm "$container" >/dev/null; rm -rf "$tmp"' EXIT
for file in target/wasm32-unknown-unknown/release/iki_wasm.wasm oligo-measurement.wasm dna-annotation.wasm rust-standard-library.html; do
  name="${file##*/}"
  docker cp "$container:/src/worker/$file" - | tar -xO > "$tmp/$name"
  chmod 644 "$tmp/$name"
done
# Verify the staged canonical bytes before replacing any installed resource.
scripts/verify-server-wasm.sh "$tmp"
mv "$tmp"/* spec/src/iki_spec/

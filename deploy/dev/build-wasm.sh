#!/usr/bin/env bash
# Builds the WebAssembly modules in Docker, as the server image does, into iki-spec:
# iki-wasm, which `iki deploy build` checks plugins' rules and guards with (ADR 0127); the
# worker's DNA annotation module and the standard library report beside it; and the modules
# first-party plugins' actions call, which `iki deploy build` puts in their bundles
# (ADR 0200). The dev cluster's builds run it first, so they need no Rust here.
#   deploy/dev/build-wasm.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
docker build -f server/Dockerfile --target wasm -t iki-wasm:dev .
container="$(docker create iki-wasm:dev)"
trap 'docker rm "$container" >/dev/null' EXIT

# The server's and the worker's builds may run it at once: each writes its own copy,
# then moves it into place.
take() {
  local from=$1 to=$2 part
  mkdir -p "$(dirname "$to")"
  part="$(mktemp "$to.XXXXXX")"
  docker cp "$container:$from" - | tar -xO >"$part"
  chmod 644 "$part"
  mv "$part" "$to"
}
take /src/worker/target/wasm32-unknown-unknown/release/iki_wasm.wasm \
  spec/src/iki_spec/iki_wasm.wasm
take /src/worker/dna-annotation.wasm \
  spec/src/iki_spec/dna-annotation.wasm
take /src/worker/rust-standard-library.html \
  spec/src/iki_spec/rust-standard-library.html
take /src/plugins/plasmid/data/plasmid-tools.wasm \
  spec/src/iki_spec/modules/plasmid-tools.wasm

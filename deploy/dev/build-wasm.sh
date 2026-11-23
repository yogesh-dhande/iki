#!/usr/bin/env bash
# Builds the modules in Docker, as the server image does. `iki deploy build`
# checks rules with iki-wasm (ADR 0127) and stages the worker's annotation module.
#   deploy/dev/build-wasm.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
docker build -f server/Dockerfile --target wasm -t iki-wasm:dev .
# The server's and the worker's builds may run it at once: each writes its own copy,
# then moves it into place.
container="$(docker create iki-wasm:dev)"
staged="$(mktemp -d spec/src/iki_spec/iki_wasm.XXXXXX)"
trap 'docker rm "$container" >/dev/null; rm -rf "$staged"' EXIT
for file in target/wasm32-unknown-unknown/release/iki_wasm.wasm dna-annotation.wasm rust-standard-library.html; do
  name="${file##*/}"
  docker cp "$container:/src/worker/$file" - | tar -xO > "$staged/$name"
  chmod 644 "$staged/$name"
done
mv "$staged"/* spec/src/iki_spec/

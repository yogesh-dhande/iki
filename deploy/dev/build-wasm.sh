#!/usr/bin/env bash
# Builds iki-wasm in Docker, as the server image does, into iki-spec, where
# `iki deploy build` checks plugins' rules and guards with it (ADR 0127). The dev
# cluster's builds run it first, so they need no Rust here.
#   deploy/dev/build-wasm.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
docker build -f server/Dockerfile --target wasm -t iki-wasm:dev .
# The server's and the worker's builds may run it at once: each writes its own copy,
# then moves it into place.
container="$(docker create iki-wasm:dev)"
part="$(mktemp spec/src/iki_spec/iki_wasm.XXXXXX)"
trap 'docker rm "$container" >/dev/null; rm -f "$part"' EXIT
docker cp "$container:/src/worker/target/wasm32-unknown-unknown/release/iki_wasm.wasm" - \
  | tar -xO > "$part"
chmod 644 "$part"
mv "$part" spec/src/iki_spec/iki_wasm.wasm

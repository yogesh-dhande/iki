#!/usr/bin/env bash
# Builds iki-wasm in Docker, as the server image does, into iki-spec, where
# `iki deploy build` checks plugins' rules and guards with it (ADR 0127). The dev
# cluster's builds run it first, so they need no Rust here.
#   deploy/dev/build-wasm.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
image=$(scripts/build-wasm-image.sh)
# Verify the exact resource the dev deployment will use, on the release architecture.
printf '%s  /src/worker/oligo-measurement.wasm\n' "$(<plugins/dna/data/oligo-measurement.sha256)" |
  docker run --rm -i --network none --platform linux/amd64 "$image" sha256sum --check --strict -
# The server's and the worker's builds may run it at once: each writes its own copy,
# then moves it into place.
container="$(docker create --platform linux/amd64 "$image")"
tmp="$(mktemp -d spec/src/iki_spec/iki_wasm.XXXXXX)"
trap 'docker rm "$container" >/dev/null; rm -rf "$tmp"' EXIT
for file in target/wasm32-unknown-unknown/release/iki_wasm.wasm oligo-measurement.wasm rust-standard-library.html; do
  name="${file##*/}"
  docker cp "$container:/src/worker/$file" - | tar -xO > "$tmp/$name"
  chmod 644 "$tmp/$name"
done
# Validate every Docker copy before replacing files from a previous successful build.
mv "$tmp"/* spec/src/iki_spec/

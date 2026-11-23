#!/bin/sh
# Builds the DNA record's isolated Rust measurement using worker/rust-toolchain.toml.
# A relative output path is relative to the caller. With --check, compare it with a
# fresh build without changing the committed artifact.
set -eu
check=false
if [ "${1:-}" = --check ]; then
  check=true
  shift
fi
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 [--check] output.wasm" >&2
  exit 2
fi
case "$1" in
  /*) output=$1 ;;
  *) output=$PWD/$1 ;;
esac

root=$(cd "$(dirname "$0")/.." && pwd)
for dir in "${CARGO_HOME:-$HOME/.cargo}/bin" /opt/homebrew/opt/rustup/bin /usr/local/opt/rustup/bin; do
  if [ -x "$dir/rustup" ]; then
    PATH="$dir:$PATH"
    break
  fi
done
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$root/worker"
rustc --crate-name iki_oligo --crate-type cdylib --edition 2024 --target wasm32-unknown-unknown \
  --remap-path-prefix "$root"=. -C opt-level=z -C panic=abort -C lto -C strip=symbols \
  crates/iki-wasm/standalone/oligo.rs -o "$tmp/oligo.wasm"
if "$check"; then
  cmp "$tmp/oligo.wasm" "$output"
else
  cp "$tmp/oligo.wasm" "$output"
  chmod 644 "$output"
fi

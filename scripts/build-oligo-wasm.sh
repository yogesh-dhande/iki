#!/bin/sh
# Builds the DNA record's isolated Rust measurement using worker/rust-toolchain.toml.
# A relative output path is relative to the caller. With --check, compare it with a
# fresh build. --with-notice also emits the standard library notice for redistribution.
set -eu
check=false
with_notice=false
case "${1:-}" in
  --check) check=true; shift ;;
  --with-notice) with_notice=true; shift ;;
esac
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 [--check|--with-notice] output.wasm" >&2
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
if "$check"; then
  tmp=$(mktemp -d)
else
  # Concurrent builds can share the output path. Rename a complete module into place.
  tmp=$(mktemp -d "$(dirname "$output")/.oligo.XXXXXXXX")
fi
trap 'rm -rf "$tmp"' EXIT
cd "$root/worker"
rustc --crate-name iki_oligo --crate-type cdylib --edition 2024 --target wasm32-unknown-unknown \
  --remap-path-prefix "$root"=. -C opt-level=z -C panic=abort -C lto -C strip=symbols \
  crates/iki-wasm/standalone/oligo.rs -o "$tmp/oligo.wasm"
if "$check"; then
  cmp "$tmp/oligo.wasm" "$output"
else
  if "$with_notice"; then
    cp "$(rustc --print sysroot)/share/doc/rust/COPYRIGHT-library.html" "$tmp/rust-standard-library.html"
    chmod 644 "$tmp/rust-standard-library.html"
    mv -f "$tmp/rust-standard-library.html" "$(dirname "$output")/rust-standard-library.html"
  fi
  chmod 644 "$tmp/oligo.wasm"
  mv -f "$tmp/oligo.wasm" "$output"
fi

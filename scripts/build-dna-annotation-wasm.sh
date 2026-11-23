#!/bin/sh
# Build the annotation module carried by iki-spec and staged by the deployment CLI.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
module=dna-annotation.wasm
notice=rust-standard-library.html
digest="$root/plugins/plasmid/data/dna-annotation.wasm.sha256"

build() {
  cd "$root/worker"
  separator=$(printf '\037')
  export CARGO_ENCODED_RUSTFLAGS="--remap-path-prefix=$root=.${separator}--remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo"
  cargo build --locked -p iki-dna-annotation-wasm --target wasm32-unknown-unknown \
    --release --target-dir "$1"
  install -m 0644 "$1/wasm32-unknown-unknown/release/iki_dna_annotation_wasm.wasm" "$2/$module"
  install -m 0644 "$(rustc --print sysroot)/share/doc/rust/COPYRIGHT-library.html" "$2/$notice"
}

check=false
case "${1:-}" in
  # The pinned Docker compiler invokes the same build without mounting the checkout writable.
  --build-here)
    build /tmp/dna-annotation-target "$2"
    exit
    ;;
  --check) check=true; shift ;;
esac
if [ "$#" -gt 1 ] || { [ "$check" = true ] && [ "$#" -ne 0 ]; }; then
  echo "Usage: $0 [output-directory | --check]" >&2
  exit 2
fi

output="${1:-$root/spec/src/iki_spec}"
if "$check"; then
  staged=$(mktemp -d)
else
  mkdir -p "$output"
  output=$(cd "$output" && pwd)
  staged=$(mktemp -d "$output/.dna-annotation.XXXXXXXX")
fi
trap 'rm -rf "$staged"' EXIT
# Cargo's crate metadata includes the compiler host. Use the Linux x86-64 compiler
# on every host so plugin archives, CI and developer checkouts agree byte for byte.
if [ "$(uname -s)/$(uname -m)" = Linux/x86_64 ]; then
  build "$root/worker/target/dna-annotation" "$staged"
else
  rust_image=$(sed -n 's/^ARG RUST_IMAGE=//p' "$root/server/Dockerfile")
  docker run --rm --platform linux/amd64 --mount "type=bind,source=$root,target=/src,readonly" \
    --mount "type=bind,source=$staged,target=/out" \
    "$rust_image" /src/scripts/build-dna-annotation-wasm.sh --build-here /out
fi

if "$check"; then
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$staged/$module")
  else
    actual=$(shasum -a 256 "$staged/$module")
  fi
  actual=${actual%% *}
  expected=$(cat "$digest")
  if [ "$actual" != "$expected" ]; then
    printf '%s SHA-256 mismatch. Expected %s, actual %s\n' "$module" "$expected" "$actual" >&2
    exit 1
  fi
else
  mv -f "$staged/$notice" "$staged/$module" "$output/"
fi

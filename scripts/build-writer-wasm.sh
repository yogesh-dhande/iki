#!/bin/sh
# Build a plugin's writer module natively, for the tests here (ADR 0230).
#
# A writer is a Rust crate in a plugin, writers/<name>/, beside the digest of its module,
# writers/<name>.wasm.sha256 ("<hex>  <name>.wasm"). No .wasm is ever tracked in git. This
# builds the module to writers/<name>.wasm with the toolchain worker/rust-toolchain.toml
# pins, through worker/crates/iki-writer/build-writer.sh, as the plugin image's writers
# stage does.
#
# The digest is of the module built for Linux x86-64. The build is reproducible there,
# but not across host platforms: rustc links its own precompiled wasm32 standard library,
# which differs by host (ADR 0200). So on another platform this builds the module for
# the tests and says the digest can't be compared.
#
#   build-writer-wasm.sh <crate dir>               build it; compare it with the digest on
#                                                  Linux x86-64
#   build-writer-wasm.sh <crate dir> --check       build it to a temporary file and compare
#                                                  only; elsewhere, say so and pass
#   build-writer-wasm.sh <crate dir> --build-only  build it and compare nothing
#
# This never writes the digest. After a change to the Rust that is meant to change the
# module, `uv run iki plugin writers <plugin dir> --update` writes it, building in Docker
# as the plugin image does, so it is the Linux x86-64 one wherever it is run.
set -eu

usage() {
  echo "Usage: $0 <crate dir> [--check|--build-only]" >&2
  exit 2
}

[ $# -ge 1 ] && [ $# -le 2 ] || usage
mode=${2:-}
case "$mode" in
  ""|--check|--build-only) ;;
  *) usage ;;
esac

root=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$1/Cargo.toml" ] || { echo "build-writer-wasm: $1 isn't a crate: it has no Cargo.toml." >&2; exit 1; }
crate=$(cd "$1" && pwd)
name=$(basename "$crate")
output="$crate.wasm"
digest="$crate.wasm.sha256"
builder="$root/worker/crates/iki-writer/build-writer.sh"

sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}

# Whether this machine is the platform the digest is of.
canonical=no
if [ "$(uname -s)" = Linux ] && { [ "$(uname -m)" = x86_64 ] || [ "$(uname -m)" = amd64 ]; }; then
  canonical=yes
fi

for dir in "${CARGO_HOME:-$HOME/.cargo}/bin" /opt/homebrew/opt/rustup/bin /usr/local/opt/rustup/bin; do
  if [ -x "$dir/rustup" ]; then
    PATH="$dir:$PATH"
    break
  fi
done
if ! command -v rustup >/dev/null 2>&1; then
  echo "build-writer-wasm: no rustup. See https://rustup.rs, or 'brew install rustup'." >&2
  exit 1
fi
channel=$(sed -n 's/^channel *= *"\(.*\)"/\1/p' "$root/worker/rust-toolchain.toml")
[ -n "$channel" ] || { echo "build-writer-wasm: no channel in worker/rust-toolchain.toml." >&2; exit 1; }
if ! rustup target list --installed --toolchain "$channel" 2>/dev/null | grep -qx wasm32-unknown-unknown; then
  rustup toolchain install "$channel" --profile minimal --target wasm32-unknown-unknown >&2
fi
CARGO="cargo +$channel"
export CARGO

if [ "$mode" = --check ]; then
  dir=$(mktemp -d "${TMPDIR:-/tmp}/iki-writer-check.XXXXXX")
  trap 'rm -rf "$dir"' EXIT
  trap 'exit 1' HUP INT TERM
  built="$dir/$name.wasm"
else
  built=$output
fi

"$builder" fetch "$crate"
"$builder" build "$crate" "$built"
made=$(sha256 "$built")

if [ "$mode" = --build-only ]; then
  echo "build-writer-wasm: built $output ($made, $(uname -s) $(uname -m))."
  exit 0
fi

if [ ! -f "$digest" ]; then
  echo "build-writer-wasm: $digest is missing. Run uv run iki plugin writers <plugin dir> --update." >&2
  exit 1
fi
want=$(cut -d' ' -f1 <"$digest")

if [ "$canonical" != yes ]; then
  echo "build-writer-wasm: built $name.wasm, $made, on $(uname -s) $(uname -m)."
  echo "The tracked digest is of the Linux x86-64 build, which this can't be compared"
  echo "with; CI and the plugin image check it there."
  exit 0
fi

if [ "$made" != "$want" ]; then
  printf '%s SHA-256 mismatch. Expected %s, actual %s\n' "$name.wasm" "$want" "$made" >&2
  echo "If the Rust changed on purpose, run uv run iki plugin writers <plugin dir> --update and commit the digest." >&2
  exit 1
fi
echo "build-writer-wasm: $name.wasm matches $digest."

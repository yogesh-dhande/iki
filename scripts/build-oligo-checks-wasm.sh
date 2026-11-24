#!/bin/sh
# Compile the DNA plugin's primer checks: the hairpins and dimers an oligo forms,
# how stable they are, and its melting temperature in a reaction's buffer (ADR 0201).
#
# No .wasm is ever tracked in git. What git holds is this script, the Rust it compiles, and
# the module's SHA-256 digest beside where it lands. The module itself is built here, by
# iki-spec's distribution build backend, and by the server image's wasm stage; the
# deployment CLI checks the bytes against the tracked digest before any of them go into a
# worker archive or a worker image.
#
# The digest is of the module built for Linux x86-64, which is what every image, CI job
# and deployment runs. The build is reproducible there -- paths are remapped out of it, so
# the same source gives the same bytes in Docker and on a Linux machine -- but not across
# host platforms: rustc links its own precompiled wasm32 standard library, and the copy
# shipped for macOS arm64 is not byte-identical to the one shipped for Linux x86-64. So a
# developer on another platform builds the module to run the tests, and the digest is
# checked and written where it means something.
#
#   build-oligo-checks-wasm.sh            build it for the tests here; check it against
#                                          the digest when this is Linux x86-64
#   build-oligo-checks-wasm.sh --check    check only; on another platform, say so and
#                                          pass, since there is nothing to compare
#   build-oligo-checks-wasm.sh --update   build it in Docker, as the image does, and
#                                          write the digest -- after a change to the Rust
#                                          that is meant to change the module
#   build-oligo-checks-wasm.sh --build-only
#                                          build and install it, comparing nothing: what
#                                          the image's own wasm stage runs, since --update
#                                          builds that stage and would otherwise be
#                                          checking the digest it is about to write
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
output="$root/plugins/dna/data/oligo-checks.wasm"
digest="$output.sha256"
# iki-spec's wheel carries the built module, because `iki deploy build` exports a plugin
# with `git archive` and so gets the digest but not the bytes (iki_spec.modules).
carried="$root/spec/src/iki_spec/modules/oligo-checks.wasm"
mode=${1:-}
case "$mode" in
  ""|--check|--update|--build-only) ;;
  *) echo "Usage: $0 [--check|--update|--build-only]" >&2; exit 2 ;;
esac

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

# --update asks the canonical builder -- the server image's wasm stage -- so the digest is
# the Linux x86-64 one whoever runs it.
if [ "$mode" = --update ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "build-oligo-checks-wasm: --update builds the module in Docker, as the image" >&2
    echo "does, so the digest is the Linux x86-64 one wherever it is written. Docker" >&2
    echo "isn't here: run this on a Linux x86-64 machine instead, or install Docker." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$output")" "$(dirname "$carried")"
  docker build --platform linux/amd64 -f "$root/server/Dockerfile" --target wasm \
    -t iki-oligo-checks-wasm:update "$root" >&2
  container="$(docker create --platform linux/amd64 iki-oligo-checks-wasm:update)"
  part="$(mktemp "$output.XXXXXX")"
  docker cp "$container:/src/plugins/dna/data/oligo-checks.wasm" - | tar -xO >"$part"
  docker rm "$container" >/dev/null
  chmod 644 "$part"
  made=$(sha256 "$part")
  printf '%s  %s\n' "$made" "$(basename "$output")" >"$digest"
  if [ "$canonical" = yes ]; then
    cp "$part" "$output"
    cp "$part" "$carried"
  fi
  rm -f "$part"
  echo "build-oligo-checks-wasm: $made (Linux x86-64, from the image's wasm stage)"
  if [ "$canonical" != yes ]; then
    echo "build-oligo-checks-wasm: now run $0 to build the module this machine runs."
  fi
  exit 0
fi

for dir in "${CARGO_HOME:-$HOME/.cargo}/bin" /opt/homebrew/opt/rustup/bin /usr/local/opt/rustup/bin; do
  if [ -x "$dir/rustup" ]; then
    PATH="$dir:$PATH"
    break
  fi
done
if ! command -v cargo >/dev/null 2>&1; then
  echo "build-oligo-checks-wasm: no Rust toolchain. See https://rustup.rs," >&2
  echo "or 'brew install rustup'. A worker needs none: it gets the built module." >&2
  exit 1
fi

cd "$root/worker"
# Two builds on the same platform must give the same bytes, so neither the checkout's
# path nor the cargo home may appear in them.
separator=$(printf '\037')
export CARGO_ENCODED_RUSTFLAGS="--remap-path-prefix=$root=.${separator}--remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo"
target="$root/worker/target/oligo-checks"
built="$target/wasm32-unknown-unknown/release/iki_oligo_checks_wasm.wasm"
# Let Cargo check its inputs even when a previous checkout left a module here.
cargo build --locked -p iki-oligo-checks-wasm \
  --target wasm32-unknown-unknown --release --target-dir "$target"
made=$(sha256 "$built")

if [ "$mode" != --check ]; then
  mkdir -p "$(dirname "$output")" "$(dirname "$carried")"
  install -m 0644 "$built" "$output"
  install -m 0644 "$built" "$carried"
fi
if [ "$mode" = --build-only ]; then
  exit 0
fi

if [ ! -f "$digest" ]; then
  echo "build-oligo-checks-wasm: $digest is missing. Run $0 --update." >&2
  exit 1
fi
want=$(cut -d' ' -f1 <"$digest")

if [ "$canonical" != yes ]; then
  echo "build-oligo-checks-wasm: built $made on $(uname -s) $(uname -m)."
  echo "The tracked digest is of the Linux x86-64 build, which this can't be compared"
  echo "with; CI and the images check it there. Run $0 --update if the Rust changed."
  exit 0
fi

if [ "$made" != "$want" ]; then
  printf '%s SHA-256 mismatch. Expected %s, actual %s\n' "$(basename "$output")" "$want" "$made" >&2
  echo "If the Rust changed on purpose, run $0 --update and commit the digest." >&2
  exit 1
fi

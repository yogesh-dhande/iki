#!/bin/sh
# Build a plugin's writer: a Rust crate that depends on iki-writer, compiled to a
# WebAssembly module with no imports (ADR 0230).
#
#   build-writer.sh fetch <crate dir>
#       Download the crate's dependencies, as its Cargo.lock pins them, for
#       wasm32-unknown-unknown. The only step that needs the network.
#   build-writer.sh build <crate dir> <output .wasm>
#       Build the module offline and copy it to <output .wasm>, mode 0644.
#
# The plugin image's writers stage runs this script as it is, with the network off for
# `build` and with the builder image's pinned toolchain; so do local test builds
# (scripts/build-writer-wasm.sh) and `iki plugin writers --update`, which writes a
# writer's digest from the build in that stage.
#
# The SDK. A writer crate depends on `iki-writer = "=0.1.0-dev.0"`. Until the SDK is on
# crates.io, both steps patch it to a copy of the directory this script is in, so the
# crate's Cargo.lock entry for it has no source, and is the same wherever the SDK sits.
#
# Reproducible bytes. Two builds of the same crate with the same toolchain on the same
# platform give the same module wherever the crate, the SDK and the cargo home are. Both
# steps run on a copy of the crate with the SDK inside it (see below), whose path is
# remapped to /writer, and registry and vendored crates' to /crates; the target
# directory is outside the crate ($IKI_WRITER_TARGET_DIR when set, else a temporary one).
# A writer crate therefore builds from its own directory alone.
#
# The digest a plugin tracks is of the Linux x86-64 build: rustc links its own
# precompiled wasm32 standard library, which differs by host (ADR 0200).
#
# Sites without crates.io.
#   - A registry mirror: set IKI_CARGO_MIRROR to its sparse index URL, such as
#     https://cargo.example.org/index/ (a leading sparse+ is fine). Both steps replace
#     crates.io with it; Cargo still checks every crate against Cargo.lock's checksum.
#   - Vendored crates: `cargo vendor --versioned-dirs` into the crate's vendor/ directory
#     and commit it (--versioned-dirs, so the module is the one crates.io's give), with
#     the crate's .cargo/config.toml saying
#         [source.crates-io]
#         replace-with = "vendored-sources"
#         [source.vendored-sources]
#         directory = "vendor"
#     Cargo runs in (a copy of) the crate's directory, so it reads that file; nothing
#     else is needed.
#
# Environment:
#   CARGO                  the cargo to run (default: cargo on PATH); may carry a
#                          toolchain, as in CARGO="cargo +1.93.0"
#   IKI_WRITER_TARGET_DIR  the target directory (default: a temporary one, removed after)
#   IKI_CARGO_MIRROR       a sparse registry to use instead of crates.io
#   CARGO_HOME             where Cargo keeps what it fetched (default: ~/.cargo)
set -eu

usage() {
  echo "Usage: $0 fetch <crate dir>" >&2
  echo "       $0 build <crate dir> <output .wasm>" >&2
  exit 2
}

[ $# -ge 2 ] || usage
step=$1
case "$step" in
  fetch) [ $# -eq 2 ] || usage ;;
  build) [ $# -eq 3 ] || usage ;;
  *) usage ;;
esac

fail() {
  echo "build-writer: $*" >&2
  exit 1
}

[ -f "$2/Cargo.toml" ] || fail "$2 isn't a crate: it has no Cargo.toml."
[ -f "$2/Cargo.lock" ] || fail "$2 has no Cargo.lock. A writer's dependencies are pinned by it: run cargo generate-lockfile there, with the patch this script passes, and commit it."
crate=$(cd "$2" && pwd -P)
if [ "$step" = build ]; then
  case "$3" in
    /*) output=$3 ;;
    *) output="$(pwd)/$3" ;;
  esac
fi
sdk=$(cd "$(dirname "$0")" && pwd -P)
cargo_home=${CARGO_HOME:-$HOME/.cargo}

# The paths go into TOML strings and into rustc's flags.
for path in "$crate" "$sdk" "$cargo_home" "${TMPDIR:-/tmp}"; do
  case "$path" in
    *'"'* | *\\* | *"$(printf '\037')"*)
      fail "$path has a character the build can't pass on (\", \\ or the unit separator)." ;;
  esac
done

# Writers build with the builder's toolchain alone; a toolchain file would ask rustup for
# another, which an offline build can't fetch, and whose module wouldn't match.
for file in rust-toolchain rust-toolchain.toml; do
  [ ! -e "$crate/$file" ] || fail "$crate/$file asks for its own toolchain. Writers build with Iki's pinned one: remove it."
done

# The build runs on a copy: the crate, without its target/, with the SDK inside it at
# .iki-writer/. Cargo hashes a path dependency's location into the build -- relative to
# the workspace when it is inside it, absolute when not -- so the SDK must sit at the same
# place in the crate's workspace for every build, wherever this script is. The crate's
# .cargo/config.toml and vendor/ come along. So a writer builds from its own directory
# alone: nothing in it may reach outside (a path dependency, an include_bytes! of a
# plugin file, a build script reading ../), as nothing outside is there.
stage=$(mktemp -d "${TMPDIR:-/tmp}/iki-writer.XXXXXX")
stage=$(cd "$stage" && pwd -P)
trap 'rm -rf "$stage"' EXIT
trap 'exit 1' HUP INT TERM
work="$stage/writer"
mkdir "$work"
for entry in "$crate"/* "$crate"/.[!.]* "$crate"/..?*; do
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  [ "$(basename "$entry")" != target ] || continue
  cp -R "$entry" "$work/"
done
[ ! -e "$work/.iki-writer" ] || fail "$crate has a .iki-writer, where the build puts the SDK: rename it."
mkdir "$work/.iki-writer"
cp -R "$sdk/Cargo.toml" "$sdk/README.md" "$sdk/src" "$work/.iki-writer/"

set -- --config 'patch.crates-io.iki-writer.path=".iki-writer"'
if [ -n "${IKI_CARGO_MIRROR:-}" ]; then
  case "$IKI_CARGO_MIRROR" in
    sparse+*) mirror=$IKI_CARGO_MIRROR ;;
    *) mirror="sparse+$IKI_CARGO_MIRROR" ;;
  esac
  case "$mirror" in
    *'"'* | *\\*) fail "IKI_CARGO_MIRROR has a \" or a \\ in it." ;;
  esac
  set -- "$@" --config 'source.crates-io.replace-with="iki-mirror"' \
    --config "source.iki-mirror.registry=\"$mirror\""
fi

cd "$work"
# CARGO may be a command with arguments, such as "cargo +1.93.0": split it on purpose.
# shellcheck disable=SC2086
if [ "$step" = fetch ]; then
  ${CARGO:-cargo} fetch --locked --target wasm32-unknown-unknown "$@"
  exit 0
fi

if [ -n "${IKI_WRITER_TARGET_DIR:-}" ]; then
  target=$IKI_WRITER_TARGET_DIR
  mkdir -p "$target"
else
  target="$stage/target"
fi

# Paths in the module (panic locations) must not say where anything was built. The crate
# is /writer, and the SDK in it /writer/.iki-writer. Registry crates are /crates/<name>-
# <version> whichever registry they came from: Cargo unpacks each registry's crates into
# a directory named after it, so a mirror's would otherwise differ from crates.io's. So
# are vendored ones, when `cargo vendor --versioned-dirs` named them that way. rustc
# applies the last remap that matches, so the narrower ones come later.
# Unpack every crate first, so each registry's directory is there to be remapped.
# shellcheck disable=SC2086
${CARGO:-cargo} metadata --locked --offline --format-version 1 \
  --filter-platform wasm32-unknown-unknown "$@" >/dev/null
separator=$(printf '\037')
flags="--remap-path-prefix=$work=/writer${separator}--remap-path-prefix=$cargo_home=/cargo"
for registry in "$cargo_home"/registry/src/*; do
  [ -d "$registry" ] || continue
  case "$registry" in
    *'"'* | *\\* | *"$separator"*) fail "$registry has a character the build can't pass on." ;;
  esac
  flags="$flags${separator}--remap-path-prefix=$registry=/crates"
done
CARGO_ENCODED_RUSTFLAGS="$flags${separator}--remap-path-prefix=$work/vendor=/crates"
export CARGO_ENCODED_RUSTFLAGS
# shellcheck disable=SC2086
${CARGO:-cargo} build --locked --offline --release --target wasm32-unknown-unknown \
  --target-dir "$target" "$@"

# The crate's own cdylib is in release/; its dependencies' artifacts are in deps/.
set -- "$target"/wasm32-unknown-unknown/release/*.wasm
if [ ! -f "$1" ]; then
  fail "$crate built no module. Its Cargo.toml needs [lib] crate-type = [\"cdylib\"]."
fi
if [ $# -ne 1 ]; then
  fail "$crate built $# modules ($*); a writer crate builds exactly one."
fi
mkdir -p "$(dirname "$output")"
part=$(mktemp "$output.XXXXXX")
cp "$1" "$part"
chmod 0644 "$part"
mv "$part" "$output"

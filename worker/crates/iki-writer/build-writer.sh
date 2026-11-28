#!/bin/sh
# Build a plugin's writer: a Rust crate that depends on iki-writer, compiled to a
# WebAssembly module with no imports (ADR 0230).
#
#   build-writer.sh fetch <crate dir>
#       Gather the crate's dependencies as its Cargo.lock pins them, each checked against
#       its checksum there, into a directory of their own in the cargo home. The only
#       step of a build that needs the network.
#   build-writer.sh build <crate dir> <output .wasm>
#       Build the module offline from what fetch gathered, and copy it to <output .wasm>,
#       mode 0644.
#   build-writer.sh test <crate dir> [cargo test arguments]
#       Run the crate's unit tests natively, offline, from what fetch gathered.
#   build-writer.sh lock <crate dir>
#       Pin the crate's iki-writer to the one beside this script, in its Cargo.lock with
#       its checksum, keeping the crate's other pins.
#   build-writer.sh vendor <crate dir>
#       Gather the crate's dependencies into its vendor/ instead, for a site that builds
#       offline (see below).
#   build-writer.sh package <dir>
#       Package the iki-writer beside this script as crates.io has it, into <dir>, and
#       print its SHA-256.
#   build-writer.sh publish <dir>
#       Iki's release workflow's: package it, check it's the checksum committed beside
#       it, and publish that package to crates.io (CARGO_REGISTRY_TOKEN).
#
# The plugin image's writers stage runs this script as it is, with the network off for
# `build` and with the writer builder image's toolchain; so do local test builds
# (scripts/build-writer-wasm.sh) and `iki plugin writers --update`, which writes a
# writer's digest from the build in that stage.
#
# The SDK. A writer crate depends on `iki-writer = "=<version>"`, from crates.io, and its
# Cargo.lock pins that version with its checksum. Iki publishes iki-writer with each
# release, so a writer builds against the SDK it pins, not the one of the Iki that builds
# it, and an Iki release doesn't change its module. This script carries the iki-writer of
# its own Iki, beside it: when a crate pins that version, with that checksum, the build
# takes it from here instead of the registry. It's the same crate either way: `package`
# makes it as `cargo publish` does, in the same toolchain, and the checksum it must have
# is committed beside it (iki-writer.crate.sha256), so a writer pinned to an Iki that
# isn't released yet, or a site that can't reach crates.io, gets the bytes crates.io has
# or will have. A crate pinned to any other version gets it from the registry.
#
# Reproducible bytes. Two builds of the same crate with the same toolchain on the same
# platform give the same module wherever the crate and the cargo home are. Every crate
# the build reads is gathered into one directory (Cargo's directory source, as
# `cargo vendor --versioned-dirs` lays it out) that replaces crates.io; Cargo still
# checks each against Cargo.lock, and builds it as crates.io's. The build runs on a copy
# of the crate whose path is remapped to /writer, and the gathered crates' to /crates;
# the target directory is outside the crate ($IKI_WRITER_TARGET_DIR when set, else a
# temporary one). A writer crate therefore builds from its own directory alone.
#
# The digest a plugin tracks is of the Linux x86-64 build: rustc links its own
# precompiled wasm32 standard library, which differs by host (ADR 0200).
#
# Sites without crates.io.
#   - A registry mirror: set IKI_CARGO_MIRROR to its sparse index URL, such as
#     https://cargo.example.org/index/ (a leading sparse+ is fine). fetch, lock and
#     vendor read crates from it instead of crates.io; Cargo still checks every crate
#     against Cargo.lock's checksum.
#   - Vendored crates: `build-writer.sh vendor <crate dir>` into the crate's vendor/
#     directory, commit it, with the crate's .cargo/config.toml saying
#         [source.crates-io]
#         replace-with = "vendored-sources"
#         [source.vendored-sources]
#         directory = "vendor"
#     A crate that replaces crates.io itself builds from its own sources, and fetch
#     gathers nothing for it.
#
# Environment:
#   CARGO                  the cargo to run (default: cargo on PATH); may carry a
#                          toolchain, as in CARGO="cargo +1.93.0"
#   IKI_WRITER_TARGET_DIR  the target directory (default: a temporary one, removed after)
#   IKI_CARGO_MIRROR       a sparse registry to use instead of crates.io
#   CARGO_HOME             where Cargo keeps what it fetched, and fetch what it gathered
#                          (default: ~/.cargo)
set -eu

usage() {
  echo "Usage: $0 fetch <crate dir>" >&2
  echo "       $0 build <crate dir> <output .wasm>" >&2
  echo "       $0 test <crate dir> [cargo test arguments]" >&2
  echo "       $0 lock <crate dir>" >&2
  echo "       $0 vendor <crate dir>" >&2
  echo "       $0 package <dir>" >&2
  echo "       $0 publish <dir>" >&2
  exit 2
}

[ $# -ge 2 ] || usage
step=$1
case "$step" in
  fetch | lock | vendor | package | publish) [ $# -eq 2 ] || usage ;;
  build) [ $# -eq 3 ] || usage ;;
  test) ;;
  *) usage ;;
esac

fail() {
  echo "build-writer: $*" >&2
  exit 1
}

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

sdk=$(cd "$(dirname "$0")" && pwd -P)
cargo_home=${CARGO_HOME:-$HOME/.cargo}
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$sdk/Cargo.toml" | head -n 1)
[ -n "$version" ] || fail "$sdk/Cargo.toml has no version."

# The paths go into TOML strings and into rustc's flags.
separator=$(printf '\037')
checked() {
  case "$1" in
    *'"'* | *\\* | *"$separator"*)
      fail "$1 has a character the build can't pass on (\", \\ or the unit separator)." ;;
  esac
}
for path in "$sdk" "$cargo_home" "${TMPDIR:-/tmp}"; do
  checked "$path"
done

stage=$(mktemp -d "${TMPDIR:-/tmp}/iki-writer.XXXXXX")
stage=$(cd "$stage" && pwd -P)
trap 'rm -rf "$stage"' EXIT
trap 'exit 1' HUP INT TERM

mirror=
if [ -n "${IKI_CARGO_MIRROR:-}" ]; then
  case "$IKI_CARGO_MIRROR" in
    sparse+*) mirror=$IKI_CARGO_MIRROR ;;
    *) mirror="sparse+$IKI_CARGO_MIRROR" ;;
  esac
  case "$mirror" in
    *'"'* | *\\*) fail "IKI_CARGO_MIRROR has a \" or a \\ in it." ;;
  esac
fi

# cargo, with the mirror in place of crates.io when there is one. CARGO may be a command
# with arguments, such as "cargo +1.93.0": split it on purpose.
registry_cargo() {
  if [ -n "$mirror" ]; then
    # shellcheck disable=SC2086
    ${CARGO:-cargo} "$@" --config 'source.crates-io.replace-with="iki-mirror"' \
      --config "source.iki-mirror.registry=\"$mirror\""
  else
    # shellcheck disable=SC2086
    ${CARGO:-cargo} "$@"
  fi
}

# This iki-writer, packaged as `cargo publish` packages it: from a copy outside any git
# repository (so it carries no .cargo_vcs_info.json), with its own Cargo.lock. Writes
# the .crate into $1; prints its path.
package_sdk() {
  copy="$stage/package/iki-writer"
  rm -rf "$stage/package"
  mkdir -p "$copy"
  cp -R "$sdk/Cargo.toml" "$sdk/Cargo.lock" "$sdk/README.md" "$sdk/src" "$copy/"
  (cd "$copy" && registry_cargo package --locked --no-verify --quiet \
    --target-dir "$stage/package/target") >&2
  made="$stage/package/target/package/iki-writer-$version.crate"
  [ -f "$made" ] || fail "cargo package made no iki-writer-$version.crate."
  mkdir -p "$1"
  cp "$made" "$1/"
  echo "$1/iki-writer-$version.crate"
}

# This iki-writer's .crate, checked against the checksum committed beside it; prints
# its checksum.
pinned_sdk() {
  made=$(package_sdk "$stage/sdk")
  sum=$(sha256 "$made")
  want=$(cut -d' ' -f1 <"$sdk/iki-writer.crate.sha256")
  if [ "$sum" != "$want" ]; then
    fail "iki-writer $version packages to $sum, not the $want its iki-writer.crate.sha256 pins. If its source changed on purpose, run scripts/pin-iki-writer.sh in Iki's checkout; otherwise this toolchain ($(${CARGO:-cargo} --version)) isn't the writer builder's."
  fi
  echo "$sum"
}

# A .crate unpacked as a directory source holds it: <dir>/<name>-<version>, with
# .cargo-checksum.json listing each file's SHA-256 and the .crate's.
unpack() { # <.crate> <checksum> <dir>
  tar -xzf "$1" -C "$3"
  unpacked="$3/iki-writer-$version"
  (
    cd "$unpacked"
    find . -type f | sed 's|^\./||' | LC_ALL=C sort >"$stage/files"
    printf '{"files":{'
    first=yes
    while IFS= read -r file; do
      checked "$file"
      [ "$first" = yes ] || printf ','
      first=no
      printf '"%s":"%s"' "$file" "$(sha256 "$file")"
    done <"$stage/files"
    printf '},"package":"%s"}' "$2"
  ) >"$stage/checksums"
  mv "$stage/checksums" "$unpacked/.cargo-checksum.json"
  echo "$unpacked"
}

# What a crate's Cargo.lock pins of iki-writer: "<version> <source> <checksum>".
sdk_pin() {
  awk '
    function emit() { if (name == "iki-writer") { print version " " source " " checksum; done = 1 } }
    /^\[\[package\]\]/ { emit(); if (done) exit; name = version = source = checksum = ""; next }
    /^name = / { gsub(/"/, "", $3); name = $3 }
    /^version = / { gsub(/"/, "", $3); version = $3 }
    /^source = / { gsub(/"/, "", $3); source = $3 }
    /^checksum = / { gsub(/"/, "", $3); checksum = $3 }
    END { if (!done) emit() }
  ' "$1"
}

[ "$step" != package ] || {
  made=$(package_sdk "$2")
  echo "$(sha256 "$made")  $(basename "$made")"
  exit 0
}

# Publishing packages again, from the same copy: Cargo packages it the same way, and
# the checksum is checked first, so what crates.io gets is the crate Iki's writers pin.
[ "$step" != publish ] || {
  sum=$(pinned_sdk)
  cp "$stage/sdk/iki-writer-$version.crate" "$2/"
  (cd "$stage/package/iki-writer" && registry_cargo publish --locked --no-verify \
    --target-dir "$stage/package/target")
  echo "Published iki-writer $version ($sum)."
  exit 0
}

[ -f "$2/Cargo.toml" ] || fail "$2 isn't a crate: it has no Cargo.toml."
crate=$(cd "$2" && pwd -P)
checked "$crate"
if [ "$step" != lock ]; then
  [ -f "$crate/Cargo.lock" ] || fail "$crate has no Cargo.lock. A writer's dependencies are pinned by it: run build-writer.sh lock $crate, and commit it."
fi
if [ "$step" = build ]; then
  case "$3" in
    /*) output=$3 ;;
    *) output="$(pwd)/$3" ;;
  esac
fi

# Writers build with the builder's toolchain alone; a toolchain file would ask rustup for
# another, which an offline build can't fetch, and whose module wouldn't match.
for file in rust-toolchain rust-toolchain.toml; do
  [ ! -e "$crate/$file" ] || fail "$crate/$file asks for its own toolchain. Writers build with the writer builder's: remove it."
done

# Each step runs on a copy: the crate, without its target/. Its .cargo/config.toml and
# vendor/ come along. Nothing in it may reach outside (a path dependency, an
# include_bytes! of a plugin file, a build script reading ../), as nothing outside is
# there.
work="$stage/writer"
mkdir "$work"
for entry in "$crate"/* "$crate"/.[!.]* "$crate"/..?*; do
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  [ "$(basename "$entry")" != target ] || continue
  cp -R "$entry" "$work/"
done

# A crate that replaces crates.io itself (its vendor/) builds from its own sources.
own_sources=no
for file in "$work/.cargo/config.toml" "$work/.cargo/config"; do
  if [ -f "$file" ] && grep -Eq '^\[source\.crates-io\]|^[[:space:]]*source\.crates-io\.' "$file"; then
    own_sources=yes
  fi
done

# Where fetch gathers the crates its Cargo.lock pins: named by the lock, which holds
# every crate's checksum, so it's the same crates whenever it's the same name.
if [ -f "$crate/Cargo.lock" ]; then
  gathered="$cargo_home/iki-writer/crates-$(sha256 "$crate/Cargo.lock")"
  checked "$gathered"
fi

# cargo vendor of the copy's Cargo.lock into $1, its crates in versioned directories, from
# the registry (or the mirror). With $2, iki-writer comes from that directory instead,
# and the lock cargo vendor rewrites to say so is the copy's, put back after.
vendor_into() { # <dir> [<unpacked iki-writer>]
  set -- "$1" "${2:-}" --versioned-dirs --no-delete --quiet
  [ -z "$mirror" ] || set -- "$@" --respect-source-config
  if [ -n "$2" ]; then
    set -- "$@" --config "patch.crates-io.iki-writer.path=\"$2\""
  else
    set -- "$@" --locked
  fi
  into=$1
  shift 2
  registry_cargo vendor "$@" "$into" >/dev/null
}

# Gathers the crates the copy's Cargo.lock pins into $1, each checked against its
# checksum there: this iki-writer among them when the lock pins it.
gather() {
  into=$1
  rm -rf "$into"
  mkdir -p "$into"
  pin=$(sdk_pin "$work/Cargo.lock")
  pinned_version=${pin%% *}
  if [ "$pinned_version" != "$version" ]; then
    vendor_into "$into"
    return
  fi
  rest=${pin#* }
  pinned_source=${rest%% *}
  pinned_sum=${rest#* }
  [ "$pinned_source" = "registry+https://github.com/rust-lang/crates.io-index" ] && [ -n "$pinned_sum" ] ||
    fail "$crate's Cargo.lock pins iki-writer $version without crates.io's checksum (a path patch?): run build-writer.sh lock $crate, and commit it."
  sum=$(pinned_sdk)
  [ "$pinned_sum" = "$sum" ] ||
    fail "$crate's Cargo.lock pins iki-writer $version with the checksum $pinned_sum, but iki-writer $version is $sum. Run build-writer.sh lock $crate, and commit it."
  unpacked=$(unpack "$stage/sdk/iki-writer-$version.crate" "$sum" "$into")
  vendor_into "$into" "$unpacked"
  cp "$crate/Cargo.lock" "$work/Cargo.lock"
}

# Whether fetch gathered the crate's crates (or it has its own sources).
gathered_or_own() {
  [ "$own_sources" = yes ] || [ -d "$gathered" ] ||
    fail "$crate's crates haven't been gathered: run build-writer.sh fetch $crate first."
}

cd "$work"
case "$step" in
  fetch)
    if [ "$own_sources" = yes ]; then
      # shellcheck disable=SC2086
      ${CARGO:-cargo} fetch --locked
      exit 0
    fi
    gather "$stage/gathered"
    mkdir -p "$(dirname "$gathered")"
    rm -rf "$gathered"
    mv "$stage/gathered" "$gathered"
    # Every pin resolves from what was gathered, offline.
    # shellcheck disable=SC2086
    ${CARGO:-cargo} metadata --locked --offline --format-version 1 \
      --config 'source.crates-io.replace-with="iki-writer-crates"' \
      --config "source.iki-writer-crates.directory=\"$gathered\"" >/dev/null
    exit 0
    ;;
  vendor)
    [ ! -e "$crate/vendor" ] || fail "$crate/vendor is there already: remove it to gather it again."
    gather "$stage/vendor"
    mv "$stage/vendor" "$crate/vendor"
    echo "Gathered $crate/vendor. The crate's .cargo/config.toml builds from it with:"
    echo
    echo '[source.crates-io]'
    echo 'replace-with = "vendored-sources"'
    echo
    echo '[source.vendored-sources]'
    echo 'directory = "vendor"'
    exit 0
    ;;
  lock)
    sum=$(pinned_sdk)
    mkdir "$stage/gathered"
    unpacked=$(unpack "$stage/sdk/iki-writer-$version.crate" "$sum" "$stage/gathered")
    if [ ! -f Cargo.lock ]; then
      registry_cargo generate-lockfile --quiet --config "patch.crates-io.iki-writer.path=\"$unpacked\""
    fi
    # The crate's other pins stay as they are. Its iki-writer becomes this one, from
    # crates.io with its checksum: Cargo writes it so when this one is the only
    # iki-writer the source replacing crates.io has.
    vendor_into "$stage/gathered" "$unpacked"
    # shellcheck disable=SC2086
    ${CARGO:-cargo} metadata --offline --format-version 1 --quiet \
      --config 'source.crates-io.replace-with="iki-writer-crates"' \
      --config "source.iki-writer-crates.directory=\"$stage/gathered\"" >/dev/null
    [ "$(sdk_pin Cargo.lock)" = "$version registry+https://github.com/rust-lang/crates.io-index $sum" ] ||
      fail "$crate doesn't depend on iki-writer $version: its Cargo.toml needs iki-writer = \"=$version\"."
    cp Cargo.lock "$crate/Cargo.lock.$$"
    mv "$crate/Cargo.lock.$$" "$crate/Cargo.lock"
    echo "Pinned $crate to iki-writer $version ($sum)."
    exit 0
    ;;
esac

# build and test: offline, from what fetch gathered.
if [ -n "${IKI_WRITER_TARGET_DIR:-}" ]; then
  target=$IKI_WRITER_TARGET_DIR
  mkdir -p "$target"
else
  target="$stage/target"
fi

gathered_or_own
if [ "$step" = test ]; then
  shift 2
else
  set --
fi
if [ "$own_sources" = no ]; then
  set -- --config 'source.crates-io.replace-with="iki-writer-crates"' \
    --config "source.iki-writer-crates.directory=\"$gathered\"" "$@"
fi

if [ "$step" = test ]; then
  # shellcheck disable=SC2086
  ${CARGO:-cargo} test --locked --offline --target-dir "$target" "$@"
  exit 0
fi

# Paths in the module (panic locations) must not say where anything was built. The crate
# is /writer, and every crate it reads /crates/<name>-<version>, whether gathered by fetch
# or vendored in the crate by `cargo vendor --versioned-dirs`. rustc applies the last
# remap that matches, so the narrower ones come later.
flags="--remap-path-prefix=$work=/writer${separator}--remap-path-prefix=$cargo_home=/cargo"
if [ "$own_sources" = no ]; then
  flags="$flags${separator}--remap-path-prefix=$gathered=/crates"
fi
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

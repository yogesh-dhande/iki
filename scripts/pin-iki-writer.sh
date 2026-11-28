#!/bin/sh
# Pin iki-writer again after a change to what its crate carries (ADR 0230): its source,
# its README, its dependencies or its version.
#
#   scripts/pin-iki-writer.sh
#
# 1. Writes worker/crates/iki-writer/Cargo.lock, the lock its published crate carries,
#    from the workspace's (worker/Cargo.lock), so both pin the same crates.
# 2. Packages it as `cargo publish` will, with the writer builder's toolchain, and writes
#    its SHA-256 to worker/crates/iki-writer/iki-writer.crate.sha256: what crates.io will
#    have of this version. A version already published never changes; bump it instead.
# 3. Pins each writer of Iki's own plugins and examples to it (build-writer.sh lock).
#
# Then rebuild each of those writers' digests, in the builder, and commit them with this:
#
#   uv run iki plugin writers <plugin dir> --update
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
sdk="$root/worker/crates/iki-writer"

for dir in "${CARGO_HOME:-$HOME/.cargo}/bin" /opt/homebrew/opt/rustup/bin /usr/local/opt/rustup/bin; do
  if [ -x "$dir/rustup" ]; then
    PATH="$dir:$PATH"
    break
  fi
done
command -v rustup >/dev/null 2>&1 || { echo "pin-iki-writer: no rustup. See https://rustup.rs." >&2; exit 1; }
channel=$("$root/scripts/writer-toolchain.sh")
rustup toolchain list | grep -q "^$channel" ||
  rustup toolchain install "$channel" --profile minimal --target wasm32-unknown-unknown >&2
CARGO="cargo +$channel"
export CARGO

work=$(mktemp -d "${TMPDIR:-/tmp}/iki-writer-pin.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM

# The SDK's own lock: the workspace's, less what the SDK doesn't use.
mkdir "$work/sdk"
cp -R "$sdk/Cargo.toml" "$sdk/README.md" "$sdk/src" "$work/sdk/"
cp "$root/worker/Cargo.lock" "$work/sdk/Cargo.lock"
(cd "$work/sdk" && $CARGO metadata --offline --format-version 1 --quiet >/dev/null)
cp "$work/sdk/Cargo.lock" "$sdk/Cargo.lock"

line=$(sh "$sdk/build-writer.sh" package "$work/package")
printf '%s\n' "$line" >"$sdk/iki-writer.crate.sha256"
echo "iki-writer: $line"

for toml in "$root"/plugins/*/writers/*/Cargo.toml "$root"/examples/plugins/*/writers/*/Cargo.toml; do
  [ -f "$toml" ] || continue
  sh "$sdk/build-writer.sh" lock "$(dirname "$toml")"
done
echo "Now rebuild each writer's digest: uv run iki plugin writers <plugin dir> --update"

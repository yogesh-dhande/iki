#!/bin/sh
# Builds the shared parser module for the browser package, app tests, and server.
#
# It needs Rust from rustup, which reads worker/rust-toolchain.toml and installs its
# WebAssembly target. Homebrew's rustup is keg-only, so it's looked for where Homebrew
# keeps it too.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
for dir in "${CARGO_HOME:-$HOME/.cargo}/bin" /opt/homebrew/opt/rustup/bin /usr/local/opt/rustup/bin; do
  if [ -x "$dir/rustup" ]; then
    PATH="$dir:$PATH"
    break
  fi
done
if ! command -v rustup >/dev/null 2>&1; then
  echo "build-wasm: rustup isn't installed. The web app's parsers are built with it:" >&2
  echo "see https://rustup.rs, or 'brew install rustup'." >&2
  exit 1
fi

cd "$root/worker"
if [ "${1:-}" = --package-notices ]; then
  report=$(mktemp)
  trap 'rm -f "$report"' EXIT
  cargo build --locked -p iki-wasm --target wasm32-unknown-unknown --release \
    --message-format=json-render-diagnostics > "$report"
  node "$root/scripts/write-wasm-notices.mjs" < "$report"
else
  cargo build --locked -p iki-wasm --target wasm32-unknown-unknown --release
fi
mkdir -p "$root/web/app/wasm" "$root/web/packages/science/wasm"
cp target/wasm32-unknown-unknown/release/iki_wasm.wasm "$root/web/packages/science/wasm/iki_wasm.wasm"
cp target/wasm32-unknown-unknown/release/iki_wasm.wasm "$root/web/app/wasm/iki_wasm.wasm"
# The server checks apps' expressions (ADR 0090), and iki-spec plugins' rules and guards
# (ADR 0127), with the same module.
cp target/wasm32-unknown-unknown/release/iki_wasm.wasm "$root/spec/src/iki_spec/iki_wasm.wasm"

# The deployment CLI copies this digest-pinned build from iki-spec into the DNA worker
# archive. Direct development invocations read the same ignored file in the plugin.
"$root/scripts/build-oligo-wasm.sh" --with-notice "$root/spec/src/iki_spec/oligo-measurement.wasm"
# Keep unfinished local copies outside the plugin directory that workers bundle.
(
  part=$(mktemp "$root/plugins/.oligo-measurement.XXXXXXXX")
  trap 'rm -f "$part"' EXIT
  cp "$root/spec/src/iki_spec/oligo-measurement.wasm" "$part"
  chmod 644 "$part"
  mv -f "$part" "$root/plugins/dna/data/oligo-measurement.wasm"
)

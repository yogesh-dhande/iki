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
# The file previews' readers (ADR 0226): the web app alone runs them, in its own worker.
cargo build --locked -p iki-previews --target wasm32-unknown-unknown --release
mkdir -p "$root/web/app/wasm" "$root/web/packages/science/wasm"
cp target/wasm32-unknown-unknown/release/iki_previews.wasm "$root/web/app/wasm/iki_previews.wasm"
cp target/wasm32-unknown-unknown/release/iki_wasm.wasm "$root/web/packages/science/wasm/iki_wasm.wasm"
cp target/wasm32-unknown-unknown/release/iki_wasm.wasm "$root/web/app/wasm/iki_wasm.wasm"
# The server checks apps' expressions (ADR 0090), and iki-spec plugins' rules and guards
# (ADR 0127), with the same module.
cp target/wasm32-unknown-unknown/release/iki_wasm.wasm "$root/spec/src/iki_spec/iki_wasm.wasm"

# The deployment CLI copies this digest-pinned build from iki-spec into the DNA worker
# archive. Direct development invocations read the same ignored file in the plugin.
"$root/scripts/build-oligo-wasm.sh" --with-notice "$root/spec/src/iki_spec/oligo-measurement.wasm"
# The release pin is generated in the Linux x86-64 Docker stage. A source wheel
# built elsewhere can carry native rustc's Wasm bytes, which can differ by host.
module="$root/spec/src/iki_spec/oligo-measurement.wasm"
test -s "$module"
digest=$(cat "$root/plugins/dna/data/oligo-measurement.sha256")
if [ "${#digest}" -ne 64 ] || ! printf '%s\n' "$digest" | grep -Eq '^[0-9a-f]{64}$'; then
  echo "The DNA measurement marker must hold a SHA-256 digest." >&2
  exit 1
fi
host="$(uname -s)/$(uname -m)"
case "$host" in
  Linux/x86_64 | Linux/amd64)
    printf '%s  %s\n' "$digest" "$module" |
      sha256sum --check --strict -
    ;;
  *) echo "Built oligo-measurement.wasm on $host. Its SHA-256 pin is compared on Linux x86-64." >&2 ;;
esac
# Keep unfinished local copies outside the plugin directory that workers bundle.
(
  part=$(mktemp "$root/plugins/.oligo-measurement.XXXXXXXX")
  trap 'rm -f "$part"' EXIT
  cp "$module" "$part"
  chmod 644 "$part"
  mv -f "$part" "$root/plugins/dna/data/oligo-measurement.wasm"
)
# And the plugin runtime's wheel (ADR 0288), which iki-spec carries beside the modules.
"$root/scripts/build-plugin-runtime.sh"

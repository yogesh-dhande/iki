#!/bin/sh
# Produce both generic Linux targets from the same verified server module.
set -eu

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <verified iki_wasm.wasm> <output directory>" >&2
  exit 2
fi
root=$(cd "$(dirname "$0")/.." && pwd)
wasm=$1
output=$2
digest() { sha256sum < "$1" | cut -d ' ' -f 1; }
wasm_hash=$(digest "$wasm")
if [ "$wasm_hash" != "$(cat "$root/server/iki_wasm.wasm.sha256")" ]; then
  echo "Input module does not match server/iki_wasm.wasm.sha256" >&2
  exit 1
fi

# wasmtime-py 50.0.1 ships the native v50.0.0 engine (see its tagged
# ci/download-wasmtime.py). Serialized code must use the same native version.
python_version=50.0.1
cli_version=50.0.0
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) host=x86_64; cli_hash=77c19ee0437991f9ac7837b9c269d9c7076744f85dad263fea5214cc2723c7ff ;;
  Linux/aarch64) host=aarch64; cli_hash=10f9ad1f8539d2e04e041cc622de6a270109ab1e43a6be1f69843c2082166832 ;;
  *) echo "The Wasmtime producer needs a native Linux amd64 or arm64 host." >&2; exit 1 ;;
esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
archive="wasmtime-v$cli_version-$host-linux"
curl -fsSL --retry 3 "https://github.com/bytecodealliance/wasmtime/releases/download/v$cli_version/$archive.tar.xz" -o "$work/cli.tar.xz"
if [ "$(digest "$work/cli.tar.xz")" != "$cli_hash" ]; then
  echo "Wasmtime CLI release archive failed SHA-256 verification." >&2
  exit 1
fi
tar -xJf "$work/cli.tar.xz" -C "$work" "$archive/wasmtime"
for arch in amd64 arm64; do
  case "$arch" in
    amd64) target=x86_64-unknown-linux-gnu ;;
    arm64) target=aarch64-unknown-linux-gnu ;;
  esac
  mkdir -p "$work/$arch"
  # An explicit triple uses its baseline ISA. DRC is the Python engine's
  # collector. Supplying a fuel budget enables metering in the compiled code.
  "$work/$archive/wasmtime" compile --target "$target" -C cache=n -C collector=drc \
    -W fuel=1 -o "$work/$arch/iki_wasm.cwasm" "$wasm"
  compiled_hash=$(digest "$work/$arch/iki_wasm.cwasm")
  printf '{"format":1,"consume_fuel":true,"wasm_sha256":"%s","wasmtime_version":"%s","target":"%s","cwasm_sha256":"%s"}\n' \
    "$wasm_hash" "$python_version" "$target" "$compiled_hash" > "$work/$arch/iki_wasm.json"
done
for arch in amd64 arm64; do
  mkdir -p "$output/$arch"
  cp "$work/$arch/iki_wasm.cwasm" "$work/$arch/iki_wasm.json" "$output/$arch/"
done

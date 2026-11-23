#!/bin/sh
# Check an exported server Wasm payload against this checkout's pins.
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <artifact-directory>\n' "$0" >&2
  exit 2
fi
root=$(cd "$(dirname "$0")/.." && pwd)
artifact=$1

required_file() {
  if [ ! -f "$1" ] || [ -L "$1" ] || [ ! -s "$1" ]; then
    printf 'Expected a nonempty regular file at %s\n' "$1" >&2
    exit 1
  fi
}

verify() {
  name=$1
  pin=$root/$2
  file=$artifact/$name
  required_file "$pin"
  required_file "$file"
  expected=$(cat "$pin")
  case "$expected" in
    *[!0-9a-f]*)
      printf 'Invalid SHA-256 pin for %s in %s\n' "$name" "$pin" >&2
      exit 1
      ;;
  esac
  if [ "${#expected}" -ne 64 ]; then
    printf 'Invalid SHA-256 pin for %s in %s\n' "$name" "$pin" >&2
    exit 1
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum < "$file")
  else
    actual=$(shasum -a 256 < "$file")
  fi
  actual=${actual%% *}
  if [ "$actual" != "$expected" ]; then
    printf '%s SHA-256 mismatch. Expected %s, actual %s\n' "$name" "$expected" "$actual" >&2
    exit 1
  fi
  printf '%s SHA-256 verified: %s\n' "$name" "$actual"
}

verify iki_wasm.wasm server/iki_wasm.wasm.sha256
verify dna-annotation.wasm plugins/plasmid/data/dna-annotation.wasm.sha256
required_file "$artifact/rust-standard-library.html"
python3 "$root/scripts/verify-server-aot.py" "$artifact"

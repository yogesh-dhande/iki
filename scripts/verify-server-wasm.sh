#!/bin/sh
# Check an exported server Wasm payload against this checkout's pins.
set -eu

# The dev deployment CLI stages only the portable modules into its local Python
# package. Server images and published artifacts must also carry both native targets.
check_native=true
if [ "${1:-}" = --portable-only ]; then
  check_native=false
  shift
fi
if [ "$#" -ne 1 ]; then
  printf 'Usage: %s [--portable-only] <artifact-directory>\n' "$0" >&2
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
  # A plugin module's pin is written as sha256sum writes it, `<digest>  <name>`: then the
  # name must be this module's, and the digest is checked as a bare pin is.
  case "$expected" in
    *" "*)
      if [ "${expected#*  }" != "$name" ]; then
        printf 'The SHA-256 pin in %s names %s, not %s\n' "$pin" "${expected#*  }" "$name" >&2
        exit 1
      fi
      expected=${expected%%  *}
      ;;
  esac
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
# The file previews' readers, which the web app's previews worker runs (ADR 0226).
verify iki_previews.wasm web/app/iki_previews.wasm.sha256
verify dna-annotation.wasm plugins/plasmid/data/dna-annotation.wasm.sha256
verify oligo-measurement.wasm plugins/dna/data/oligo-measurement.sha256
# The modules first-party plugins' actions call (ADR 0200, ADR 0201).
verify plasmid-tools.wasm plugins/plasmid/data/plasmid-tools.wasm.sha256
verify oligo-checks.wasm plugins/dna/data/oligo-checks.wasm.sha256
required_file "$artifact/rust-standard-library.html"
if [ "$check_native" = true ]; then
  python3 "$root/scripts/verify-server-aot.py" "$artifact"
fi

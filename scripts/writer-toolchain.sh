#!/bin/sh
# Print the Rust toolchain of the writer builder (spec/src/iki_spec/writers/build.py's
# BUILDER), such as 1.93.0: the one plugins' writers, and iki-writer's crate, are built
# and packaged with. It is pinned apart from Iki's own toolchain (worker/rust-toolchain.toml).
#
#   writer-toolchain.sh          the Rust version, such as 1.93.0
#   writer-toolchain.sh --image  the builder image, pinned by digest
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
image=$(awk '
  /^BUILDER = \(/ { inside = 1; next }
  inside && /^\)/ { exit }
  inside { gsub(/[ "]/, ""); printf "%s", $0 }
' "$root/spec/src/iki_spec/writers/build.py")
case "$image" in
  rust:*@sha256:*) ;;
  *) echo "writer-toolchain: no rust: image pinned by digest in spec/src/iki_spec/writers/build.py." >&2; exit 1 ;;
esac
if [ "${1:-}" = --image ]; then
  echo "$image"
  exit 0
fi
version=${image#rust:}
echo "${version%%-*}"

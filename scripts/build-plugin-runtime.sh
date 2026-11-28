#!/bin/sh
# Builds the plugin runtime's wheel (runtime/, ADR 0288) into iki-spec, which carries it
# for `iki deploy build` to install into the environments that declare `iki_runtime`.
#
# runtime/pyproject.toml pins one uv_build, which writes the same bytes for the same
# source: the wheel built here, in the server image and in the release is one file.
# With a directory, it builds there instead (the release's PyPI job).
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-"$root/spec/src/iki_spec/plugin_runtime"}
mkdir -p "$out"
work=$(mktemp -d "$out/.build.XXXXXX")
trap 'rm -rf "$work"' EXIT
uv build --quiet --wheel --out-dir "$work" "$root/runtime"
set -- "$work"/iki_plugin_runtime-*-py3-none-any.whl
if [ "$#" -ne 1 ] || [ ! -s "$1" ]; then
  echo "build-plugin-runtime: uv build made no single wheel" >&2
  exit 1
fi
chmod 644 "$1"
built=${1##*/}
# Moved into place in one rename, so a build reading it at the same time (the dev
# cluster's server and worker builds run this at once) sees the old file or the new one.
mv -f "$1" "$out/$built"
# Then only this build's wheel is left, so iki-spec carries one.
for other in "$out"/iki_plugin_runtime-*.whl; do
  if [ "$other" != "$out/$built" ]; then rm -f "$other"; fi
done

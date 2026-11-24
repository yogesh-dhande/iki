#!/bin/sh
# Check the proposed manifest against the deployment selected by the Iki sign-in.
set -eu

if [ "$#" -lt 2 ]; then
  printf '%s\n' 'Usage: build-and-preflight.sh NAMESPACE/NAME PLUGIN [DEPENDENCY ...]' >&2
  exit 2
fi

plugin=$1
shift
case "$plugin" in
  *[!a-z0-9_/]* | /* | */ | */*/*)
    printf '%s\n' 'Expected a qualified plugin name (namespace/name).' >&2
    exit 2
    ;;
  */*) ;;
  *)
    printf '%s\n' 'Expected a qualified plugin name (namespace/name).' >&2
    exit 2
    ;;
esac

build=$(mktemp -d "${TMPDIR:-/tmp}/iki-skill-preflight.XXXXXXXX")
trap 'rm -rf "$build"' EXIT
iki plugin build "$@" --output "$build" >&2
iki plugin preflight "$build/iki_plugin_${plugin%/*}__${plugin#*/}/manifest.json" --json

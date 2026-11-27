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
# A candidate that doesn't build has no report to review: an input error (2), not the
# review exit (1) that preflight's findings get.
if ! iki plugin build "$@" --output "$build" >&2; then
  printf '%s\n' "The candidate didn't build, so it wasn't preflighted: see the errors above." >&2
  exit 2
fi
# Preflight's own exit: 0 clear, 1 findings to review, 2 an input, connection or access error.
status=0
iki plugin preflight "$build/iki_plugin_${plugin%/*}__${plugin#*/}/manifest.json" --json || status=$?
exit "$status"

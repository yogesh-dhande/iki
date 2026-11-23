#!/usr/bin/env bash
# Run a gate from the worktree root and leave its browser traces in one file
# that a remote runner can fetch, even when the command fails.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: $0 COMMAND [ARG ...]" >&2
  exit 2
fi

mkdir -p build
archive=build/browser-failures.tar.gz
rm -f "$archive"
work=$(mktemp -d "$PWD/build/browser-traces.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
export IKI_BROWSER_ARTIFACTS="$work/traces"
mkdir "$IKI_BROWSER_ARTIFACTS"

status=0
"$@" || status=$?

# A completed run always replaces the previous archive, even when a prerequisite
# failed before pytest started. Publish the tar atomically after all workers exit.
umask 077
# macOS tar otherwise adds metadata files even to an empty archive.
COPYFILE_DISABLE=1 tar -czf "$work/archive.tar.gz" -C "$IKI_BROWSER_ARTIFACTS" .
mv "$work/archive.tar.gz" "$archive"
echo "Saved browser trace archive to $archive" >&2
exit "$status"

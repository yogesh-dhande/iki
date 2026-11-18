#!/usr/bin/env bash
# Whether a change needs CI's code jobs. Prints `code=true` or `code=false` (to
# $GITHUB_OUTPUT on CI): false only when every changed file is documentation, so a
# Markdown edit runs the docs checks alone. Anything uncertain (a new branch, a base
# that can't be found, a manual run) runs everything.
#
#   EVENT    push, pull_request, merge_group or workflow_dispatch
#   BASE     the commit the change is compared with: the push's `before`, the pull
#            request's or merge group's base
#   HEAD     the commit being checked
set -euo pipefail

# Documentation: Markdown, and anything under docs/. Not a plugin's or the template's
# files (their trees are checked, README included), and not a package's own source
# (the units' licence ships inside iki_spec).
is_docs() {
  case "$1" in
    plugins/* | plugin-template/* | */src/*) return 1 ;;
    docs/* | *.md) return 0 ;;
    *) return 1 ;;
  esac
}

decide() {
  case "${EVENT:-}" in
    push | pull_request | merge_group) ;;
    *) echo true && return ;;
  esac
  local base=${BASE:-}
  if [[ -z "$base" || "$base" =~ ^0+$ ]] || ! git cat-file -e "$base^{commit}" 2>/dev/null; then
    echo true && return
  fi
  local files
  files=$(git diff --name-only "$base" "${HEAD:?}")
  [[ -n "$files" ]] || { echo true && return; }
  while IFS= read -r file; do
    is_docs "$file" || { echo true && return; }
  done <<<"$files"
  echo false
}

code=$(decide)
echo "code=$code" | tee -a "${GITHUB_OUTPUT:-/dev/null}"

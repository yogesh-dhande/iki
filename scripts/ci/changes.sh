#!/usr/bin/env bash
# Whether a change needs CI's code and server image jobs. A package README can be
# documentation for the code jobs and still be copied into the server build.
# Anything uncertain (a new branch, an unknown base, a manual run) runs both.
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

is_server_input() {
  # Include the input directories, workspace manifests, shared build scripts and
  # workflow pins. A Dockerfile can take a new file from any of these directories.
  case "$1" in
    server/* | cli/* | spec/* | codegen/* | sdk/python/* | web/* | worker/* | scripts/*) return 0 ;;
    .github/workflows/* | .github/actions/* | .github/dependabot.yml) return 0 ;;
    .dockerignore | .grype.yaml | .node-version | .python-version | LICENSE | NOTICE) return 0 ;;
    plugins/plasmid/data/*.sha256) return 0 ;;
    pyproject.toml | uv.lock | package.json | pnpm-lock.yaml | pnpm-workspace.yaml | tsconfig.base.json) return 0 ;;
    *) return 1 ;;
  esac
}

changed_inputs() {
  case "${EVENT:-}" in
    push | pull_request | merge_group) ;;
    *) return ;;
  esac
  local base=${BASE:-} head=${HEAD:-} file
  if [[ -z "$base" || -z "$head" ]] || ! git cat-file -e "$base^{commit}" 2>/dev/null ||
    ! git cat-file -e "$head^{commit}" 2>/dev/null; then
    return
  fi
  changed_paths=$(mktemp)
  trap 'rm -f "$changed_paths"' EXIT
  # Treat moves as a removal and an addition, so a file moved out of the Docker
  # context still counts. NUL delimiters preserve paths with spaces or newlines.
  if ! git diff --no-renames --name-only -z "$base" "$head" > "$changed_paths" ||
    [[ ! -s "$changed_paths" ]]; then
    return
  fi
  code=false
  server_image=false
  while IFS= read -r -d '' file; do
    if ! is_docs "$file"; then code=true; fi
    if is_server_input "$file"; then server_image=true; fi
  done < "$changed_paths"
}

code=true
server_image=true
changed_inputs
printf 'code=%s\nserver_image=%s\n' "$code" "$server_image" | tee -a "${GITHUB_OUTPUT:-/dev/null}"

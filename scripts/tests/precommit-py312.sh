#!/usr/bin/env bash
# Exercise both public gates against the Python 3.12 regression in a copy of HEAD.
set -euo pipefail

repo=$(git rev-parse --show-toplevel)
head=$(git rev-parse HEAD)
temp=$(mktemp -d "${TMPDIR:-/tmp}/iki-precommit.XXXXXXXX")
trap 'rm -rf "$temp"' EXIT
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE UV_PROJECT_ENVIRONMENT VIRTUAL_ENV
# Keep hook installation inside the disposable clone even with global core.hooksPath.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
git clone --quiet --shared --no-checkout "$repo" "$temp/repo"
cd "$temp/repo"
git checkout --quiet --detach "$head"
make hooks

file=cli/src/iki_cli/preflight.py
printf '\n\n# Exercise a staged CLI change.\n' >> "$file"
git add -- "$file"
TIMEFORMAT='Hook took %3R s'
time git hook run pre-commit
time git hook run pre-commit
.venv-py312/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12); print(sys.version)'

expect_import_failure() {
  if "$@" > "$temp/output" 2>&1; then
    echo "Expected $* to reject the broken annotation" >&2
    return 1
  fi
  if ! grep -F "type '_SubParsersAction' is not subscriptable" "$temp/output"; then
    cat "$temp/output" >&2
    echo "$* failed for a different reason" >&2
    return 1
  fi
  echo "$* caught the Python 3.12 regression"
}

git restore --source=HEAD --staged --worktree -- "$file"
git diff f5cf0a5f^ f5cf0a5f -- "$file" | git apply --reverse --index
# Leave a fixed copy on disk to catch hooks that accidentally read unstaged edits.
git restore --source=HEAD --worktree -- "$file"
expect_import_failure git hook run pre-commit
git restore --worktree -- "$file"
expect_import_failure make check-python

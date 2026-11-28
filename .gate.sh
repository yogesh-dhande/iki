#!/usr/bin/env bash
# The full merge gate, run on the VM from a trial-merge worktree. IKI_DEVCI=1 adds make dev-ci (vm-run passes IKI_* through).
make setup >/dev/null 2>&1 && make -s test-wasm >/dev/null 2>&1 && make web >/dev/null 2>&1 && make -s test-browsers >/dev/null 2>&1 || echo SETUP_FAILED
uv run scripts/first_party_trees.py --check && echo TREES_OK
(cd worker && cargo build -q 2>&1 | tail -2)
IKI_WORKER=$PWD/worker/target/debug/iki-worker uv run scripts/ci/check_plugin_examples.py 2>&1 | tail -2
make -s core client protocol >/dev/null 2>&1; git status --porcelain | grep -v '^??'; echo DRIFT_DONE
pnpm -s test 2>&1 | grep -E 'Test Files|FAIL'; echo WEB_RC=${PIPESTATUS[0]}
# Every pyproject testpath except server/tests (next line), so nothing CI runs is skipped here.
uv run pytest -q plugins/*/tests spec/tests cli/tests codegen/tests sdk/python/tests deploy/helm/iki/tests deploy/dev/tests deploy/live deploy/gcp/tests deploy/aws/tests scripts/ci/tests -n 32 2>&1 | grep -E '^(FAILED|ERROR)|passed|failed' | tail -8
export PYTHONPATH=scripts/ci; pv=$(sed -n '/^name = "pytest"$/{n;p;}' uv.lock | cut -d'"' -f2); uv run --no-project --with "pytest==$pv" pytest -q -p no:cacheprovider -p pytest_gate_skips -c /dev/null --rootdir . docs/tests 2>&1 | grep -E '^(FAILED|ERROR)|passed|failed' | tail -4; echo DOCS_TESTS_DONE; unset PYTHONPATH
uv run pytest -q server/tests -n auto --dist worksteal 2>&1 | grep -v '^{' | grep -E '^(FAILED|ERROR)|passed|failed' | tail -30; echo PYTEST_RC=${PIPESTATUS[0]}
if [ "${IKI_DEVCI:-${DEVCI:-0}}" = 1 ]; then dl="$PWD/.devci.log"; make dev-ci > "$dl" 2>&1; rc=$?; echo DEVCI_RC=$rc; if [ $rc != 0 ]; then grep -iE "error|fail|Traceback" "$dl" | tail -25; tail -40 "$dl"; fi; fi

---
name: iki-test-plugin
description: Verify an Iki plugin's manifests, migrations, worker actions, and lab workflow in local tests and CI.
---

# Test an Iki plugin

Use an Iki CLI and `iki-worker` that match the deployment. `iki plugin test` runs the worker commands locally, so install their environment requirements as well. In the Iki source checkout, use the locked Python workspace and [build Wasm](https://github.com/yogesh-dhande/iki/blob/main/scripts/build-wasm.sh) for manifest rules and guards. The [worker build instructions](https://github.com/yogesh-dhande/iki/blob/main/worker/README.md) cover building from source.

## Run the plugin checks

Set `SKILL_DIR` to the directory containing this file and `PLUGIN` to the plugin directory. Export `IKI_WORKER` as the worker executable if it is outside `PATH`. The example takes `DNA` as a dependency directory. Repeat `--with` for each dependency, including transitive ones, or omit it when there are none.

```sh
sh "$SKILL_DIR/scripts/check-plugin.sh" "$PLUGIN" --with "$DNA"
```

The script runs `validate`, `makemigrations --check --no-input` and `test`. Confirm that the summary counts the action fixtures you expected to run. If the migration check fails, generate and review the migration before rerunning it. For a rename, run `iki plugin makemigrations` interactively and confirm the pair before inspecting the generated history operations. [CLI options](https://github.com/yogesh-dhande/iki/blob/main/cli/README.md#iki-plugin-makemigrations)

Write an [`ActionTest`](https://ikisci.org/guides/write-a-plugin/action-tests/) for each action and include the input records, file fixtures, and the writes or error expected. Each input is a list, even if it holds one record. `expect` matches only the values it names, so assert the important fields. When the target ID matters, see the [fixture's run input builder](https://github.com/yogesh-dhande/iki/blob/main/spec/src/iki_spec/action_tests.py) for the deterministic IDs it assigns. Use pytest for parser edge cases, calculations, and server behavior such as permissions, state changes, signatures, and an import retry. The worker tests check what a command proposes to write. Server tests establish what gets committed.

## Worked example in Plate assays

From the Iki repository root, with the source environment active and the worker installed, run the action fixtures and pytest. The first-party migration test passes the plugin's full dependency closure to `iki plugin makemigrations --check`. The action runner discovers first-party dependencies in a source checkout. Start with the [reader fixtures](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/tests/reader_actions.yaml). Observe that an incomplete layout reports missing assignments and that a valid preview can commit measurements. The [workflow tests](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/tests/test_assay_workflow.py) exercise the parser and fit. The [server review test](https://github.com/yogesh-dhande/iki/blob/main/server/tests/test_plate_review.py) exercises the two signature flows and correction.

```sh
iki plugin test plugins/plate_assays
uv run pytest 'server/tests/test_first_party_plugins.py::test_it_is_valid_and_its_migrations_are_current[plate_assays]'
uv run pytest plugins/plate_assays/tests server/tests/test_plate_review.py
```

Use the [plate map](https://github.com/yogesh-dhande/iki/blob/main/server/tests/test_plate_map_component.py) and [curve component](https://github.com/yogesh-dhande/iki/blob/main/server/tests/test_dose_response_component.py) Playwright tests as examples. Run browser tests with the repository's browsers installed. Cover the user's clicks and keyboard controls, axe, narrow layouts, and light and dark themes.

```sh
uv run pytest server/tests/test_plate_map_component.py server/tests/test_dose_response_component.py
```

## Wire up CI

- A standalone plugin starts with the template's [CI workflow](https://github.com/yogesh-dhande/iki/blob/main/plugin-template/.github/workflows/ci.yml). Pin the Iki version and dependency commit, and add pytest to its CI if you introduce Python tests. Use the [upgrade skill](../iki-upgrade-plugin/SKILL.md) for an installed schema.
- In the Iki monorepo, register any new first-party plugin and its dependency closure in the [test matrix](https://github.com/yogesh-dhande/iki/blob/main/server/tests/first_party_dependencies.py). The [worker tests](https://github.com/yogesh-dhande/iki/blob/main/server/tests/test_worker_local.py) discover action fixtures from that matrix. Put server and browser tests in `server/tests`, or register a new plugin-local pytest directory in [pytest's configuration](https://github.com/yogesh-dhande/iki/blob/main/pyproject.toml). Run `make first-party` after changing a first-party plugin and commit the updated tree list.
- For a plugin installed in the dev cluster, add it to the [dev lockfile](https://github.com/yogesh-dhande/iki/blob/main/deploy/dev/plugins.lock.yaml) with its dependencies. For Plate assays, the [dev e2e seed](https://github.com/yogesh-dhande/iki/blob/main/deploy/dev/sso.py) calls `plate_assay` and `dose_response_starters`. The seed sets up the project, opens the reader fixture and exercises both review configurations. Run `make dev-ci` to exercise the deployed app and its seed, using the [dev cluster instructions](https://github.com/yogesh-dhande/iki/blob/main/deploy/dev/README.md).

```sh
make first-party
make dev-ci
```

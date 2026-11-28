---
name: iki-write-plugin
description: Build a new Iki plugin from the template with typed records, workflows, worker actions, and an example the lab can run.
---

# Write an Iki plugin

Put one lab workflow through the plugin, from the initial record or input file to a result someone can use. Agree on what evidence the result keeps and who can approve it before modeling the records.

## Start from the template

Use the [Iki plugin template](https://github.com/yogesh-dhande/iki/tree/main/plugin-template) at the revision your deployment uses. If the project already started from the template, work in that copy. Otherwise, copy the whole template, including its CI and skills. Use the deployment's Iki CLI version. For development in the Iki monorepo, run `uv sync --locked` and put its `.venv/bin` on `PATH`. Build the [shared Wasm module](https://github.com/yogesh-dhande/iki/blob/main/scripts/build-wasm.sh) to validate CEL rules or guards in a source checkout.

1. Choose the permanent `namespace/name` in `plugin.yaml`. Declare just the plugins and schema versions your references need. Use the versions pinned by the deployment, including transitive dependencies when building or generating migrations.
2. Adapt the template’s Sample type, storage action and smoke test. When you copy and rename the plugin for a new lab, generate its initial migration under the new name. The template README walks through creating a record and running the action.
3. Pin the Iki version your deployment uses in the template’s CI workflow. If you add a dependency, check it out at the deployment’s pinned commit and pass its directory to each CLI command with `--with`.

Use the [manifest spec](https://github.com/yogesh-dhande/iki/blob/main/spec/README.md) and the template's schema hints when editing YAML. The CLI validates the manifests and generates the Django models and migrations.

The [storage labels lesson](https://github.com/yogesh-dhande/iki/tree/main/examples/plugins/storage_labels) shows a deployment setting, a DNA extension, two local actions in a workflow, a creation trigger and a label component. Copy it when your lab uses its own storage codes. Workflow steps and triggers resolve actions in the plugin that declares them.

## Model the workflow

- Declare each record in `types/`. Use references and stable record IDs for reusable evidence. Declare typed fields, required and immutable facts, and quantity units. Define identity only for records that should deduplicate across projects. Use `rules` for invariants on all writes and `draw_guards` when consuming a quantity must meet conditions. [Lot draw guards](https://github.com/yogesh-dhande/iki/blob/main/plugins/lots/types/lot.yaml)
- For a workflow with review, offer **Research** or **Regulated QC** and keep the choice on each result. Iki's own plugins let a project choose at setup through a starter, but a lab plugin can't declare starters (the validator warns, `unknown-starter`): let each deployment choose instead. Declare an `enum` setting of the procedures, have the action that records a result copy it into an immutable computed field, and branch the result's state guards on that field. [The lots plugin's `verification_flow`](https://github.com/yogesh-dhande/iki/blob/main/plugins/lots/plugin.yaml) Research has a recorded result and one signed review. Regulated QC has a signed submission, independent signed review and signed QA release. Declare roles in `plugin.yaml` and use `set_by`, signatures and guards on states. Check `state.from`, prior signers and `edits.<field>.<state>` to enforce independence even when one person has several roles. Lock signed states. Use `on_edit` where a correction must return a result to an editable state and require another review. Test the correction as well as refused skips and self-review. [Correction and independence](https://github.com/yogesh-dhande/iki/blob/main/docs/adr/0179-a-corrected-result-goes-back-for-review.md)
- Declare each action's inputs, writes and environment. Keep parsing in testable functions and use the worker's run input and output contract for effects. For file imports, preserve the source, preview mapping problems before committing, and handle replay without duplicate records.
- Use Iki's standard pages for typed records. Add a component when a domain view helps someone act or inspect evidence. Declare the fields it reads and actions it offers. Use Iki's theme tokens and accessible controls, and check the view at 390px and in both themes.
- Write a component's source in `web/src/<name>.js` and ask Iki for things only through [@ikisci/component](https://github.com/yogesh-dhande/iki/tree/main/web/packages/component)'s helpers (`select`, `more`, `refine`, `open`, `run`), never by dispatching its events by hand. Offer a `run` button only when `place.runs` lists the action. Keep `web/` a package of its own with `@ikisci/component` at your deployment's Iki version, build `components/<name>.js` with `npm run bundle` there, and commit both; the template's CI checks that they match.
- Ship source and license information for public examples. Label synthetic inputs and expected results. For scientific formulas or constants, cite their primary sources in code and tests. Document setup, roles, the example inputs and the result the lab should see.

## Validate and generate

Set `SKILL_DIR` to the directory containing this file and `PLUGIN` to the plugin directory. The template needs only Iki core. Add one `--with` argument per dependency directory if your lab uses other plugins.

```sh
sh "$SKILL_DIR/scripts/validate-and-migrate.sh" "$PLUGIN"
```

Review the generated migration. Use the [test skill](../iki-test-plugin/SKILL.md) to exercise an `ActionTest` and any service and browser paths. For edits to an installed plugin, use the [upgrade skill](../iki-upgrade-plugin/SKILL.md).

## Worked example in Plate assays

Follow the [Plate assays walkthrough](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/README.md). The [plate type](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/types/assay_plate.yaml) gives a reading its well and assignment. The [reader parser](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/scripts/reader.py) reads a MARS export. [Preview Reader](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/actions/preview_reader.yaml) surfaces missing or unassigned wells before [Commit Reader](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/actions/commit_reader.yaml) creates measurements. The [plate map component](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/components/plate-map.yaml) puts assignment and import controls next to the measurements. Use the [public instrument file and synthetic series](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/data/reader.md) to reproduce the workflow.

Read the [setup setting](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/plugin.yaml) and compare the [Research result](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/types/research_assay_result.yaml) with the [Regulated QC result](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/types/qc_assay_result.yaml). The latter checks author, submitter, reviewer and intervening edits as appropriate for each signature. Run validation from the Iki repository root to see the complete model, including its dependency on analysis requests.

```sh
iki plugin validate plugins/plate_assays --with plugins/inventory --with plugins/lab_roles --with plugins/requests
```

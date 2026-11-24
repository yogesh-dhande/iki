---
name: iki-write-plugin
description: Build a new Iki plugin from the template with typed records, workflows, worker actions, and an example the lab can run.
---

# Write an Iki plugin

Put one lab workflow through the plugin, from the initial record or input file to a result someone can use. Agree on what evidence the result keeps and who can approve it before modeling the records.

## Start from the template

Use the [Iki plugin template](https://github.com/yogesh-dhande/iki/tree/main/plugin-template) at the revision your deployment uses. If the project already started from the template, work in that copy. Otherwise, copy the whole template, including its CI and skills. Use the deployment's Iki CLI version. For development in the Iki monorepo, run `uv sync --locked` and put its `.venv/bin` on `PATH`. Build the [shared Wasm module](https://github.com/yogesh-dhande/iki/blob/main/scripts/build-wasm.sh) to validate CEL rules or guards in a source checkout.

1. Choose the permanent `namespace/name` in `plugin.yaml`. Declare just the plugins and schema versions your references need. Use the versions pinned by the deployment, including transitive dependencies when building or generating migrations.
2. Replace the template's example types, actions, tests, and settings as needed. Remove the copied initial migration when this is a new plugin with no deployed data. That migration belongs to the example schema. Generate your own after the edit.
3. Fill in the template workflow's Iki version and dependency commit. Update its dependency checkout and `--with` arguments to match your manifest. For a plugin with no external dependencies, remove the dependency checkout and its `--with` arguments.

Use the [Write a plugin guide](https://ikisci.org/guides/write-a-plugin/), the [plugin spec reference](https://ikisci.org/reference/plugin-spec/) and the template's schema hints when editing YAML. The CLI validates the manifests and generates the Django models and migrations.

## Model the workflow

- Declare each record in `types/`. Use references and stable record IDs for reusable evidence. Declare typed fields, required and immutable facts, and quantity units. Define identity only for records that should deduplicate across projects. Use `rules` for invariants on all writes and `draw_guards` when consuming a quantity must meet conditions. [Lot draw guards](https://github.com/yogesh-dhande/iki/blob/main/plugins/lots/types/lot.yaml)
- For a workflow with review, let setup choose **Research** or **Regulated QC** and retain that choice with each result. Research has a recorded result and one signed review. Regulated QC has a signed submission, independent signed review and signed QA release. Declare roles in `plugin.yaml` and use `set_by`, signatures and guards on states. Check `state.from`, prior signers and `edits.<field>.<state>` to enforce independence even when one person has several roles. Lock signed states. Use `on_edit` where a correction must return a result to an editable state and require another review. Test the correction as well as refused skips and self-review. [Correction and independence](https://github.com/yogesh-dhande/iki/blob/main/docs/adr/0179-a-corrected-result-goes-back-for-review.md)
- Declare each action's inputs, writes and environment. Keep parsing in testable functions and use the worker's run input and output contract for effects. For file imports, preserve the source, preview mapping problems before committing, and handle replay without duplicate records.
- Use Iki's standard pages for typed records. Add a component when a domain view helps someone act or inspect evidence. Declare the fields it reads and actions it offers. Use Iki's theme tokens and accessible controls, and check the view at 390px and in both themes.
- Ship source and license information for public examples. Label synthetic inputs and expected results. For scientific formulas or constants, cite their primary sources in code and tests. Document setup, roles, the example inputs and the result the lab should see.

## Validate and generate

Set `SKILL_DIR` to the directory containing this file, `PLUGIN` to the plugin directory and `DNA` to a dependency directory for a plugin that still uses the template's `iki/dna` types. Drop that argument if unused and repeat `--with` for other dependencies.

```sh
sh "$SKILL_DIR/scripts/validate-and-migrate.sh" "$PLUGIN" --with "$DNA"
```

Review the generated migration. Use the [test skill](../iki-test-plugin/SKILL.md) to exercise an `ActionTest` and any service and browser paths. For edits to an installed plugin, use the [upgrade skill](../iki-upgrade-plugin/SKILL.md).

## Worked example in Plate assays

Follow the [Plate assays walkthrough](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/README.md). The [plate type](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/types/assay_plate.yaml) gives a reading its well and assignment. The [reader parser](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/scripts/reader.py) reads a MARS export. [Preview Reader](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/actions/preview_reader.yaml) surfaces missing or unassigned wells before [Commit Reader](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/actions/commit_reader.yaml) creates measurements. The [plate map component](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/components/plate-map.yaml) puts assignment and import controls next to the measurements. Use the [public instrument file and synthetic series](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/data/reader.md) to reproduce the workflow.

Read the [setup setting](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/plugin.yaml) and compare the [Research result](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/types/research_assay_result.yaml) with the [Regulated QC result](https://github.com/yogesh-dhande/iki/blob/main/plugins/plate_assays/types/qc_assay_result.yaml). The latter checks author, submitter, reviewer and intervening edits as appropriate for each signature. Run validation from the Iki repository root to see the complete model, including its dependency on analysis requests.

```sh
iki plugin validate plugins/plate_assays --with plugins/example_lims
```

---
name: iki-upgrade-plugin
description: Version and preflight a change to an installed Iki plugin, verify its migrations against old records, and prepare a release that preserves history.
---

# Upgrade an Iki plugin

Record the exact deployed plugin commit and dependency pins from the deployment lockfile. Keep the plugin's qualified name and existing migration files. Start from a Git revision that contains old `ActionTest` entities for representative records and values.

1. Edit the manifests and bump the plugin `version`. Increase each affected type's `schema_version` when what a record may hold changes, such as a new field or tightened rule. Review dependent plugins' schema ranges before releasing. Add optional fields first if old records have no value.
2. Validate using the intended dependency versions. Run `iki plugin makemigrations` interactively for a rename and confirm the intended pair. Mark removed fields `removed: true` so historical values remain readable. If you remove a state, give stored records a valid destination and review the effect on signed history. [Generated migrations](https://github.com/yogesh-dhande/iki/blob/main/docs/adr/0033-plugin-codegen-and-migrations.md) · [State migrations](https://github.com/yogesh-dhande/iki/blob/main/docs/adr/0121-a-plugin-cant-strand-records-in-a-state-it-removes.md)
3. Review the migration and test against the base commit. Set `PLUGIN` to the plugin directory, `BASE` to the deployed Git revision, and `IKI_TEST_DATABASE_URL` to an isolated PostgreSQL server where the test can create a scratch database. Run with a compatible `iki-worker` on `PATH` or in `IKI_WORKER`. The example uses a dependency directory `DNA`; omit it when unused and repeat `--with` for other dependencies.

```sh
iki plugin validate "$PLUGIN" --with "$DNA"
iki plugin makemigrations "$PLUGIN" --with "$DNA"
iki plugin test "$PLUGIN" --with "$DNA" --upgrade-from "$BASE"
```

Read the upgrade output and confirm that it created representative samples, moved them forward with history intact and rolled them back with their previous values. Add an explicit migration test when the base action fixtures don't cover important existing values or a signed revision. [Upgrade test contract](https://github.com/yogesh-dhande/iki/blob/main/docs/adr/0094-a-plugins-upgrade-is-tested-from-an-earlier-commit.md)

## Preflight the candidate and release

Select the deployment to check with the Iki SDK sign-in or `IKI_URL` and an all-project `IKI_TOKEN` with the `read` and `admin` scopes. Run as an Org Admin with read access to every project, including archived projects. Set `SKILL_DIR` to this skill directory and `PLUGIN_NAME` to the qualified plugin name. The script takes dependency directories as positional arguments. It uses `iki plugin build` to write a temporary compiled package, then runs `iki plugin preflight` with its `manifest.json`.

```sh
sh "$SKILL_DIR/scripts/build-and-preflight.sh" "$PLUGIN_NAME" "$PLUGIN" "$DNA"
```

Record the candidate and configuration revisions from the report. Exit 0 is clear, exit 1 calls for review of findings or incomplete sample coverage, and exit 2 is an input, connection or access error. Resolve incompatible existing records through a reviewed migration or remediation plan and rerun the check. For a promotion, run preflight using the manifest built with the deployment's actual settings and pinned dependencies. [Preflight and promotion](https://github.com/yogesh-dhande/iki/blob/main/deploy/config-template/README.md#how-a-change-reaches-a-deployment)

Commit the version, schema, new migration, tests and upgrade notes together. Keep previous releases and their commit pins available. Document any intentional one-way migration and the recovery path before release. Once the change is approved, release the tested commit and update the deployment's lockfile through its normal promotion process. For a first-party plugin, include the updated tree list from `make first-party`.

## Worked example in Iki DNA

The [library annotation upgrade](https://github.com/yogesh-dhande/iki/commit/e1dd443ff4d2b4e909c363330a95a15b7ceb7bde) adds optional `library_features` and `library_annotated_with` to [DNASequence](https://github.com/yogesh-dhande/iki/blob/main/plugins/dna/types/dna_sequence.yaml). Its [manifest](https://github.com/yogesh-dhande/iki/blob/main/plugins/dna/plugin.yaml) moved from 0.11.0 to 0.12.0 and the type's schema from 3 to 4. The [new migration](https://github.com/yogesh-dhande/iki/blob/main/plugins/dna/migrations/0002_library_annotation.py) adds nullable columns to the current and historical models. Existing records keep their prior values until an annotation is produced.

In an Iki checkout, set `BASE` to `e1dd443f^` to test that release transition. In the source environment, [build Iki's shared Wasm](https://github.com/yogesh-dhande/iki/blob/main/scripts/build-wasm.sh), which also builds the measurement module used by DNA action fixtures. On a staging deployment loaded with earlier records, build and preflight the proposed `iki/dna` manifest and confirm no old record requires the new values.

```sh
iki plugin test plugins/dna --upgrade-from "$BASE"
sh "$SKILL_DIR/scripts/build-and-preflight.sh" iki/dna plugins/dna
```

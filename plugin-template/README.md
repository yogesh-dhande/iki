# Iki plugin template

A starting point for a lab's own Iki plugin: entity types, the fields it adds
to other plugins' types, and the CI that checks them. Copy this directory
into a repository of its own (or use it as a GitHub template), then make it
yours.

What's here:

- `plugin.yaml`: the plugin, `acme/example`, its version, the plugins it
  depends on (here, Iki's `iki/dna`), and its settings.
- `types/sample.yaml`: an entity type, `acme/example.Sample`, with references to
  its own type and to `iki/dna.DNASequence`.
- `types/dna_sequence.yaml`: an extension adding two fields to
  `iki/dna.DNASequence`. They live in this plugin's tables.
- `actions/assign_storage.yaml` and `scripts/storage.py`: an action that sets
  a sample's storage to the plugin's setting, and the command it runs.
- `actions/tidy_storage.yaml`: another, with the same script, that asks for a
  persistent machine (`lifecycle: persistent`): a quick action that runs
  often can go to a pod that's kept warm, rather than wait for one of its own.
- `environments/tools.yaml`: where that command runs. On the container tier,
  each run gets a pod with this image, pinned by its digest.
- `workflows/store_sample.yaml`: a workflow that assigns a sample's storage,
  then decodes it: two actions run one after the other, as one.
- `triggers/store_new_samples.yaml`: a trigger that assigns each new sample's
  storage by itself, in projects whose Admins turn it on.
- `components/sample_label.yaml` and `components/sample-label.js`: a tab on
  each sample's page showing its tube label, drawn by the plugin's own code,
  and a sheet of labels for what the grid of samples finds ("Show as").
  Iki runs it in a sandboxed frame with no network and no access to the
  person's session, and gives it the sample, or the samples, with only the
  fields it reads. A component can also be a section of a project's page or
  a person's home, or a panel in an app; `@ikisci/component` describes what
  it's given. Use a component only for what Iki's own pages can't show.
- `tests/storage.yaml`: tests of the storage actions. `iki plugin test`
  runs each as a worker would and compares what it writes.
- `migrations/`: the database migrations for those types, made by
  `iki plugin makemigrations` and reviewed in pull requests like code.
- `.github/workflows/ci.yml`: checks every change, the way a deployment's
  build will.

The manifest format is in Iki's
[spec](https://github.com/yogesh-dhande/iki/tree/main/spec#readme). Editors that read
`yaml-language-server` comments (VS Code with the YAML extension, JetBrains)
complete and check it as you type.

## Work with a coding agent

Copy the whole template, including hidden directories. It includes three
skills that Codex and Claude Code can discover from the plugin repository.
With another agent, point it to the relevant `SKILL.md`. Keep the skill
directory intact so the agent can run its scripts.

- [`iki-write-plugin`](.agents/skills/iki-write-plugin/SKILL.md) starts a plugin and generates its migration.
- [`iki-test-plugin`](.agents/skills/iki-test-plugin/SKILL.md) checks action fixtures, migrations and the lab workflow.
- [`iki-upgrade-plugin`](.agents/skills/iki-upgrade-plugin/SKILL.md) preflights a versioned change and verifies the upgrade.

## Make it yours

1. Install the `iki` CLI at the version your deployments run:
   `uv tool install iki-cli==0.1.0`.
2. Rename the plugin in `plugin.yaml`: `namespace/name`, where the namespace
   is your lab's or company's (`acme` is a placeholder) and both are
   lowercase, with underscores (`acme/assays`). Plugins in different
   namespaces can share a short name, so pick a namespace that's yours alone.
   `iki` is Iki's own. The name is in every table it makes, so choose it
   before the first deployment. Other plugins' types are written with their
   full names (`iki/dna.DNASequence`); your own need none (`Sample`).
3. Replace the example types with yours. Keep only the dependencies you use.
   Besides fields, a type can have an identity (one registry entry per
   molecule, however it's written), states, lists of rows, references to one
   of several types and, for what the lab keeps, an `amount`: how much of
   each record there is, drawn and split into aliquots
   ([spec](https://github.com/yogesh-dhande/iki/tree/main/spec#amounts)).
4. Delete `migrations/` and make your first migration:

   ```sh
   iki plugin makemigrations . --with path/to/iki/plugins/dna
   ```

5. In `.github/workflows/ci.yml`, set `IKI_VERSION` to your deployments' Iki
   and `IKI_PLUGINS_SHA` to the `yogesh-dhande/iki` commit their lockfile pins.

## Change it

1. Edit the YAML. Increase a type's `schema_version` when its fields change.
2. Run `iki plugin validate . --with …` to check it.
3. Run `iki plugin makemigrations . --with …`. It asks whether a field that
   disappeared was renamed, so renaming keeps its data and history. It
   refuses to delete a field outright: mark it `removed: true` instead, so
   its history stays readable.
4. Run `iki plugin test . --with …` to run your actions' tests in `tests/`,
   with [`iki-worker`](https://github.com/yogesh-dhande/iki/tree/main/worker#readme)
   installed. Add a test for each change in what an action writes. With
   `--upgrade-from main --database postgres://…`, it also upgrades the
   entities of `main`'s tests with your new migrations, and rolls them
   back, as CI does on each pull request.
   For a backfill on existing records, use `MigrateValues` and give the
   expected fields in `after` on an `upgrade: true` fixture. Each record then
   carries the migration's reason. See the [backfill example](https://github.com/yogesh-dhande/iki/blob/main/cli/README.md#iki-plugin-test).
5. Commit the manifests, the new migration and the tests together, in one
   pull request.

## Deploy it

A deployment installs a plugin by pinning a commit in its lockfile
(`plugins.lock.yaml` in its config repository). For example, this entry
pins the Small molecule plugin from Iki's repository:

```yaml
plugins:
  - name: iki/small_molecule
    git: https://github.com/yogesh-dhande/iki.git
    path: plugins/small_molecule
    sha: b9b1632a67fda1d13a26f0a06b9c9edb18ffe19a
    state: active
```

For your own plugin, use its name, repository and commit. Production builds
refuse a pin to the template directory in Iki's repository or a configured
mirror. The [author examples](https://github.com/yogesh-dhande/iki/tree/main/examples/plugins)
describe how to adapt a worked recipe and test the copy. CI builds the
plugins into the deployment's image, checking them
together. The deployment migrates the database on its next rollout.

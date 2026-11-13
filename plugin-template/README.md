# Iki plugin template

A starting point for a lab's own Iki plugin: entity types, the fields it adds
to other plugins' types, and the CI that checks them. Copy this directory
into a repository of its own (or use it as a GitHub template), then make it
yours.

What's here:

- `plugin.yaml`: the plugin, its version, the plugins it depends on (here,
  Iki's `dna` plugin), and its settings.
- `types/sample.yaml`: an entity type, `example.Sample`, with references to
  its own type and to `dna.DNASequence`.
- `types/dna_sequence.yaml`: an extension adding two fields to
  `dna.DNASequence`. They live in this plugin's tables.
- `actions/assign_storage.yaml` and `scripts/storage.py`: an action that sets
  a sample's storage to the plugin's setting, and the command it runs.
- `actions/tidy_storage.yaml`: another, with the same script, that asks for a
  persistent machine (`lifecycle: persistent`): a quick action that runs
  often can go to a pod that's kept warm, rather than wait for one of its own.
- `environments/tools.yaml`: where that command runs. On the container tier,
  each run gets a pod with this image, pinned by its digest.
- `workflows/store_sample.yaml`: a workflow that assigns a sample's storage,
  then decodes it: two actions run one after the other, as one.
- `migrations/`: the database migrations for those types, made by
  `iki plugin makemigrations` and reviewed in pull requests like code.
- `.github/workflows/ci.yml`: checks every change, the way a deployment's
  build will.

The manifest format is in Iki's
[spec](https://github.com/yogesh-dhande/iki/tree/main/spec#readme). Editors that read
`yaml-language-server` comments (VS Code with the YAML extension, JetBrains)
complete and check it as you type.

## Make it yours

1. Install the `iki` CLI at the version your deployments run:
   `uv tool install iki-cli==0.1.0`.
2. Rename the plugin in `plugin.yaml`. The name is in every table it makes,
   so choose it before the first deployment: lowercase, with underscores
   (`acme_assays`).
3. Replace the example types with yours. Keep only the dependencies you use.
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
4. Commit the manifests and the new migration together, in one pull request.

## Deploy it

A deployment installs the plugin by pinning a commit in its lockfile
(`plugins.lock.yaml` in its config repository):

```yaml
plugins:
  - name: acme_assays
    git: https://github.com/acme/iki-plugin-acme-assays.git
    sha: <the commit>
    state: active
    settings: settings/acme_assays.yaml
```

Its CI builds the plugins into the deployment's image, checking them
together. The deployment migrates the database on its next rollout.

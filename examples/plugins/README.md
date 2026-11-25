# Plugin examples for lab authors

Use a worked plugin as the starting point for a procedure your lab owns. To
start a new plugin, the [plugin template](../../plugin-template/README.md)
includes a sample storage workflow, action tests and a GitHub Actions build.
Iki's installable first-party plugins live in [`plugins/`](../../plugins).

- [Automatic calibration impact tracing](calibration_tracing/README.md) opens
  impact cases for affected measurements when an engineer marks an instrument's
  calibration as failed.
- [Hosted model adapter](hosted_models/README.md) submits a job to a fictional
  provider and imports its result when the job finishes. Use it to adapt an
  asynchronous service your lab has approved.
- [Independent construct review](construct_review/README.md) adds a reviewed Assembly
  for labs that require someone other than the builder or a later editor to verify a
  matching comparison and lock the record.
- [Oligonucleotide design](oligo_design/README.md) registers a strand from its
  known bases and a lab’s standard pattern of modifications.

## Copy and name a plugin

Choose a directory containing `plugin.yaml`. Its README describes the lab
situation, the settings and actions to adapt, and the dependencies to check
out. The commands below use the template, which depends on `iki/dna`. From
the root of Iki's checkout, copy it into an empty directory for your new
repository. Keep the tests, scripts and hidden CI files. Start fresh migrations
for the new plugin identity.

```sh
IKI_SRC=$PWD
mkdir ../lab-storage
rsync -a --exclude=/migrations/ plugin-template/ ../lab-storage/
cd ../lab-storage
```

In `plugin.yaml`, change `acme/example` to your plugin's identity, such as
`acme/lab_storage`, and give it the title your users should see. The identity
determines table names. Update qualified references in the copied manifests,
scripts and tests before the first migration. Keep other plugins' names on
their types. Each workflow step calls an action in its own plugin.

## Test and build the copy

Use the same Iki CLI and worker version as the deployment. Install any tools
the example's actions use. The storage template invokes Python 3 as `python`,
so activate a Python environment that provides that command. Put `iki-worker`
on your PATH or set `IKI_WORKER` to its executable. Check out the dependencies at the commits
your deployment pins. Repeat `--with` for each direct and transitive dependency
when validating, making migrations and testing. Give `build` those directories
as positional arguments.

```sh
DNA="$IKI_SRC/plugins/dna"
iki plugin validate . --with "$DNA"
iki plugin makemigrations . --with "$DNA"
iki plugin makemigrations . --with "$DNA" --check --no-input
iki plugin test . --with "$DNA"
iki plugin build "$DNA" . --output ../build-lab-storage
```

Review the generated migration for your plugin's new name. Commit it with
the manifests and tests. In the [template's CI workflow](../../plugin-template/.github/workflows/ci.yml),
set `IKI_VERSION` and `IKI_PLUGINS_SHA` to the versions your deployment uses
and update its dependency paths for your plugin.

## Deploy the reviewed copy

In the deployment's `plugins.lock.yaml`, add the lab's plugin name,
its repository URL and the full commit SHA. Include `path` if the plugin
is in a subdirectory. Pin each dependency in the same lockfile and provide
the environment bindings and settings the recipe uses. Build from the
deployment config repository.

```sh
iki deploy build deployments/staging --output build/plugins --workers build/worker
```

Promote the tested commit using the [deployment guide](../../deploy/config-template/README.md).
Production builds refuse pins into the official teaching directories, including
pins to Iki mirrors. A test deployment that builds those sources in place uses
`--test-only`, with `--allow-local` as well for a local lockfile entry.

## Contribute an example

Put the plugin in its own directory under `examples/plugins/`, give it a
nonreserved name, and add a link and its use case to this index. Include a
README and tests a lab can keep after copying it. Record the source and
license of any sample data the recipe uses. From the root of the Iki checkout,
run the example checks used in CI.

```sh
uv run --locked python scripts/ci/check_plugin_examples.py
```

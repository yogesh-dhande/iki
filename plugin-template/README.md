# Iki plugin template

Use this template when your lab needs its first project record and an action that
updates it. The `Sample` type stores a name and a storage location. The
`assign_storage` action fills an empty storage field with the location you
choose. The default is `-80 °C freezer`. The action uses Python's standard
library and has no plugin dependencies.

## Copy and test the plugin

From the Iki source checkout, copy the whole directory into a new repository.
This includes the GitHub Actions workflow and the agent skills.

```sh
cp -R plugin-template ../sample-intake
cd ../sample-intake
git init -b main
```

In `plugin.yaml`, replace `acme/example` with a permanent name under your lab's
namespace, such as `yourlab/sample_intake`. Update its title, description and
license. Choose the name before the first deployment because it determines the
database table names. Edit `types/sample.yaml` for the fields you collect.
If you change the storage field or the action's location parameter, update
`actions/assign_storage.yaml`, `scripts/storage.py` and `tests/storage.yaml`
together.

Use the Iki CLI and worker release that matches your deployment. Set
`IKI_VERSION` in `.github/workflows/ci.yml` to the same version. Put the worker
binary for your machine on `PATH`. The local worker runs the action with Python 3
as `python`. If that command is missing, run `uv venv --python 3.13 ../iki-test-env`
and activate it with `. ../iki-test-env/bin/activate` before testing. [Worker releases](https://github.com/yogesh-dhande/iki/releases)

```sh
export IKI_VERSION=0.1.0  # use your deployment's release
uv tool install "iki-cli==$IKI_VERSION"
iki plugin validate .
rm migrations/0001_initial.py
iki plugin makemigrations .
iki plugin makemigrations . --check --no-input
iki plugin test .
```

Remove the copied initial migration only in a new, undeployed plugin.
Review the generated migration and commit it with the manifests, action, tests
and CI workflow. CI runs the same checks with an attested release worker. On a
pull request it also migrates records from the base commit's action tests and
checks that their values and history survive upgrade and rollback.

## Deploy it

Push your plugin repository. In the deployment's `plugins.lock.yaml`, add an
entry like this, replacing the repository URL and `sha` with your URL and the
full commit ID from `git rev-parse HEAD`.

```yaml
plugins:
  - name: yourlab/sample_intake
    git: https://github.com/yourlab/sample-intake.git
    sha: "<full commit ID>"
    state: active
```

Production builds refuse a pin to the template directory in Iki's repository
or a configured mirror. For other recipes to copy into your own repository,
see the [author examples](https://github.com/yogesh-dhande/iki/tree/main/examples/plugins).

Add an environment binding to the deployment's `compute.yaml`. Use the name of
a container-tier job template your deployment provides and the plugin name you
chose above. The template must meet the requirements in `environments/tools.yaml`.

```yaml
apiVersion: iki/v1
kind: EnvironmentBinding
environment: yourlab/sample_intake.tools
templates: [{ template: container-small }]
```

The deployment build checks the locked plugins together and packages them in
the image. Roll out the new configuration to apply the migration and create
the Sample table.

## Make the first record

Create an empty lab project in Iki and copy its project ID. Open a Python shell
with `uv run --with "ikisci==$IKI_VERSION" python`. Use your deployment URL and
the plugin name from your lockfile. Sign in to that project with permission to
read, write and run actions.

```python
import iki

iki.login("https://iki.example.org")
project_id = input("Project ID: ").strip()
plugin = "yourlab/sample_intake"

sample = iki.entities.create(
    f"{plugin}.Sample",
    {"name": "Rack control"},
    project=project_id,
    reason="Received at sample intake",
)
sample_id = sample["id"]
run = iki.run(
    f"{plugin}.assign_storage",
    project=project_id,
    inputs={"sample": sample_id},
    params={"location": "Freezer 2, rack A"},
    reason="Placed the sample in rack A",
)
iki.wait(run)
saved = iki.entities.get(sample_id, project=project_id)
assert saved["values"]["storage"] == "Freezer 2, rack A"
print(saved["hrid"], saved["values"]["storage"])
```

Omit `params` to use the default location. For a lab that prints storage labels,
the worked lesson includes the extension, setting, workflow and trigger to copy
and adapt. [Storage labels](https://github.com/yogesh-dhande/iki/tree/main/examples/plugins/storage_labels)

## Change an installed plugin

Keep the committed migrations. Increase a type's `schema_version` when its
fields change, generate the next migration with `iki plugin makemigrations .`,
and add action tests for changed behavior. For a backfill on existing records,
use `MigrateValues` and give the expected fields in `after` on an `upgrade: true`
fixture. Each record then carries the migration's reason. [Backfill example](https://ikisci.org/reference/cli/#iki-plugin-test)

The copied agent skills cover
[writing](.agents/skills/iki-write-plugin/SKILL.md),
[testing](.agents/skills/iki-test-plugin/SKILL.md) and
[upgrading](.agents/skills/iki-upgrade-plugin/SKILL.md) a plugin.

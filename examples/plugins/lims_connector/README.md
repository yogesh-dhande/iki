# Sync protein records with a LIMS

Use this example when a lab keeps protein records in a LIMS and wants to
import changes into Iki or send names back. The example plugin,
`acme/lims_connector`, adds a unique, linked `lims_id` field to
`iki/protein.Protein`. Installing the connector adds two actions and two optional project triggers.

| Action             | What happens                                                                                                                                                                                                                                                    |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `import_from_lims` | A manual run imports all records. The optional `import_nightly` trigger runs at 02:00 UTC and requests changes since the last successful scheduled run. The importer follows each page and matches by external ID so a repeated import updates the same record. |
| `send_to_lims`     | Sends the Iki record ID and current name to the matching external record. Records without a LIMS ID are skipped. The run retains the updates it sent in `sent.json`. The optional `send_renamed` trigger starts this action when the name changes.              |

## Try the example

From Iki's checkout, run the same example check as CI. It validates the
manifests with Protein and its DNA dependency, checks the migration, and
runs the connector tests.

```sh
uv run --locked python scripts/ci/check_plugin_examples.py examples/plugins/lims_connector
```

The [tests](tests/test_connector.py) use an in-process LIMS with synthetic
records to check pagination, the import watermark, retries and write-back.
For a full cluster run, `make dev-ci` from the Iki checkout runs both
actions against a [fake LIMS](https://github.com/yogesh-dhande/iki/blob/main/deploy/dev/fixtures/fake-lims.yaml)
using the [fixture profile](https://github.com/yogesh-dhande/iki/tree/main/deploy/dev#readme).

## Adapt the connector

Copy the plugin into an empty lab repository. Keep the tests and generate
the initial migration under the lab's identity. For example, from Iki's
checkout:

```sh
IKI_SRC=$PWD
mkdir ../lab-lims
rsync -a --exclude=/migrations/ examples/plugins/lims_connector/ ../lab-lims/
cd ../lab-lims
```

1. Rename `acme/lims_connector` in `plugin.yaml` to your lab's namespace and
   plugin name. Keep the type reference `iki/protein.Protein`. Update the
   title and description for your users.
2. Match your LIMS API in [scripts/lims.py](scripts/lims.py). The example API
   lists changes at `GET /api/v1/proteins`, with `modified_since` and `cursor`
   parameters, and returns `items` and `next_cursor`. Write-back uses
   `PATCH /api/v1/proteins/<id>`. Update the fake API and its assertions
   when changing this contract.
3. Choose the external ID, import mapping and fields your lab permits Iki
   to write. Keep the field link in [the extension](types/protein.yaml)
   and the origin link in the client consistent with the record URL. Set
   the trigger schedule and name-change policy to suit the project.

With the deployment's Iki CLI, `iki-spec` and pytest in the active environment, run the following
from the copied plugin. Check out DNA and Protein at the commits your
deployment will pin.

```sh
DNA="$IKI_SRC/plugins/dna"
PROTEIN="$IKI_SRC/plugins/protein"
iki plugin validate . --with "$DNA" --with "$PROTEIN"
iki plugin makemigrations . --with "$DNA" --with "$PROTEIN"
iki plugin makemigrations . --with "$DNA" --with "$PROTEIN" --check --no-input
python -m pytest tests/test_connector.py
iki plugin build "$DNA" "$PROTEIN" . --output ../build-lims
```

## Deploy the lab's copy

Commit the reviewed migration, scripts and tests in your lab's repository.
Pin its full commit SHA under the new plugin name in the deployment's
`plugins.lock.yaml`, alongside `iki/dna` and `iki/protein`. Set the
entry's `settings` to a file containing your LIMS's HTTPS hostname.

```yaml
lims_host: lims.acme.example
```

Bind `acme/lims_connector.lims-tools` to a backend compute template. Both actions
declare egress to `{settings.lims_host}` and request `LIMS_TOKEN`. Store the
token issued by your LIMS under `acme__lims_connector.LIMS_TOKEN` in the deployment's
run secret. Replace both qualified names to match your copy. The
[deployment guide](https://github.com/yogesh-dhande/iki/tree/main/deploy/config-template#readme) describes compute
bindings and the `runSecrets.existingSecret` Helm value.

Build from the deployment config repository with
`iki deploy build deployments/staging --output build/plugins --workers build/worker`.
Run a manual import in a staging project and check one external record ID
against its imported Protein. A project admin can then enable the import
schedule with a service account and enable rename write-back if the lab uses it.

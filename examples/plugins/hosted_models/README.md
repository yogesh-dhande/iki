# Connect a hosted model to Iki

Use this example when your lab submits jobs to a provider that returns results
asynchronously. The adapter, `lab/hosted_models`, sends one Protein record to
FoldCloud, a fictional service. Its `predict_structure` action parks while the
provider works. On resume it imports the result as a ProteinStructure and
records the model that produced it.

## Run the example

From an Iki checkout with the locked development dependencies and Wasm modules
installed, run the same check CI uses. It validates the manifest against
`iki/protein` and `iki/dna`, checks migrations and runs the plugin's pytest
case. The test supplies the provider's replies, including a completed job whose
result must be fetched in a separate request.

```sh
uv run --locked python scripts/ci/check_plugin_examples.py examples/plugins/hosted_models
```

For the complete integration, `make dev-ci` enables the
[fixture service](https://github.com/yogesh-dhande/iki/blob/main/deploy/dev/fixtures/fake-foldcloud.yaml). That check
exercises project approval, proxy authentication, a signed callback, resumed
output, provenance and charges. The run's API call record retains the reported
model version, request parameters, usage and price.

## Copy the adapter for your lab

From the Iki checkout, copy the entire directory, including its tests, to a new
lab repository. The commands below assume a copy named `acme/structure_service`.

```sh
IKI_SRC=$PWD
cp -R examples/plugins/hosted_models ../structure-service
cp LICENSE NOTICE ../structure-service/
cd ../structure-service
```

In `plugin.yaml`, set `name: acme/structure_service` and change the title,
description and `apis.foldcloud` declaration for your provider. Set its approved
host, credential and purpose, job paths, success and failure statuses, signature
scheme, model/version fields and usage units. If you rename the API, update its
key in the action, the worker's `IKI_API_…` environment lookups and the parked
job's `api` field. Keep `iki/protein` as the owner of the input and output types.

Adapt the request and result mapping in `scripts/predict.py`, the fields listed
under `apis.*.sends` in the action, and the responses and expected records in
`tests/test_prediction.py`. The proxy injects the provider credential when it
sends the request. The worker receives a callback URL for the job. Add a
provider result to the tests for each output form your lab accepts.

Install the Iki CLI version your deployment runs and pytest in your test
environment. Check out the dependency commits your lab uses, then run the
following commands in your copied repository.

```sh
DNA="$IKI_SRC/plugins/dna"
PROTEIN="$IKI_SRC/plugins/protein"
iki plugin validate . --with "$DNA" --with "$PROTEIN"
iki plugin makemigrations . --with "$DNA" --with "$PROTEIN" --check --no-input
python -m pytest tests/test_prediction.py
iki plugin build "$DNA" "$PROTEIN" . --output ../build-structure-service
```

## Deploy the copy

Pin the lab repository and full commit SHA in the deployment's
`plugins.lock.yaml`, alongside the pinned Protein and DNA plugins. Set
`fold_api_host` in the plugin's settings file. Bind
`acme/structure_service.model-tools` to a backend compute template and set an
`ApiPrice` for `acme/structure_service.foldcloud` using the provider's prices.

Provision the key and callback signing secret under the renamed plugin's
secret prefix, such as `acme__structure_service.FOLDCLOUD_API_KEY` and
`acme__structure_service.FOLDCLOUD_WEBHOOK_SECRET`. Build the server and worker
from the deployment configuration and stage the image. An Org Admin approves
the destination for each project in **Admin → Outside APIs**. Each person
launching the action can review the fields that will be sent.

```sh
iki deploy build deployments/staging --output build/plugins --workers build/worker
```

See the [deployment guide](https://github.com/yogesh-dhande/iki/blob/main/deploy/config-template/README.md) and the
[outside API decision](https://github.com/yogesh-dhande/iki/blob/main/docs/adr/0130-outside-apis-are-approved-per-project-and-called-through-the-proxy.md).

# Sample storage labels

Use this plugin as a starting point if your lab has short storage codes on sample
sheets and wants the same readable location on a record and its tube label.
For example, a sample arriving with `ln2` should show “Liquid nitrogen dewar.”
These labels describe storage. Track tube placement, moves and balances with Inventory.

## Try the workflow

1. Create a **Sample** with a name and kind. Enter a storage code, or leave
   storage empty to use the deployment's `default_storage` setting.
2. Launch **Store a sample**. **Assign default storage** fills an empty location
   (the supplied default is `-80`). **Decode storage codes** reads the same
   sample after the first step commits and expands recognized codes using the
   reference volume. The example expands `-80` to “-80 °C freezer.” Full names
   and unrecognized locations keep their original text.
3. Open **Label** on the sample or choose **Show as → Labels** in the samples
   grid. The label shows the sample's ID, name, kind and location. For a DNA
   sample, link its construct to a DNA sequence. The extension in
   `types/dna_sequence.yaml` adds a construct ID and verification flag to that
   sequence's project record.

Both workflow steps call actions declared by this plugin. The DNA dependency
supplies a type. A project admin can also enable **Assign storage to new
samples** in project triggers. It queues the assignment action on creation
and requires approval for its container run. Run the workflow to expand a code.

## Copy and customize

From an Iki checkout at the same revision as your deployment:

```sh
cp -R examples/plugins/storage_labels ../my-storage-labels
cd ../my-storage-labels
```

Choose the lab's permanent `namespace/name` in `plugin.yaml`. Change the
sample fields and ID prefix for your labels, the default code for unlabelled
samples, and the code list and data version in `setup/storage_codes.sh`. Change
the web component and its declared fields together when changing the label.
The component's source is `web/src/sample-label.js`, which asks Iki for the
next page with [@ikisci/component](../../../web/packages/component)'s `more`.
`web/` is a package of its own: run `npm install` there once and commit the
`package-lock.json` it writes, then `npm run bundle` after each change to
rebuild `components/sample-label.js`, the one module Iki serves. The
template's CI runs `npm run check`, which fails if they don't match.
Update the examples and expected labels in `tests/` with values from your lab.
The manifest also declares `tube_scan`, a rack scanner's export. Its header lets
Iki recognize a scan even when the filename has lost its extension. To accept
it in a file field or action file parameter, set
`media_types: [acme/storage_labels.tube_scan]`, with your copy's name in
that ID. Change the label, media type, extension and signature to match your
scanner's files, or remove the format if your lab has no scanner.

Set `IKI_CHECKOUT` to the absolute path of the Iki checkout. Activate its
locked Python environment (with the CLI, spec and pytest), install the matching
`iki-worker` on your PATH, and build the checkout's Wasm with `make test-wasm`.
In the new copy, replace the example's migration before first deployment.
Generating a fresh migration gives the lab's plugin its own tables and history.

```sh
DNA="$IKI_CHECKOUT/plugins/dna"
rm migrations/0001_initial.py
iki plugin makemigrations . --with "$DNA"
iki plugin validate . --with "$DNA"
iki plugin makemigrations . --with "$DNA" --check --no-input
iki plugin test . --with "$DNA"
pytest tests/test_workflow.py
```

The action fixture checks the configured default and preservation of an existing
location through the local worker. The workflow test builds the code list in a
temporary volume and checks the assignment and decoding steps against that data.
Keep both tests with the copied plugin and run them in its CI.

## Deploy the lab's copy

Commit the plugin to your lab's repository. Add its new name, repository URL,
plugin directory (if nested), full commit SHA and `state: active` to the
deployment's `plugins.lock.yaml`. Include the deployment's pinned `iki/dna`.
To override the default, point the lab plugin's lock entry at a settings YAML
file containing, for example, `default_storage: "ln2"`.

In the deployment's `compute.yaml`, bind the lab plugin's `tools` environment to
a container template. Bind its `reference` environment to a container template
that provides the `storage-codes` volume. For a Kubernetes cluster with default
storage and snapshot classes, that template can use:

```yaml
volumes:
  storage-codes: { strategy: clone_from_snapshot }
```

Run `iki deploy build path/to/deployment --output build/plugins`. Build the
deployment images from the pinned plugin sources and roll them out. A Compute admin requests a
setup build for the `reference` environment and a second Compute admin
approves it. Wait for the build to finish before running **Store a sample**.
The decoder reads the ready version at `/data/storage-codes/codes.json`,
and the run records the version it used. Request a new build after updating
the lab's code list.

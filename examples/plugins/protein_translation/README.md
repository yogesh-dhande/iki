# Translate a sequence and characterize its protein

Use this plugin when your lab routinely translates a registered DNA sequence
and wants the resulting protein’s theoretical properties in the same run.
In a project, open **Workflows**, choose **Run a workflow**, then select
**Translate and characterize**. Select the DNA record, reading frame, and
genetic code. Select **Complete coding sequence** when the input should be
checked from its start codon through its terminal stop.

The first step runs the local `translate` action. It registers the translated
protein and records the DNA as its source. The second step passes
`$steps.translate.created.iki/protein.Protein` to the local
`compute_properties` action. Open the workflow run to inspect each step,
then open the protein to see its length, molecular weight, isoelectric point,
extinction coefficients, GRAVY, hydropathy profile and instability index.
The action reports which values it could calculate for ambiguous residues.

## Run the tests

Use the Iki CLI and worker that match your deployment. Put `iki-worker` on
your path or set `IKI_WORKER` to its executable. Set `IKI_SRC` to a checkout
containing the pinned DNA and Protein plugins. From this example directory,
activate a Python 3.12 environment that provides the `python` command, then run

```sh
uv pip install --require-hashes -r requirements.txt
iki plugin validate . --with "$IKI_SRC/plugins/dna" --with "$IKI_SRC/plugins/protein"
iki plugin makemigrations . --check --no-input --with "$IKI_SRC/plugins/dna" --with "$IKI_SRC/plugins/protein"
iki plugin test . --with "$IKI_SRC/plugins/dna" --with "$IKI_SRC/plugins/protein"
```

The two action fixtures use the same short synthetic example. Translating
`ATGTGGTATTAA` with the standard code yields `MWY` and links the protein
to its source. The properties fixture checks its length and extinction
coefficients. To exercise the complete workflow in a test project, register
that DNA as a linear sequence, use frame 1 and genetic code 1, select
**Complete coding sequence**, and open the second step’s input. It should be
the protein created by the first step.

## Adapt and deploy a lab copy

1. Copy this directory to a repository your lab owns. In `plugin.yaml`,
   change `examples/protein_translation` to your own namespace and plugin
   name. Keep `iki/dna` and `iki/protein` as dependencies so your projects
   use the existing registry types.
2. Set the workflow’s defaults to match the sequences you accept. To offer
   reverse-strand translation, expose the local translation action’s
   `strand` parameter in the workflow and bind it in the `translate` step.
   Each workflow step calls an action declared in its own plugin.
   The action manifests, worker command and hashed environment here are
   local copies of the two calculations from Iki’s Protein plugin. When
   upgrading, review changes to those calculations and keep your action
   fixtures in CI.
3. Pin your reviewed repository URL, full commit SHA, plugin identity, and
   any subdirectory in your deployment’s `plugins.lock.yaml`. Include DNA
   and Protein in the lock. In `compute.yaml`, bind your copy’s
   `your_namespace/your_plugin.protein-tools` environment to a backend
   worker template. Build the deployment and run the synthetic sequence in
   staging before rolling it out to your lab.

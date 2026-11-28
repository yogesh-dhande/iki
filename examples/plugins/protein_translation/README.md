# Translate a sequence and characterize its protein

Use this plugin when your lab routinely translates a registered DNA sequence
and wants the resulting protein’s theoretical properties in the same run.
In a project, open **Workflows**, choose **Run a workflow**, then select
**Translate and characterize**. Select the DNA record, reading frame, and
genetic code. Select **Complete coding sequence** when the input should be
checked from its start codon through its terminal stop.

The workflow is the only part of this plugin. Both of its steps run actions of
Iki’s Protein plugin, named in full. The first step runs
`iki/protein.translate`. It registers the translated protein and records the
DNA as its source. The second step passes
`$steps.translate.created.iki/protein.Protein` to
`iki/protein.compute_properties`. Open the workflow run to inspect each step,
then open the protein to see its length, molecular weight, isoelectric point,
extinction coefficients, GRAVY, hydropathy profile and instability index.
The action reports which values it could calculate for ambiguous residues.

Each step’s run is Protein’s. It runs in Protein’s environment, at the Protein
plugin version your deployment installed, with Protein’s settings. It can
change only what Protein’s action declares. The run page names this
workflow, and the plugin it belongs to, beside the action.

## Check it

Use the Iki CLI that matches your deployment. Set `IKI_SRC` to a checkout
containing the pinned DNA and Protein plugins. From this example directory, run

```sh
iki plugin validate . --with "$IKI_SRC/plugins/dna" --with "$IKI_SRC/plugins/protein"
iki plugin makemigrations . --check --no-input --with "$IKI_SRC/plugins/dna" --with "$IKI_SRC/plugins/protein"
IKI_CHECKOUT="$IKI_SRC" python -m pytest tests
```

The validator checks each step against the Protein action it names: that
every input comes from the workflow or an earlier step, of a type the action
takes, and that every param fits. `tests/test_workflow.py` checks that both
steps still name Protein’s actions, and that the protein the first creates is
the one the second measures. Protein’s own action tests cover the two
calculations. To exercise the complete workflow in a test project, register
`ATGTGGTATTAA` as a linear sequence, use frame 1 and genetic code 1, and select
**Complete coding sequence**. The first step should create `MWY`, and the
second step’s input should be that protein.

## Adapt and deploy a lab copy

1. Copy this directory to a repository your lab owns. In `plugin.yaml`,
   change `acme/protein_translation` to your own namespace and plugin
   name. Keep `iki/dna` and `iki/protein` as dependencies. A step can run
   only the actions of a plugin listed under `dependencies`.
2. Set the workflow’s defaults to match the sequences you accept. To offer
   reverse-strand translation, add a workflow param and bind it to
   `iki/protein.translate`’s `strand` in the `translate` step.
3. Pin your reviewed repository URL, full commit SHA, plugin identity, and
   any subdirectory in your deployment’s `plugins.lock.yaml`. Include DNA
   and Protein in the lock. The steps run in Protein’s `protein-tools`
   environment, which the deployment already binds for Protein. Build the
   deployment. The build checks the workflow against the Protein release in
   the lock, so a release that changes either action fails the build, not
   the workflow. Run the synthetic sequence in staging before rolling it out
   to your lab.

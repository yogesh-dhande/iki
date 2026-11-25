# Register oligonucleotides from a modification pattern

A lab that repeatedly enters strands with the same backbone, sugar pattern or
end groups can offer those choices on one form. The scientist enters the
known bases and selects a pattern. The action writes the corresponding HELM
and registers the strand and its monomers in the existing Oligo registry. A
strand already registered under the same canonical HELM keeps its identity.

## Try the example

From an Iki checkout with its Python environment active and a matching
`iki-worker` on your path, run:

```sh
iki plugin validate examples/plugins/oligo_design --with plugins/oligo
iki plugin test examples/plugins/oligo_design --with plugins/oligo
```

The two cases in `tests/design.yaml` check the HELM and calculated properties of
a published gapmer and a fluorescent probe. The local action runner requires
the packages in `requirements.txt`. Iki’s locked development environment includes
them.

## Adapt it for your lab

1. Copy this directory to a repository your lab owns. Rename
   `acme/oligo_design` in `plugin.yaml` to your lab’s namespace and plugin name.
   Keep the qualified `iki/oligo` type names in the action and fixtures so that
   the records go into the same registry.
2. In `actions/design_oligonucleotide.yaml`, set the choices, defaults and labels
   your lab uses. Keep their HELM symbol mappings in `scripts/design.py` in sync.
   Add an action fixture with an independently checked HELM and expected
   properties for each pattern you change.
3. Use Python 3.12 and install the pinned environment with
   `uv pip install --require-hashes -r requirements.txt`. Run the validate and
   test commands in your CI, replacing the example path with `.` and the
   `--with` path with a checkout of the Oligo version your lab deploys.
4. Commit the copy and pin its repository URL, full commit SHA and renamed
   plugin identity in your deployment’s `plugins.lock.yaml`, alongside
   `iki/oligo`. Bind `your_namespace/your_plugin.oligo-tools` to a backend
   worker in `compute.yaml`, then build and roll out the deployment. Project
   members can run **Design an oligonucleotide from its bases**.

The worker runs the plugin from its own directory. Its local registration code,
HELM reader, chemistry routines, dictionary and hashed Python requirements come
from [Iki Oligo 0.2.0](https://github.com/yogesh-dhande/iki/tree/84bbabd1678f3ad44516c49bd96955ba4edde9a4/plugins/oligo).
When updating the dependency, review those files together and run the fixtures.
The [dictionary sources and MIT notice](data/README.md) accompany the copy.

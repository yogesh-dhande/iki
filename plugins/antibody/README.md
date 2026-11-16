# antibody

Iki's antibody plugin: antibodies, assembled from the proteins that are their
chains, what their variable domains say about them, and their predicted
structures.

- **`antibody.Antibody`** (`ABY-000001`): an antibody, by its chains.
  - A whole antibody (`igg`) or a Fab (`fab`) has a heavy chain and a light
    chain. A single domain (`vhh`, a camelid's or a nanobody) or both variable
    domains on one chain (`scfv`) has one chain, its `heavy_chain`.
  - Its identity is its heavy chain and its light chain, if it has one, so
    assembling the same chains again reuses it. A VHH and an antibody with the
    same heavy chain and a light chain are different antibodies
    ([ADR 0104](../../docs/adr/0104-an-identity-can-name-an-optional-field.md)).
  - It keeps its format, its isotype (a constant region's, so only `igg` and
    `fab` take one) and what it binds (`target`).

- **Proteins' numbering** (an extension of `protein.Protein`): what numbering a
  chain finds, in the plugin's own table.
  - Its variable domains (`variable_domains`), in order along the chain: each
    residue's number and insertion code, and where each domain starts and
    ends. An scFv's chain has a heavy and a light one (`heavy_and_light`).
  - Its CDRs (`cdr_h1` to `cdr_l3`), in the scheme it was numbered in: IMGT's,
    Kabat's or Chothia's.
  - The species whose germline V gene it's closest to (`species`), such as
    `alpaca` for a nanobody.
  - Its humanness: the human germline V gene each domain is closest to, and how
    much of its V region is the same (`heavy_germline_identity`,
    `light_germline_identity`). A human antibody is usually above 90%, a
    humanized one above 80%.

- **Predicted structures' errors** (an extension of `protein.ProteinStructure`):
  for a structure `predict_structure_with_immunebuilder` made, how far ImmuneBuilder's models
  disagree (`predicted_error`, `cdr_h3_predicted_error`).

Its chains are [protein plugin](../protein) proteins, so their properties,
alignments and structures are the protein plugin's. The plugin depends on it,
and on the [dna plugin](../dna), which the protein plugin depends on.

## Actions

| Action                                 | What it does                                                                 | Tier      |
| -------------------------------------- | ---------------------------------------------------------------------------- | --------- |
| `assemble`                             | Registers the antibody its chains make, or reuses the one in Iki.            | backend   |
| `number_chains`                        | Numbers proteins' variable domains with ANARCI: CDRs, species and humanness. | container |
| `predict_structure_with_immunebuilder` | Predicts an antibody's structure with ImmuneBuilder, and how sure it is.     | container |

`assemble` checks that the format and the chains agree: an IgG or a Fab needs a
light chain, a VHH or an scFv has none, and only a format with a constant
region has an isotype. It names the antibody after its chains unless it's given
a name.

`number_chains` runs [ANARCI](https://github.com/oxpig/ANARCI) (BSD-3-Clause)
with HMMER's hmmscan, in the plugin's `numbering-tools` image
(`images/numbering-tools`), which Iki's publish workflow builds, scans and
signs. ANARCI numbers each chain twice: with every species' models and
germlines, for the numbering and the closest species, then with human
germlines alone, for humanness. Its germlines are
[IMGT's](https://www.imgt.org) (CC BY 4.0): cite IMGT when you publish what
they found. A protein without an antibody domain, or with a residue ANARCI
can't read (such as U), is left as it was, and the run says which.

`predict_structure_with_immunebuilder` runs [ImmuneBuilder](https://github.com/oxpig/ImmuneBuilder)
(BSD-3-Clause) in the plugin's `structure-tools` image (`images/structure-tools`),
with its trained weights ([Zenodo 7258553](https://zenodo.org/records/7258553),
CC BY 4.0: cite its paper when you publish its structures). Its input includes the
antibody's chains (ADR 0105), so it reads their sequences as they were when it was
launched. ABodyBuilder2 models a heavy and a light chain's variable domains, from
an IgG's or a Fab's two chains or an scFv's one; NanoBodyBuilder2 a VHH's single
domain. Each runs four models and keeps the one closest to the others, refined
with OpenMM. It makes a `protein.ProteinStructure` of the antibody's chains
(chain IDs H and L), derived from the antibody, with how far the four models
disagree: `predicted_error` over every residue and `cdr_h3_predicted_error` over
CDR-H3, in Å, and each residue's in the file as its B-factor. Refining isn't
exactly repeatable, so the action isn't deterministic: running it again makes a
new structure, with the same estimated errors. On four CPUs an antibody takes
about 15 seconds.

The image carries OpenMM's licences, which its wheel doesn't: OpenMM is MIT, and
the OpenCL platform its wheel carries, which these runs don't use, LGPL-3.0.

## Tests

```console
iki plugin test plugins/antibody --with plugins/dna --with plugins/protein
```

`server/tests/test_antibody_numbering.py` runs `number_chains`' script with a
stand-in ANARCI that answers what the real one said in the image
(`server/tests/data/record_anarci.py` records it). The image's `check.py`
runs the real one on trastuzumab, and the dev cluster's end-to-end check
numbers trastuzumab's chains in a pod of their own.

`server/tests/test_antibody_structure.py` runs `predict_structure_with_immunebuilder`'s script with a
stand-in ImmuneBuilder. The structure-tools image's `check.py` models
trastuzumab's Fv and the GFP enhancer nanobody with the real one, and the
end-to-end check predicts trastuzumab's structure in a pod of its own.

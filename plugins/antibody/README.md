# antibody

Iki's antibody plugin: antibodies, assembled from the proteins that are their
chains.

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

Its chains are [protein plugin](../protein) proteins, so their properties,
alignments and structures are the protein plugin's. The plugin depends on it,
and on the [dna plugin](../dna), which the protein plugin depends on.

## Actions

| Action          | What it does                                                                 | Tier      |
| --------------- | ---------------------------------------------------------------------------- | --------- |
| `assemble`      | Registers the antibody its chains make, or reuses the one in Iki.            | backend   |
| `number_chains` | Numbers proteins' variable domains with ANARCI: CDRs, species and humanness. | container |

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

## Tests

```console
iki plugin test plugins/antibody --with plugins/dna --with plugins/protein
```

`server/tests/test_antibody_numbering.py` runs `number_chains`' script with a
stand-in ANARCI that answers what the real one said in the image
(`server/tests/data/record_anarci.py` records it). The image's `check.py`
runs the real one on trastuzumab, and the dev cluster's end-to-end check
numbers trastuzumab's chains in a pod of their own.

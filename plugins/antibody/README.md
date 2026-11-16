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

Its chains are [protein plugin](../protein) proteins, so their properties,
alignments and structures are the protein plugin's. The plugin depends on it,
and on the [dna plugin](../dna), which the protein plugin depends on.

## Actions

| Action     | What it does                                                      | Tier    |
| ---------- | ----------------------------------------------------------------- | ------- |
| `assemble` | Registers the antibody its chains make, or reuses the one in Iki. | backend |

`assemble` checks that the format and the chains agree: an IgG or a Fab needs a
light chain, a VHH or an scFv has none, and only a format with a constant
region has an isotype. It names the antibody after its chains unless it's given
a name.

## Tests

```console
iki plugin test plugins/antibody --with plugins/dna --with plugins/protein
```

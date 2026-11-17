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

- **Proteins' sequence liabilities** (the same extension of `protein.Protein`):
  the motifs a developability screen reports in a numbered chain
  (`liabilities`), each with where it is, whether it falls in a CDR or in
  framework, and how severe published screens call it; how many of each kind it
  has (`liability_counts`); and how many in all (`liability_count`, indexed, so
  chains can be sorted and filtered by it). A chain with none has
  `liability_count` 0, which is not the same as one that hasn't been screened
  (it has none of the three fields).

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
| `find_liabilities`                     | Finds the sequence liabilities a developability screen reports in a chain.   | backend   |
| `predict_structure_with_immunebuilder` | Predicts an antibody's structure with ImmuneBuilder, and how sure it is.     | container |

`number_chains` and `predict_structure_with_immunebuilder` list the software
they run, with the versions their images install, so what they make is recorded
as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)):
ANARCI 2026.2.13.2 and HMMER 3.4, and for the structure ImmuneBuilder 1.2,
OpenMM 8.6.1 and PDBFixer 1.12.0 as well. `assemble` and `find_liabilities` are
the plugin's own code, in pure Python, so they run none.

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

### Sequence liabilities

`find_liabilities` reads a chain's liabilities off the numbering `number_chains`
stored, so a chain has to be numbered first; one that hasn't been is left as it
was, and the run says so. It's the plugin's own code (`scripts/liabilities.py`),
pure Python on the stored numbering, so it runs on the backend tier and lists no
software. Each liability it reports gives its `kind`, the residues that make it
(`motif`), where it is (`position`, the index in the sequence from 1, and
`number`, its number in the scheme the chain was numbered in, insertion code
included), whether it falls in a CDR (`region`: `cdr_h1` … `cdr_l3`) or in
`framework`, and its `severity`.

These are the rules it implements, and nothing else. The motifs and severities
are the ones published developability screens use, chiefly the liability table
of [LAP](https://doi.org/10.1371/journal.pcbi.1011881) (Satława et al., _PLoS
Comput Biol_ 20(3):e1011881, 2024, Table 1), whose scope per motif (a CDR, or
the whole variable fragment) this follows:

| Kind                | Motif                                   | Where          | Severity                   |
| ------------------- | --------------------------------------- | -------------- | -------------------------- |
| `n_glycosylation`   | `N-X-S/T`, X not proline                | A whole domain | high                       |
| `deamidation`       | `NG`, `NS`, `NT`, `NN`                  | A CDR          | high for `NG`, else medium |
| `isomerisation`     | `DG`, `DS`, `DT`, `DD`                  | A CDR          | high for `DG`, else medium |
| `fragmentation`     | `DP`                                    | A CDR          | high                       |
| `oxidation`         | `M`, `W`                                | A CDR          | medium                     |
| `unpaired_cysteine` | `C` away from the domain's disulphide   | A whole domain | high                       |
| `pyroglutamate`     | `Q` or `E` at the chain's first residue | The N-terminus | low                        |

- **N-glycosylation**: the sequon an N-linked glycan is added at. LAP's `Ngly`,
  over the variable fragment; here, within a variable domain ANARCI numbered, so
  a sequon that runs past a domain's end isn't counted.
- **Deamidation** and **isomerisation**: `NG` and `DG` are ranked above the
  others because they're the most labile, as measured across 131 clinical-stage
  antibodies: deamidation falls off in the order NG, NS, NT, and DG isomerises
  more than DS, which isomerises more than DD or DT
  ([Lu et al.](https://doi.org/10.1080/19420862.2018.1548233), _mAbs_
  11(1):45-57, 2019, whose hot spots are Kabat H54 (NG), H98 (DG) and CDR-L1).
  LAP puts `N[GS]` together as high; this follows the measured order instead and
  leaves `NS` at medium. Both sources count them in the CDRs alone, so a motif
  in framework isn't reported.
- **Unpaired cysteines**: every cysteine in a variable domain other than the two
  that make its disulphide, at IMGT 23 and 104 (Kabat's and Chothia's numbering
  of the same residues, heavy 22 and 92, light 23 and 88). If either of those
  positions isn't a cysteine, the other has nothing to pair with, so it's
  reported too. LAP's `xCys` and `mCys`.
- **Oxidation**: methionine and tryptophan in a CDR. LAP's `MetOx` and `TrpOx`.
- **Fragmentation**: `DP` in a CDR. LAP's `FragH`.
- **Pyroglutamate**: a glutamine or glutamate at the chain's first residue
  cyclises, which makes charge variants rather than changing what the antibody
  does, so it's low ([Liu et al.](https://doi.org/10.1074/jbc.M110.185041),
  _J Biol Chem_ 286(13):11211-11217, 2011). It isn't in LAP's table.

The [Therapeutic Antibody Profiler](https://doi.org/10.1073/pnas.1810576116)
(Raybould et al., _PNAS_ 116(10):4025-4030, 2019) reports such motifs alongside
its five developability guidelines; those guidelines are structural, and this
action doesn't compute them. LAP's own `Hydro` (`NP`), `FragM` (`TS`), low
`[STK]N` deamidation and integrin-binding motifs aren't implemented.

A liability is a site to look at, not a defect: what a screen reports needs
measuring before it's believed. Which scheme a chain was numbered in decides
where its CDRs are and how a liability is numbered, so screening the same chain
after renumbering it can report different regions and numbers for the same
residues.

`liabilities` and `liability_counts` are JSON, which a chain's page shows as it
is, under its fields; `liability_count` is an indexed integer, so chains can be
filtered and sorted by how many they have, in the grid and through the API.

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
new structure, with the same estimated errors. An antibody takes about a minute on one CPU, and about 15 seconds on four.

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

`tests/find_liabilities.yaml` screens made-up domains, where each rule is in one
place, and `server/tests/test_antibody_liabilities.py` screens real chains on
the numbering ANARCI gave them, through the same stand-in. The dev cluster's
end-to-end check screens trastuzumab's chains after numbering them.

`server/tests/test_antibody_structure.py` runs `predict_structure_with_immunebuilder`'s script with a
stand-in ImmuneBuilder. The structure-tools image's `check.py` models
trastuzumab's Fv and the GFP enhancer nanobody with the real one, and the
end-to-end check predicts trastuzumab's structure in a pod of its own.

# complex

Iki's complex plugin: protein complexes of any parts, and every complex's
formula and masses, whatever its type.

A complex is `iki/core.Complex`'s
([ADR 0161](../../docs/adr/0161-a-complex-is-its-parts-and-their-copies.md)),
an abstract type every complex type extends: its `components` are its parts,
each a registry entry (a protein, a nucleic acid, a small molecule, another
complex) with its `copies` and its `role`, and its identity is its parts and
their copies, whatever their roles or order. So one molecule is one registry
entry, whichever type registered it: hemoglobin's two α chains, two β chains
and four hemes are one hemoglobin, and an antibody's chains, two copies of
each, registered as a protein complex are found to be that antibody. Its
`bonds`, a registry fact, join one copy's residue to another's: a disulfide two
cysteines.

- **`iki/complex.ProteinComplex`** (`PCX-000001`): a complex of any parts in any
  roles, loosely checked: each part once, with its copies, and its bonds within
  its parts. For what no specific type fits, such as hemoglobin
  (`2xHBA1:2xHBB` with its hemes), a receptor with its ligand or a viral capsid.
  A lab that wants its parts' roles held to a format chooses a specific type,
  such as the antibody plugin's, or adds its own
  ([ADR 0164](../../docs/adr/0164-a-subtype-says-which-roles-its-rows-take.md)).
  - Its `complex_portal_id` (a registry fact) is its
    [Complex Portal](https://www.ebi.ac.uk/complexportal) accession, such as
    `CPX-2158` for hemoglobin A, and its `structure` the structure that shows
    it.

- **Every complex's composition**, on `iki/core.Complex`, so every type has
  it: its `molecular_formula` (Hill order), `molecular_weight` (average, in u,
  shown in ku), `monoisotopic_mass` and `chain_count`. They're derived from its
  parts and copies (`registry: derived`): its parts as they are, each
  cysteine's thiol free. Its bonds are a fact that a registrar may correct, so
  what they take off (two hydrogens, 2.016 u, for each disulfide) is worked out
  where they're shown: the Composition tab and the run's message.

## Actions

| Action                | What it does                                                    | Tier    |
| --------------------- | --------------------------------------------------------------- | ------- |
| `compute_composition` | Computes a complex's formula, masses and chain count, any type. | backend |

The trigger `compute_new_complex_composition` runs it on each complex
registered, of any type, batching up to 100 a run. A complex's run reads its
parts (`include: [components.part]`), at the revisions they have then:

- a protein's formula is its residues', each its amino acid's less a water,
  and one water for its termini. Selenocysteine (U) is C3H5NOSe and pyrrolysine
  (O) C12H19N3O2. An ambiguous residue (B, Z, J or X) has no one formula, so a
  complex with one is named and left out;
- any other part's is its entry's `molecular_formula`: a compound's, an
  oligonucleotide's, a duplex's or another complex's, which this action
  computed when it was registered. A part without one yet, or with a charged
  one, is named and the complex left out.

Its average mass is its formula's with the standard atomic weights Biopython
has (`Bio.Data.IUPACData.atom_weights`), as the protein plugin's molecular
weights are, and its monoisotopic mass with each element's most abundant
isotope's mass (AME2020: Wang et al., _Chin Phys C_ 45:030003, 2021). Its chain
count is each part's chains times its copies: a protein, nucleic acid or
oligonucleotide is one, a duplex two, a complex its own count, a compound none.
The run says what its disulfides make it: _"With its 16 disulfides:
C6472H10014N1730O2023S46, 145,900.13 u average and 145,810.1054 u
monoisotopic"_. Its other bonds (isopeptides, thioethers) aren't weighed:
what each takes off depends on its chemistry.

What it computes, checked against published values:

| Complex                         | Its parts as they are                                          | Published                                                                                                                                                                                                 |
| ------------------------------- | -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hemoglobin A (CPX-2158)         | C2954H4508Fe4N780O806S12, 64,452.3 u                           | Complex Portal: 64 kD. Its α chain alone, 15,126.16 u: 15,126.4 Da by electrospray (Rai, Landin and Green, Waters application note, _The analysis of human haemoglobin variants using mass spectrometry_) |
| Amivantamab (KEGG D11894)       | With its 16 disulfides, C6472H10014N1730O2023S46, 145,900.13 u | KEGG DRUG: C6472H10014N1730O2023S46, 145,900.22 (average), 145,810.1055 (exact); ours 145,810.1054                                                                                                        |
| Trastuzumab Fab–HER2 (PDB 1N8Z) | C4980H7745N1389O1538S65, 113,766.39 u, 3 chains                | Its chains as Biopython weighs them; RCSB's entity weights are 0.1% higher                                                                                                                                |

Formulas from KEGG DRUG's chains (amivantamab's, mosunetuzumab's,
aflibercept's) are KEGG's exactly; average masses differ in their last
figures, as KEGG's atomic weights differ from Biopython's.

## Components

**Composition**, a tab on every complex's page: its parts with their roles and
copies; its formula and masses as computed, and with its disulfides; and its
bonds, each from a copy's residue to another's. It's named for screen readers
as it's drawn for the eye.

## Tests

```console
iki plugin test plugins/complex --with plugins/dna --with plugins/protein --with plugins/small_molecule --with plugins/antibody
```

`tests/compute_composition.yaml` computes hemoglobin A, amivantamab with its
disulfides, trastuzumab's Fab bound to HER2 (a complex of a complex) and one
with an ambiguous residue. `server/tests/test_complex_components.py` shows
emicizumab's composition in each browser, in light and dark, with axe's WCAG
2.2 AA checks; `server/tests/test_rows.py` checks a complex's parts and bonds
with the test plugin's own complex type; and `server/tests/test_first_party_plugins.py`
registers each complex type and finds them all through `iki/core.Complex` and
the graph. The dev cluster's end-to-end check waits for the trigger to compute
emicizumab's and etanercept's compositions.

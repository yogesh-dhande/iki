# complex

Iki's complex plugin: protein complexes of any parts, and every complex's
formula, masses and molar extinction coefficient at 280 nm (ε280), whatever its type.

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
cysteines. `bonds_from` says where they come from: `pattern`, placed where its
kind places them, not read from this molecule; `curated`, a source that records
this molecule's, such as a database entry or a solved structure; or `observed`,
seen in it, as by non-reduced peptide mapping. Curated and observed bonds name
their source as a link (`bonds_source`), on every type of complex
([ADR 0267](../../docs/adr/0267-a-complexs-bonds-say-where-they-come-from.md)).

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
  shown in ku), `monoisotopic_mass`, `chain_count` and
  `extinction_coefficient_reduced` (its molar extinction coefficient at 280
  nm, ε280, in L/(mol.cm)). They're derived from its parts and copies (`registry:
derived`): its parts as they are, each cysteine's thiol free. Its bonds are a
  fact that a registrar may correct, so what they change (two hydrogens, 2.016
  u, off for each disulfide, and a cystine's 125 L/(mol.cm) on at 280 nm) is
  worked out where they're shown: the Composition tab and the run's message.
  Anything else that reads them, such as a lot's concentration from its
  absorbance, works it out the same way from the complex's bonds.

## Actions

| Action                | What it does                                                          | Tier    |
| --------------------- | --------------------------------------------------------------------- | ------- |
| `compute_composition` | Computes a complex's formula, masses, chain count and ε280, any type. | backend |

The trigger `compute_new_complex_composition` runs it on each complex a
project gets, of any type, batching up to 100 a run: its entry's derived
values are set by the first, and every project's record gets its parts'
disulfides. A complex's run reads its
parts (`include: [components.part]`), at the revisions they have then:

- a protein's formula is its residues', each its amino acid's less a water,
  and one water for its termini. Selenocysteine (U) is C3H5NOSe and pyrrolysine
  (O) C12H19N3O2. An ambiguous residue (B, Z, J or X) has no one formula, so a
  complex with one is named and left out. J (leucine or isoleucine, as mass
  spectrometry reads them) is C6H11NO either way, and absorbs at 280 nm as
  neither does, so it's weighed, and the run says so;
- a DNA sequence's is its bases', as Biopython's `molecular_weight` counts
  DNA's atoms: each its deoxynucleoside 5′-monophosphate's less a water for each
  phosphodiester bond, a 5′ phosphate and a 3′ hydroxyl on a linear strand,
  and both strands of a double-stranded one, which is two chains. A strand
  made with a 5′ hydroxyl (a synthetic oligonucleotide, a PCR product of
  unphosphorylated primers) is HPO3, 79.98 u, lighter, which the run says. An
  ambiguous base has no one formula;
- any other part's is its entry's `molecular_formula`: a compound's, an
  oligonucleotide's, a duplex's or another complex's, which this action
  computed when it was registered, with any isotope as RDKit writes it
  (`[13C]`, and D and T for hydrogen's), weighed as that isotope in both
  masses: deutetrabenazine is C19H21D6NO3, its six deuteriums 2.0141 u each.
  A part without one yet, or with a charged one (a zinc ion, Zn+2), is named
  and the complex left out.

A complex it can't compute, in a run of several, is an error about it, which
fails its own run, and the others are computed (ADR 0055); a run of one fails.
Each complex gets one message, at most, saying all of it, so a run of 100
keeps to the 100 messages a run's output has: the longest is cut at a
sentence, its whole in the run's log.

Its average mass is its formula's with CIAAW's standard atomic weights 2024,
the table every Iki plugin weighs by (`scripts/weights.py`,
[ADR 0272](../../docs/adr/0272-one-table-of-atomic-weights-weighs-every-molecule.md)),
as the protein plugin's molecular weights are, and its monoisotopic mass with
each element's most abundant
isotope's mass (AME2020: Wang et al., _Chin Phys C_ 45:030003, 2021). Its chain
count is each part's chains times its copies: a protein, nucleic acid or
oligonucleotide is one, a duplex two, a complex its own count, a compound none.
Its molar extinction coefficient at 280 nm in water, ε280, is Pace's (Pace et al., _Protein Sci_
4:2411–2423, 1995), as the protein plugin's is: 5,500 L/(mol.cm) for each
tryptophan and 1,490 for each tyrosine in its parts, each times its copies,
with each cysteine's thiol free, as its masses are; a part that's another
complex gives its own. Each disulfide makes a cystine, 125 more, counted from
the disulfides it has, not from every pair of cysteines: amivantamab's chains,
one by one, have 14 pairs, and it has 16 disulfides, two of them joining a
chain with an odd number of cysteines to another. A part that's neither a
protein nor a complex (a heme, an oligonucleotide) absorbs in ways Pace's
values don't weigh, so the complex has none, and the run says why.

The run says what its disulfides make it: _"With its 16 disulfides:
C6472H10014N1730O2023S46, 145,902.15 u average and 145,810.1054 u
monoisotopic, and 216,410 L/(mol.cm) at 280 nm"_. A part that's a complex has
bonds of its own, shown on it, and the run says what the complex is with those
as well, and when some may not be counted (a part complex whose own parts'
weren't counted in this project, or whose bonds aren't complete). Its other bonds (isopeptides, thioethers) aren't weighed: what each
takes off depends on its chemistry.

What it computes, checked against published values:

| Complex                                            | Its parts as they are                                                                                                                             | Published                                                                                                                                                                                                                                           |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hemoglobin A (CPX-2158)                            | C2954H4508Fe4N780O806S12, 64,453.31 u; no absorbance, as its hemes aren't weighed                                                                 | Complex Portal: 64 kD. Its α chain alone, 15,126.40 u: 15,126.4 Da by electrospray (Rai, Landin and Green, Waters application note, _The analysis of human haemoglobin variants using mass spectrometry_)                                           |
| Amivantamab (KEGG D11894)                          | Its chains' residues alone, unglycosylated: with its 16 disulfides, C6472H10014N1730O2023S46, 145,902.15 u; 214,410 L/(mol.cm), 216,410 with them | KEGG DRUG (its peptide chains): C6472H10014N1730O2023S46, 145,900.22 (average), 145,810.1055 (exact); ours 145,810.1054. The glycoprotein's major glycoform is 148,209 Da (FDA's chemistry review of Rybrevant, BLA 761210), its glycans 2,307 more |
| Trastuzumab Fab–HER2 (PDB 1N8Z)                    | C4980H7745N1389O1538S65, 113,767.82 u, 3 chains; 133,620 L/(mol.cm), 136,495 with HER2's 23 disulfides and 136,995 with the Fab's 4 as well       | Its chains' residues, and its disulfides 1N8Z's SSBOND records; RCSB's entity weights are 0.1% higher                                                                                                                                               |
| Human insulin (P01308)                             | With its 3 disulfides, C257H383N65O77S6, 5,807.63 u, 2 chains                                                                                     | USP and DrugBank (DB00030): C257H383N65O77S6, 5,807.57, by older atomic weights                                                                                                                                                                     |
| Drew–Dickerson dodecamer with netropsin (PDB 1BNA) | Its two strands with a 5′ phosphate, 7,452.79 u, and netropsin's C18H26N10O3: C250H322N102O149P24, 7,883.26 u, 2 chains                           | Biopython's `molecular_weight`, double-stranded, by older atomic weights: 7,452.73; netropsin, PubChem CID 4461                                                                                                                                     |

Every mass is its parts' formulas', so a glycoprotein's is its chains' without
their glycans, or any other modification its parts' formulas don't have.
Amivantamab's formula, from KEGG DRUG's chains, is KEGG's exactly; its average
mass is 13 ppm more, and insulin's 10 ppm more than USP's, as they weigh by
IUPAC's atomic weights of 2005 (C 12.0107, H 1.00794), which CIAAW has since
replaced. Hemoglobin's α chain, 15,126.40 u, is what electrospray measures. Extinction coefficients are
Biopython ProtParam's for each chain with its thiols free
(`molar_extinction_coefficient`), summed over copies.

## Components

**Composition**, a tab on every complex's page: its parts with their roles and
copies; its formula, masses and ε280 as computed, with the absorbance of 1
g/L over 1 cm that follows from ε280 and its average mass (ProtParam's "Abs
0.1%"), and each with its disulfides, and with its parts' own as well; whether any may not be counted;
and its bonds, each from a copy's residue to another's. It's named for screen readers
as it's drawn for the eye.

## Tests

```console
iki plugin test plugins/complex --with plugins/dna --with plugins/protein --with plugins/small_molecule --with plugins/antibody
```

`tests/compute_composition.yaml` computes hemoglobin A, amivantamab with its
disulfides, trastuzumab's Fab bound to HER2 (a complex of a complex), human
insulin beside a complex it can't compute in one run, insulin read with J for
its leucines and isoleucines, VMAT2 with deuterated deutetrabenazine, and the
Drew–Dickerson DNA dodecamer with netropsin; and refuses one with an ambiguous
residue and insulin's hexamer with its charged zinc ions.
`server/tests/test_complex_composition.py` runs a full pack of 100, checking
its output against the run-output schema, and checks formulas and masses
against RDKit's and Biopython's. `server/tests/test_complex_components.py` shows
trastuzumab's composition in each browser, in light and dark, with axe's WCAG
2.2 AA checks; `server/tests/test_rows.py` checks a complex's parts and bonds
with the test plugin's own complex type; and `server/tests/test_first_party_plugins.py`
registers each complex type and finds them all through `iki/core.Complex` and
the graph. The dev cluster's end-to-end check waits for the trigger to compute
trastuzumab's Fv's composition.

# small_molecule

Iki's small_molecule plugin: compounds, registered by their standard InChIKey,
from SMILES or SDF files, and their batches, each with its salt form, with
RDKit's standardisation and descriptors.

- **`small_molecule.Compound`** (`CPD-000001`): a compound, by its
  standardised structure.
  - Its identity is its standard InChIKey, so registering the same compound
    again, drawn any way, reuses it.
  - It keeps its canonical SMILES (required and fixed, like its InChIKey), its
    standard InChI, its name, synonyms and description, and its PubChem CID,
    ChEMBL ID (each unique in a project, and linked to its record there) and
    CAS number.
  - Its formula, molecular weight, exact mass, charge, heavy atoms, Crippen
    logP, TPSA, hydrogen-bond donors and acceptors, rotatable bonds, rings,
    aromatic rings, fraction Csp3, stereocentres (and how many are undefined),
    QED and Lipinski violations are computed by the plugin's actions.

- **`small_molecule.CompoundBatch`** (`LOT-000001`): a batch (a lot) of a
  compound, the material in the vial.
  - Its compound is its parent structure, without salts or solvents, as
    registration systems keep them; each batch says its salt form: its salt
    (`hydrochloride`, `sodium`, `mesylate` and 28 others) and how many of it
    for each of the compound, and its solvate (`water` for a hydrate, and 8
    others) and how many.
  - Its formula weight, the weight of the material for each of the compound,
    is computed: weigh doses out by it.
  - It keeps its structure as drawn, salts and all, its purity, amount,
    supplier, catalogue number, supplier's lot and when it arrived.
  - It has no identity: every batch is a batch of its own.

## Actions

They run on backend workers, in the `chemistry-tools` environment: Python 3.12
with [RDKit](https://www.rdkit.org) 2026.09.2 from PyPI (`rdkit`,
BSD-3-Clause), which asks for 512 MiB. RDKit's wheels carry what it needs
(Boost, and the InChI Trust's InChI library 1.07.3), so the plugin needs no
image of its own, as the protein plugin's Biopython doesn't.

Each action lists its software, by name and the version its environment
installs, so what it makes is recorded as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)):
RDKit 2026.09.2, and the InChI library 1.07.3 for those that identify
compounds. A registered compound is RDKit's standardisation of what it was
given, so it's recorded as made with them.

- **`register_compounds`**: registers each line's SMILES (up to 5,000; a name
  may follow, after a space or a tab) as a compound, standardised and
  identified. A compound already in Iki is reused, and so is a line that's the
  same compound as an earlier one. With **the parent**, salts and solvents are
  taken off; with **batches**, each line is also a batch of its compound, with
  what was taken off and its formula weight. A line that can't be registered
  fails the run, which names each such line and says why, so nothing is half
  registered; or, with `on_error: skip`, is left out, and the run says so.
- **`compute_descriptors`**: a compound's descriptors and standard InChI, for
  many at once. Its trigger, which a project's Admins turn on, runs it on each
  new compound.
- **`compute_formula_weight`**: a batch's formula weight, from its compound's
  structure as it was when the run was launched (ADR 0105). Its triggers run
  it on each new batch, such as one made with its form, and again when its
  salt or solvent changes.
- **`import_sdf`**: registers each record of an SDF file as a compound,
  standardised and identified as a SMILES line is, and with **batches** a batch
  of each (up to 5,000 records; without, a million, read a record at a time).
  Its name is the SD field chosen, or the record's title. SD fields that are a
  compound's or a batch's fields are kept, matched whatever their case, spaces
  and underscores, and by the names suppliers and PubChem give them (`CAS`,
  `PUBCHEM_COMPOUND_CID`, `Catalog_No`, `Lot`, `Purity`…); the run says which
  others it left out. A salt an SD field gives must agree with the structure's;
  one only the field gives (an HCl salt drawn as its free base) is the
  batch's. A record's CAS number, PubChem CID and ChEMBL ID are its drawn
  structure's, so a parent registered from a salt doesn't take them. Each
  records the file and record it came from, with the record's SHA-256. A record that can't be imported fails the run, naming each, or with
  `on_error: skip` is left out.
- **`export_sdf`**: writes compounds (up to 50,000) to an SDF file the run
  keeps, each with 2D coordinates RDKit computes (CoordGen), its name or HRID
  as its title, and its HRID, identifiers, descriptors and external IDs as SD
  fields.

### Standardising

A structure is standardised as RDKit's MolStandardize does it
(`rdMolStandardize.Cleanup`): hydrogens made implicit, bonds to metals broken
(a sodium bonded to an oxygen becomes Na⁺ and O⁻), functional groups drawn in
their usual form (a five-bonded nitro group charge-separated, and so on), and
charges moved to the most acidic sites (reionised). Its SMILES is RDKit's
canonical isomeric SMILES of that, and its identity the standard InChIKey of
its standard InChI. A salt registered as it is is its own compound: sodium
aspirinate is JZLOKWGVGHYBKD-UHFFFAOYSA-M, not aspirin.

The parent is what registration systems register: fragments that are salts or
solvents Iki knows are taken off (matched by their structure, neutralised, so
`[Cl-]` and `Cl` are both hydrochloride), of what's left the largest is kept
(RDKit's LargestFragmentChooser), and it's neutralised where it can be
(RDKit's Uncharger). So aspirin's sodium salt and aspirin are the same
compound, and its batch says `sodium`, one equivalent. Equivalents count for
each parent: a calcium salt of two aspirins is half a calcium each. A part
that isn't a salt or solvent Iki knows is left out, and the run warns which;
two different salts, or solvents, in one structure fail its line, since a
batch records one of each.

A batch's formula weight is its compound's molecular weight, with each
equivalent of its salt and solvent. An acid or a base forms its salt whole (an
amine's hydrochloride adds HCl, 36.46 g/mol); a metal takes the place of its
acid's hydrogens (a sodium salt adds Na less H, 21.98 g/mol; a calcium salt,
for each calcium, Ca less two H).

### Descriptors

From the compound's SMILES, as RDKit 2026.09.2 computes them:

- **Molecular weight**: from IUPAC's standard atomic weights
  (`Descriptors.MolWt`); **exact mass**, monoisotopic (`ExactMolWt`).
- **Crippen logP**: Wildman and Crippen's atom contributions (1999).
- **TPSA**: Ertl's topological polar surface area (2000), of its nitrogens and
  oxygens, in Å².
- **H-bond donors and acceptors**: RDKit's own definitions
  (`CalcNumHBD`, `CalcNumHBA`), which differ from Lipinski's.
- **Lipinski violations**: of the rule of five, with its original counts:
  molecular weight over 500, logP over 5, more than 5 NH and OH, more than 10
  N and O.
- **Rotatable bonds**: RDKit's default (strict) definition; **rings**, the
  smallest set of smallest rings.
- **Stereocentres**: tetrahedral centres, by RDKit's current stereo
  perception, and how many the structure leaves undefined.
- **QED**: Bickerton's quantitative estimate of drug-likeness (2012), with its
  mean weights.

A compound whose InChIKey isn't the one its SMILES gives, as one made by hand
through Iki's form or API might be, fails its own run, naming both keys;
register its structure with `register_compounds` instead.

## Licences

RDKit is BSD-3-Clause. Its wheels bundle Boost (BSL-1.0) and the InChI Trust's
InChI library (MIT), and fonts for drawing: Roboto (Apache-2.0), Telex (SIL
OFL 1.1) and Amadeus, which came without a licence; the plugin draws nothing
with them. It needs numpy (BSD-3-Clause) and Pillow (MIT-CMU), which
`osv-scanner.toml` has an exception for, with why.

## Developing it

From the repository root:

```sh
uv run iki plugin validate plugins/small_molecule
cd plugins && uv run iki plugin makemigrations small_molecule   # after changing a manifest
iki plugin test plugins/small_molecule
```

Its environment's packages are pinned, with their hashes, in
`requirements.txt`, compiled from `requirements.in` as it says. Pin the same
releases in the repository's dev dependencies, where
`server/tests/test_small_molecule_actions.py` runs the actions: `make
check-deps` checks they match, and that each is at least 3 days old. The
`software` versions are RDKit's own (`rdkit.__version__`, 2026.09.2, which
PyPI calls 2026.9.2) and the InChI library's (`rdkit.Chem.inchi.GetInchiVersion()`).

`server/tests/test_first_party_plugins.py` checks that it's valid, that its
migrations match its manifests, and that it migrates forwards and back. The
dev cluster's end-to-end check (`deploy/dev/sso.py`) registers aspirin and its
sodium salt, checks its InChIKey, batches and descriptors, and that aspirin
drawn another way is reused.

# oligo

Iki's oligo plugin: oligonucleotides, modified or not, by their HELM, the
Pistoia Alliance's notation for them; the registry of monomers they're made
of; and duplexes of two strands, such as an siRNA. Molecular weight, formula,
extinction and bases are computed from the monomers' structures.

- **`iki/oligo.Oligonucleotide`** (`OLI-000001`): one strand, such as an
  antisense oligonucleotide, an siRNA's strand, a primer or a probe, with the
  chemical groups on it. It's an `iki/core.Sequence`, so searching sequences
  finds it beside DNA and proteins.
  - Its identity is its canonical HELM
    ([ADR 0144](../../docs/adr/0144-canonical-helm-is-an-identity.md)): one
    molecule written two ways (with or without needless brackets, its
    conjugate first, an annotation, a fixed repeat written out) is one
    registry entry, and a molecule with any monomer or bond changed is
    another. A 5′-biotin primer, or a probe with its 6-FAM and BHQ-1, isn't
    the plain one.
  - Its HELM is one RNA polymer (HELM writes DNA as RNA with deoxyribose) and
    the chemical groups (CHEM) on it. Two strands are two oligonucleotides and
    a duplex. A position that's one of several monomers, a mixture, a repeat of
    unknown count, an inline SMILES or an unknown monomer (`*`, `X`, `N`, `?`)
    has no one formula, so it's refused, saying which. A gap (`_`) is no
    monomer: canonical HELM leaves it out, so it adds nothing.
  - Its entry keeps what's computed from it: the monomers it's made of
    (references to the registry), its bases, length, formula, average
    molecular weight, monoisotopic mass, ε260 with a note on how it was
    computed or why it wasn't, its modifications, and each position's sugar,
    base and linker, which its **Chemistry** tab draws.
  - Each project's record keeps its own name, description, organism and
    purpose (antisense, siRNA strand, primer, probe, aptamer).
- **`iki/oligo.Monomer`** (`MON-000001`): a HELM monomer, by its polymer type
  and symbol (`RNA` `MOE`, `CHEM` `5FAM`): a sugar, base, linker or modifier,
  with its structure as SMILES, each attachment point's cap mapped (`[H:1]`,
  `[OH:3]`), its natural analogue, and a modifier's own ε260.
  - Its structure is a fixed fact
    ([ADR 0143](../../docs/adr/0143-a-registry-fact-can-be-fixed.md),
    [ADR 0145](../../docs/adr/0145-a-registry-of-helm-monomers.md)): given when
    it's registered, never changed, because every oligonucleotide made of it
    was weighed from it. A wrong one is superseded by the registry's
    registrars: a new monomer, and the old one's oligonucleotides registered
    again.
  - Its structure key, the SHA-256 of its polymer type, RDKit's canonical
    SMILES with its attachment points as `[*:n]`, and each attachment point's
    cap (`R1=H`), is unique: two symbols can't name one structure. The caps are
    in it because a free one stays on the molecule: a methyl capped with H is
    methane, and with OH methanol.
- **`iki/oligo.Duplex`** (`DUP-000001`): two strands annealed, by their roles
  (sense and antisense). Its identity is its strands by role, in either order
  (ADR 0138's rows); the same strands with their roles swapped is another
  duplex. Its duplex region, overhangs, mismatches and pairing are found from
  its strands' bases, which its **Pairing** tab draws, and its formula and
  weight are its strands' together.

## Actions

They run on backend workers, in the `oligo-tools` environment: Python 3.12
with [RDKit](https://www.rdkit.org) 2026.09.2 from PyPI (`rdkit`,
BSD-3-Clause), the small_molecule plugin's release, which asks for 512 MiB.

- **`register_oligonucleotide`**: registers an oligonucleotide by its HELM,
  with everything computed from its monomers. Each monomer it names is the
  registry's: one given to the run (a lab's own), or else the library's,
  registered with it if the registry hasn't got it. A library monomer the
  registry has with another structure fails the run: the structure is a fixed
  fact. A symbol in another case (`[moe]`) fails it, saying which it may mean:
  HELM 2.04 says its notation is case-insensitive, but the HELM monomer
  libraries and toolkits compare symbols exactly, and so does Iki.
- **`import_oligonucleotides`**: registers each row of a CSV file (a `helm`
  column, and `name` and `description` if it has them) or each record of an
  SDF file (a `HELM` data item, and its `Name` item or title), up to 2,000.
  SD data items are read as BIOVIA's CTfile Formats has them: a value runs to
  a blank line, so a line of it starting with `>` is its value's, and a HELM
  wrapped over several lines is one HELM. A record ends at a line that's only
  `$$$$`, since a wrapped HELM's line may start with its own `$$$$`.
  Each records the row or record it came from, with its SHA-256: of every line
  a CSV row spans, a quoted value's line breaks among them, with their line
  ends as the file has them. A row that
  can't be registered fails the run, naming each, or with `on_error: skip` is
  left out, registering none of its monomers.
- **`compute_oligonucleotide`**: an oligonucleotide's properties from the
  monomers it lists, for one registered from its form or the API. Its trigger
  runs it on each new entry. One whose HELM names a monomer it doesn't list is
  named, and nothing of it is computed.
- **`register_monomer_library`**: registers the library's 28 monomers.
- **`compute_monomer`**: checks a monomer's structure (it reads, its caps are
  H or OH, a sugar has R1, R2 and R3, a base R1, a linker R1 and R2) and
  computes its structure key, attachment points, formula and weight. Its
  trigger runs it on each new monomer.
- **`register_duplex`**, **`analyse_duplex`**: a duplex of two strands, with its
  pairing. The trigger finds a duplex registered from its form's strands
  through the graph (`related`, upstream).

For labs that register strands from a standard pattern of modifications, the
[oligo design example](../../examples/plugins/oligo_design/README.md) provides a
copyable action and two worked fixtures.

### What's computed

Its HELM is read by iki-wasm, the reader Iki's forms and identities read it
with, through the plugin runtime's `iki_runtime.helm` (its environment declares
`iki_runtime: { helm: true }`,
[ADR 0288](../../docs/adr/0288-plugins-share-readers-from-the-runtime-each-release-ships.md)).
What's computed is read from its canonical form, so one molecule written two
ways computes the same: a repeat is written out, a gap (`_`) adds nothing (it's
no part of the identity either), a cyclic strand's bases and positions start where
its canonical form starts it, and a message names polymers as the canonical
form numbers them (saying what each is when that isn't how it was written).
What has no one structure is refused: a monomer that's one of several or a
mixture, a repeat of a range of counts, an unknown monomer or position, a
polymer group, a monomer in in-line SMILES, a BLOB.

A molecule is built from its monomers with RDKit: along a polymer, each
backbone monomer's R2 joins the next one's R1 (a sugar's 3′-O to a
phosphate, the phosphate to the next sugar's 5′-O); a base's R1 joins its
sugar's R3; and each HELM connection joins the two attachment points it
names. A joined attachment point loses its cap, and `Chem.molzip` makes the
bond, keeping every atom's stereochemistry.

- **Formula and weight** are the free acid's, as the WHO's INN lists, EMA's
  assessment reports and vendors give them: every phosphate and
  phosphorothioate protonated, and a cationic dye's charge (Cy3, Cy5) taken by
  one, as an inner salt, and each is labelled "(free acid)". The weight as the
  sodium salt, which is how marketed oligonucleotides are given (nusinersen
  sodium, 7,501.0 g/mol; patisiran sodium, 14,304 Da), is beside it: 21.98
  g/mol more for each acidic proton of its phosphates and phosphorothioates,
  and none for a molecule with no acidic proton, such as a methylphosphonate
  or a morpholino. The average molecular weight is from CIAAW's standard atomic
  weights 2024, sulfur 32.06 and phosphorus 30.973761998, the table every Iki
  plugin weighs by ([ADR 0272](../../docs/adr/0272-one-table-of-atomic-weights-weighs-every-molecule.md)); the monoisotopic mass from each element's most
  abundant isotope. An atom a
  monomer's structure labels with its isotope (`[13CH]`, `[2H]`) weighs its
  nuclide's mass (RDKit's, from AME2020) in both, and the formula writes it as
  itself: ribose-¹³C₅ uridine is C4[13C]5H12N2O6, 249.16 g/mol.
- **One molecule**: each chemical group (CHEM) is joined to the strand,
  directly or through another group. One written but not connected is
  refused, naming it: the oligonucleotide is one molecule, and a dye beside it
  isn't part of its weight.
- **ε260** is by nearest neighbours
  ([ADR 0146](../../docs/adr/0146-extinction-by-nearest-neighbours-only-where-it-holds.md)),
  for a single strand of natural DNA (Cantor, Warshaw & Shapiro,
  _Biopolymers_ 9, 1059, 1970) or natural RNA (Puglisi & Tinoco, _Methods
  Enzymol._ 180, 304, 1989): the doublets' sum less the inner nucleotides'.
  Phosphorothioates are read as phosphates, whether their phosphorus is
  undefined, Rp (`[Rsp]`) or Sp (`[Ssp]`), and the note says so. A dye's own
  ε260, as its supplier gives it, is added. A strand with any other sugar or
  base (2′-MOE, LNA, 2′-OMe, 2′-F, 5-methylcytosine) or that mixes DNA and RNA
  has none: no published nearest-neighbour parameters describe them, and the
  note names which, so it's measured instead. A strand with an abasic site,
  whose neighbours aren't neighbours, has none, and nor does a ring (a strand
  joined to itself), since the model's end correction describes a linear
  strand. A duplex has none.
- **Bases** are each base's natural analogue, 5′ to 3′ (5-methylcytosine is
  C; T on a ribose stays T; `-` an abasic site, a sugar with no base), which a
  search by bases finds plain and modified oligonucleotides alike by
  ([ADR 0147](../../docs/adr/0147-oligonucleotides-are-found-by-their-bases.md)).
- **Length** is its nucleotides: its units with a sugar, an abasic site
  among them. A unit without one, such as a 5′ phosphate, isn't a nucleotide:
  the Chemistry tab draws it at the end it's on.
- **The duplex region** is where the strands, antiparallel, pair longest:
  Watson–Crick pairs, with no two non-pairs in a row; the stretch with most
  pairs, then fewest mismatches. A G·U wobble counts as a mismatch. Its length
  is the region's, from its first pair to its last, mismatches included; its
  mismatches are counted apart.

They're checked against published molecules in `tests/` (`iki plugin test
plugins/oligo`), each cited: nusinersen, fomivirsen, both strands
of patisiran and of inclisiran (with its GalNAc), and patisiran and inclisiran
as duplexes, against PubChem, ChEMBL and EMA's assessment reports; and
Sigma-Aldrich's and ATDBio's worked extinction coefficients. Iki's own tests
(`server/tests/test_oligo_chemistry.py`) check the weight each modified
nucleotide and modifier adds against IDT's and LGC Biosearch's published
values (2′-OMe, 2′-F, LNA, 2′-MOE, 5-methyl-dC, phosphorothioate, 6-FAM, BHQ-1,
BHQ-2, Cy3, Cy5, biotin-TEG, amino C6, C3 spacer), and inotersen's formula and
weight against its FDA label.

## The monomer library

`data/monomers.json` is built by `data/build_monomers.py` from the Pistoia
Alliance's [HELM monomer sets](https://github.com/PistoiaHELM/HELMMonomerSets)
(MIT licence), at commit `fecc972f`, with the corrections `data/README.md`
lists, each checked against its supplier's published mass.

## Licences

The plugin is Apache-2.0. The monomer library is derived from the HELM
monomer sets, © the Pistoia Alliance, under the MIT licence, whose notice is
in `data/README.md`. RDKit is BSD-3-Clause; its wheels bundle Boost
(BSL-1.0); it needs numpy (BSD-3-Clause) and Pillow (MIT-CMU).

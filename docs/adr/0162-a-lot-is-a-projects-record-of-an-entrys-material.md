# 0162. A lot is a project's record of an entry's material

- Status: Accepted
- Date: 2026-11-19
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0161](0161-a-complex-is-its-parts-and-their-copies.md),
  [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0139](0139-a-records-amount-is-a-ledger-of-what-moved.md)

## Context

A registry entry is a molecule (ADR 0115): an oligonucleotide by its HELM, a
protein by its sequence, an antibody by its chains. A lab doesn't keep
molecules; it keeps material: an oligonucleotide's synthesis, a plasmid's
maxiprep, a protein's purification, an ADC's conjugation run, a polymer's
batch. Each is a lot, named on its label and its certificate of analysis,
with what was measured of it, how much is left, and whether QA released it.
`iki/small_molecule.CompoundBatch` is one, for compounds, with its salt form
and formula weight; nothing else has one.

What a lot measures depends on its kind: an oligonucleotide's mass by ESI-MS
and its yield in OD, a plasmid prep's A260/A280, a protein's concentration by
A280, an ADC's drug-to-antibody ratio, a polymer's Mn and Mw. So does the
unit it's kept in: oligonucleotides are made and used by the nmol, plasmids
by the µg, proteins by the mg. What every lot needs is the same: where it
came from, its dates, its certificates, its amount, and a QA disposition, as
GMP keeps material in quarantine until it's tested and released (21 CFR
211.84, 211.22). Benchling's registry has batch schemas per entity schema,
Dotmatics Register batches per compound, and LabWare and SAP QM a lot
status of quarantine, released, restricted and rejected for every material.

ADR 0139 lets one type's state hold its amount, and keeps an amount on
concrete types: a subtype can't keep its parent's in another unit
(`amount-twice`). A state that holds the amount was allowed only on a type
with one, so an abstract parent couldn't carry a disposition that holds its
subtypes' amounts.

## Decision

**One abstract lot, a type per kind.** `iki/lots.Lot` is abstract: lot
number, whether it was made or bought, its supplier, who made it, when it was
made, received, retested and expires, storage, certificates of analysis (a
list of files), notes, and its disposition. Each kind extends it with a
reference to what it's a lot of, by type, its amount in its own unit with a
concentration to draw it by, and its own measurements: `OligoLot` of an
oligonucleotide, `PlasmidPrep` of a plasmid, and through the abstract
`BiologicLot` (A280, ε280, SEC, endotoxin) `ProteinLot` and `ConjugateLot`. `GenericLot` is a
lot of any entity that has no type of its own. A search of `iki/lots.Lot`
finds every kind, with its subtypes in a lab's plugins.

**A lot isn't an identity.** Two syntheses of one oligonucleotide are two
lots, each its project's own record, as a batch is; the reference to the
entry is what they share, and the graph walks it.

**The disposition is the parent's, and holds each kind's amount.** Quarantined
(initial: drawn from only to be tested), released (locks, a QA release
signature, from quarantine or a lifted hold, on a certificate, by someone in
QA other than its maker, in Iki rather than through an agent), on hold,
rejected (locks, signed) and expired (once its date has come). On hold,
rejected and expired hold the amount. The validator now lets an abstract
type's state hold an amount its concrete subtypes each keep, and refuses a
concrete type that inherits such a state without one (`holds-without-amount`).

**What follows from a measurement is worked out, where it can be.** An action
per kind, triggered when a lot is recorded and when its inputs change, works
out what follows from what was measured and from the entry: an oligonucleotide
lot's expected mass and its difference from the measured one, and its yield
in nmol; a plasmid prep's concentration from A260; a protein lot's
concentration from A280 and the sequence's ε280; a conjugate's DAR from its
drug load distribution; a polymer's dispersity. A value only these give (a
mass difference, a dispersity) is `computed`. A concentration or a DAR is
also measured other ways (BCA, a dye, UV/Vis), and a concentration is what the
amount draws through and what a pool changes (ADR 0157), so it stays an
ordinary field: the action fills it in while its method is the one it works
out, and leaves it as entered otherwise.

**A check that's a specification holds.** An oligonucleotide lot's measured
mass outside its tolerance of the entry's is kept, as a result out of its
specification must be, and held for QA's review (ADR 0127); held, it can't be
released. Tolerance is a field, the lab's specification, 2 Da by default. What
can't be true is refused: a polymer's Mw below its Mn, a distribution of both
intact and reduced species.

**A protein lot is of a protein or of a complex.** An antibody, an Fc fusion
or a multispecific is an `iki/core.Complex` of protein chains (ADR 0161), so
`ProteinLot.protein` names a protein or any complex (ADR 0136), and one type
serves them all rather than a lot type per kind of complex. A complex keeps
its molecular weight and ε280 with each cysteine's thiol free, as its parts
are, because its bonds may be corrected; a lot whose cysteines are cystines
applies the disulfides its bonds place, as the complex's Composition tab
does: two hydrogens off and a cystine's 125 L/(mol·cm) on for each.
Trastuzumab's two heavy and two light chains are 145,197.41 g/mol and 213,380
L/(mol·cm) free; with KEGG D03257's 16 disulfides, 145,165.16 g/mol and 215,380,
1.4837 mL/(mg·cm). A protein's own are its sequence's: its ε280 with its
cysteines paired or reduced, as the lot says.

**Kinds that aren't registered yet refer to any entity.** Conjugates and
synthetic polymers have no types yet, so `ConjugateLot.conjugate` and
`PolymerLot.polymer` name any entity. When a plugin registers them, those
references narrow to its types, `iki/lots` depending on it, and the deploy
checks that the lots recorded fit (ADR 0136). A lab adds a kind of its own by
extending `Lot` or `BiologicLot` in its plugin.

**Lots live in one plugin that depends on the kinds.** `iki/lots` depends on
`iki/protein`, `iki/oligo` and `iki/plasmid`, rather than each
kind's plugin depending on it, so installing a kind doesn't install lots,
and no plugin of a kind changes to have them.

## Consequences

- One disposition, one QA role (`iki/lots.qa`), one set of certificates and
  one search for every lot, whatever it's of; a lab's own kinds get them by
  extending the type.
- An aliquot split from a lot (ADR 0139) is a lot of the same kind, in
  quarantine, since states aren't copied: its release is QA's, signed for.
  Vials that follow their lot's disposition belong to the sample inventory,
  which models containers.
- `CompoundBatch` stays small_molecule's: its amount is a quantity field that
  imports set, and a run's creates don't carry an amount yet (ADR 0139). It
  becomes a `Lot` once they do.
- A concentration entered by hand while its method is A280 isn't checked
  against the A280; the next change to the A280 or its dilution works it out
  again, and the history shows both.
- `iki/lots` can't be installed without the plugins of the kinds it has types
  for.

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

**A lot is one entry's material, and stays it.** Each kind's amount names
that reference as what it's of (`amount.material`, a required, immutable
reference, `amount-material`), so BiologicLot keeps no amount of its own:
ProteinLot's is of its `protein`, ConjugateLot's of its `conjugate`. A
transfer into a lot takes only another lot of the same entry (a reference
to a registry entry and one to a project's record of it are the same
entry); an aliquot split off it is of that entry, whatever it's given.
Material of two entries pooled is a mixture, a record of its own, as
LabWare's and Benchling's lots each belong to one material. And a lot a
state locks, a released one, takes only material in that state: what's
added to released material has been released too, as ICH Q7 (8.4) blends
only batches each tested and released. Equal concentrations (ADR 0157)
aren't enough: quarantined material at the same 100 µM is still untested.

**The disposition is the parent's, and holds each kind's amount.** Quarantined
(initial: drawn from only to be tested), released (locks, a QA release
signature, from quarantine or a lifted hold, on a certificate, in Iki rather
than through an agent), on hold, rejected (locks, signed) and expired (QA
may set it once the expiry or retest date has come). On hold, rejected and
expired hold the whole amount, including samples for testing. The validator
lets an abstract type's state hold an amount its concrete subtypes each keep,
and refuses a concrete type that inherits such a state without one
(`holds-without-amount`).

**A release is subject to the lot's dates.** Every kind inherits the Lot's
draw guards and its QA sampling policy (ADR 0139). Starting on `expires_on`,
all draws, transfers out and splits are refused. Starting on `retest_on`,
only a QA sampling draw is allowed. The person taking that sample must be
able to draw from the lot and hold `iki/lots.qa` in the lot's owning project.
Expiry takes precedence when both dates are due. An absent date imposes no
restriction of its own. Quarantined lots also require the QA sampling purpose
for a draw. The lot's page shows why routine use is restricted even while
the disposition still reads Released. QA records any change of disposition.

**QA releases what someone else made and recorded.** GMP keeps the quality
unit independent of production (21 CFR 211.22; EU GMP Part I, chapter 2),
and Part 11 makes each signature its signer's own, so the release's guards
keep the two apart on what Iki knows, not only on what's typed in. A lot
says whether it was made or bought before it's released. One made in the
lab names who made it: left empty, it isn't someone else. Whoever it names
doesn't release it, nor does whoever recorded it, whom Iki knows from its
history whatever the maker field says. A bought lot's maker is its
supplier, so whoever recorded its receipt may release it, on its
certificate.

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

What the measurement gives is kept beside it, computed
(`a260_concentration`, `a280_concentration`, `distribution_dar`), and the
field follows that, not the reading alone. When the reading gives something
new, the field is it. When it gives nothing any more (its A260 taken out, its
ε280 gone), the field is emptied if it was what the reading gave, so an old
concentration doesn't keep drawing the amount. Otherwise the field was set
since, by a pool or by hand, and is kept: a run for a conjugate's drug loads
doesn't put a pooled lot back to the concentration its pre-pool A280 gave,
nor a hand-entered DAR to its distribution's. A DAR by UV/Vis is as measured,
whatever distribution there is. A reduced conjugate's DAR is each chain's
mean times the copies of it its conjugate has, as a complex of them, such as
an antibody, says (2 and 2 for an IgG, 1 and 1 for a Fab); one that doesn't
say has none worked out, rather than an IgG's. Its unconjugated share is of
its intact species' peak area, the molar share only if they respond alike. A
path length or an extinction coefficient is more than 0, by the types'
rules; an absorbance can't be inverted over no path or through no
absorptivity, so a 0 works nothing out, and a path left out isn't taken for
1 cm. A polymer's Mn and Mw each say how they were measured, as SRM 1478's
come from membrane osmometry and sedimentation equilibrium, and its
dispersity is of molar mass (Đ_M).

**A check that's a specification holds.** An oligonucleotide lot's measured
mass outside its tolerance of the entry's is kept, as a result out of its
specification must be, and held for QA's review (ADR 0127); held, it can't be
released. The rule reads the measured mass, its basis and its tolerance, and
the entry's mass through the reference, at every save, rather than the mass
difference its action writes later: an edit out of its tolerance is held as
it's saved, with the project's triggers on or off, and before a run could
lag behind a release. A measured mass the entry has no mass to check against
is held too (`mass_is_checked`), for QA to review its identity another way.
They're compared to a micro-dalton, finer than any mass spectrometer
resolves and coarser than the binary rounding of two decimals' difference,
so 5555.03254 against 5554.98250 is 0.05004 Da, outside ±0.05001, and
exactly 2 Da is within ±2. Tolerance is a field, the lab's specification, 2 Da by default. What
can't be true is refused: a polymer's Mw below its Mn, a distribution of both
intact and reduced species.

**A protein lot is of a protein or of a complex.** An antibody, an Fc fusion
or a multispecific is an `iki/core.Complex` of protein chains (ADR 0161), so
`ProteinLot.protein` names a protein or any complex (ADR 0136), and one type
serves them all rather than a lot type per kind of complex. A complex keeps
its molecular weight and ε280 with each cysteine's thiol free, as its parts
are, because its bonds may be corrected; a lot whose cysteines are cystines
applies the disulfides its bonds place, as the complex's Composition tab
does: two hydrogens off and a cystine's 125 L/(mol·cm) on for each. A
protein's cystines take their hydrogens off too, one pair of cysteines for
each cystine its oxidized ε280 counts: hen lysozyme's four take 8.06 g/mol
off its sequence's 14,313.0, so 37,970 L/(mol·cm) is 2.6543 mL/(mg·cm).
Trastuzumab's two heavy and two light chains are 145,197.41 g/mol and 213,380
L/(mol·cm) free; with KEGG D03257's 16 disulfides, 145,165.16 g/mol and 215,380,
1.4837 mL/(mg·cm). A protein's own are its sequence's: its ε280 with its
cysteines paired or reduced, as the lot says.

**A prep can have several verification records.** Each links that physical
prep to its evidence, the plasmid project-record revision, the outcome and
the signed review.
[ADR 0184](0184-a-plasmid-prep-links-to-reviewed-verification-evidence.md)

**A conjugate or polymer lot names its registered design.** The conjugate entry
identifies the carrier and its attachments. Each physical lot records its
own measured distribution and DAR (ADR 0182). A synthetic polymer lot names a registered
polymer and records its own molar-mass measurements and dispersity (ADR 0183). A lab can
extend `Lot` or `BiologicLot` for additional material types.

**Lots depend on their material types.** `iki/lots` depends on
`iki/protein`, `iki/oligo`, `iki/plasmid`, `iki/conjugate` and `iki/polymer`. Each
material plugin can also be installed on its own.

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
  against the A280, which is shown beside it; the next reading that gives
  another concentration works it out again, and the history shows both.
- A lot's computed values follow its entry's properties when the lot is next
  saved or run: a complex's bond map corrected later doesn't recompute its
  lots by itself, since a change to a registry entry fires no project's
  triggers. Its mass check reads the entry at every save of the lot.
- A plasmid's sequence is a double-stranded circle (`iki/plasmid`'s rules),
  which its preps' A260 assumes.
- `iki/lots` can't be installed without the plugins of the kinds it has types
  for.

# lots

Iki's lots plugin: lots of any registered entity. A registry entry is the
molecule, the same for every project; a lot is the material, what was made or
bought of it at one time, which a label and a certificate of analysis name:
how much of it is left, what was measured of it, and whether QA released it
for use ([ADR 0162](../../docs/adr/0162-a-lot-is-a-projects-record-of-an-entrys-material.md)).

Every kind of lot extends one abstract type, so a search of `iki/lots.Lot`
finds them all, and each has the measurements its kind needs:

| Type                    | HRID           | Lot of                                    | Amount kept in | What's measured, and worked out                                                                                                                 |
| ----------------------- | -------------- | ----------------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `iki/lots.OligoLot`     | `OLOT-000001`  | `iki/oligo.Oligonucleotide`               | nmol, by µM    | Synthesis scale, purification, counterion, purity; yield in OD260 and nmol; mass by ESI-MS against the one its HELM gives                       |
| `iki/lots.PlasmidPrep`  | `PREP-000001`  | `iki/plasmid.Plasmid`                     | µg, by ng/µL   | Prep scale and method, kit, host strain; concentration from A260; A260/A280, A260/A230, supercoiled share, endotoxin; how it was verified       |
| `iki/lots.ProteinLot`   | `PLOT-000001`  | `iki/protein.Protein`, `iki/core.Complex` | mg, by mg/mL   | Concentration from A280 and its sequence's ε280; purity and aggregates by SEC; endotoxin; formulation                                           |
| `iki/lots.ConjugateLot` | `CJLOT-000001` | `iki/conjugate.Conjugate`                 | mg, by mg/mL   | As a protein lot's, with its own measured ε280; DAR from its drug load distribution (HIC, LC-MS, reduced chains), unconjugated share, free drug |
| `iki/lots.PolymerLot`   | `PYLOT-000001` | `iki/polymer.Polymer`                     | mg             | Mn, Mw, Mp, dispersity (Mw/Mn), how each was measured and against which standards                                                               |
| `iki/lots.GenericLot`   | `GLOT-000001`  | any entity                                | mg, by mg/mL   | Concentration and purity                                                                                                                        |

`iki/lots.BiologicLot` is the abstract parent of the protein and conjugate
lots: their A280, path length, dilution, ε280, concentration and
its method, SEC purity and aggregates, endotoxin and formulation.

## What every lot has

`iki/lots.Lot` (abstract) gives every kind:

- **Where it came from:** its lot number (its supplier's, or the lab's own),
  whether it was made or bought, its supplier and catalogue number, who made
  it, when it was made and received, and when it's retested and expires,
  with its storage conditions. Its expiry is after it was made, and its
  retest date by its expiry: the type's rules keep both at every save.
- **Its certificates of analysis**, as files (PDF, PNG, JPEG or CSV, up to 20
  of 50 MiB each): its supplier's, or the lab's, and the reports they cite.
- **How much of it is left** (ADR 0139): each kind keeps its amount in the
  unit it's made and used in, so an oligonucleotide lot's is in nmol and a
  protein lot's in mg. Each has a concentration field too, so it's drawn by
  volume: 50 µL of a 100 µM oligonucleotide lot is 5 nmol, 2 µg of a
  420 ng/µL plasmid prep is 4.76 µL. Every draw, transfer and split is a
  movement of its ledger. A draw records its purpose (routine use or QA
  sampling), who took the material and why. Routine use is the default.
  Taking a QA sample also requires Lot QA (`iki/lots.qa`) in the project
  that owns the lot. A transfer into a lot pools its concentration (ADR 0157).
  Its amount is of what it's a lot of
  (`amount.material`), so a transfer into it takes only another lot of the
  same molecule, and an aliquot of it is of that molecule too. A released lot
  takes only released material, whatever its concentration.
- **Its disposition**, which QA sets (the plugin's `iki/lots.qa` role, Lot QA):

  | State       | Locks it | Holds its amount | Signed                        | Set                                                                                                                                                                                                                                                                                                         |
  | ----------- | -------- | ---------------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | Quarantined |          |                  |                               | Where every lot starts, as GMP keeps material until it's tested and released (21 CFR 211.84). It can be drawn from, to be tested. Set again from Released or On hold, with a reason, to correct it.                                                                                                         |
  | Released    | ✓        |                  | QA release (approval)         | From Quarantined or On hold, on a certificate of analysis, saying whether it was made or bought, and for one made in the lab who made it; by someone in QA other than who made it or recorded it, signing in again (21 CFR Part 11), in Iki itself rather than through an agent. Not while a rule holds it. |
  | On hold     |          | ✓                |                               | With a reason, such as a deviation under investigation.                                                                                                                                                                                                                                                     |
  | Rejected    | ✓        | ✓                | QA rejection (responsibility) | With a reason. Kept, with its records, but not used.                                                                                                                                                                                                                                                        |
  | Expired     | ✓        | ✓                |                               | Once its expiry or retest date has come.                                                                                                                                                                                                                                                                    |

  For a lot made in the lab, the QA signer must also be independent of
  anyone who edited its record since it last entered Quarantined.
  Whoever made it hears when it's released, and when it's put on hold or
  rejected (which they can't turn off). The states are the abstract type's,
  and hold the amount of every kind, each in its own unit (ADR 0162).

  A lot's page also shows when its dates restrict use. Starting on the
  retest date, only a QA sampling draw is allowed, even for a released lot.
  Starting on the expiry date, all draws, transfers out and splits are
  blocked, including QA samples. The date check applies immediately, even
  before QA changes the disposition.

## Verify a plasmid prep

Open the physical `PlasmidPrep` and its **Verification** tab to find the
results for that prep. `verified_by` records the method. Each
`iki/lots.PlasmidVerification` preserves an evidence submission and its
review history, including discrepancies and inconclusive results.

1. Run `record_plasmid_verification` for the prep. Select an existing alignment
   of its reads, attach an externally reviewed report, or supply both. Record
   the observed outcome (`match`, `discrepancy` or `inconclusive`) and any
   notes needed to interpret it, such as incomplete coverage.
2. Iki checks that the alignment's reference is the DNA sequence registered
   for the prep's plasmid. It records which project plasmid record and
   revision the action read, along with the alignment and its revision or
   the uploaded file. The reference revision is captured when the verification
   action launches. The alignment retains its original file and the provenance
   of the comparison that produced it. These evidence references stay pinned
   through review.
3. Assign a Verification reviewer in the project. At deployment, the
   `iki/lots` setting `verification_flow` selects the review path for new
   verifications. The record keeps the flow selected at creation.

| Setting              | Signed transitions                                                                                                                    |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `research` (default) | From Recorded, the assigned reviewer signs off the outcome.                                                                           |
| `regulated_qc`       | A Verification analyst signs submission, an independent assigned reviewer signs review, and an independent Lot QA signer releases it. |

A correction to the outcome, notes or reviewer returns the result to Recorded
for a new review. Record new evidence as another verification of the same
prep. A prep can therefore have several reported observations, each with its
own reference revision, file and decision.

## Actions

Each kind with something to work out has an action, run on the backend tier
in `lot-tools` (Python alone), and two triggers: when a lot is recorded, and
when what it's worked out from changes. A project's Admins turn triggers on.
What can't be worked out (a measurement left out, a molecule's mass not yet
computed, a path length or extinction coefficient of 0, which the types'
rules refuse) is left empty, and the run says why. A computed value whose
inputs are gone is emptied.

A concentration or a DAR can also be entered, or changed by a pool, so what
a reading gives is kept beside it (`a260_concentration`,
`a280_concentration`, `distribution_dar`), and it follows that: set to a new
reading's result, emptied with a reading taken out if it was what the reading
gave, and kept if it was set since. So a pooled lot keeps its pool's
concentration until its A280 is read again, and a DAR by UV/Vis is as
measured.

| Action                  | Works out                                                                                      |
| ----------------------- | ---------------------------------------------------------------------------------------------- |
| `compute_oligo_lot`     | Expected mass, mass difference, yield in nmol                                                  |
| `compute_plasmid_prep`  | Concentration from A260                                                                        |
| `compute_protein_lot`   | ε280 used, from a protein or a complex; concentration from A280                                |
| `compute_conjugate_lot` | DAR and unconjugated share from the drug load distribution; ε280 used, concentration from A280 |
| `compute_polymer_lot`   | Dispersity                                                                                     |

### What's computed

- **An oligonucleotide lot's mass.** ESI-MS gives the neutral mass,
  deconvoluted from the charge states. It's compared with the
  oligonucleotide's monoisotopic mass (for a peak resolved to its isotopes)
  or its average mass (for an unresolved one), as the free acid, which the
  oligo plugin computes from its HELM. The rule `mass_within_tolerance` holds
  a lot whose difference is outside its tolerance (2 Da by default; set it to
  your specification) until QA reviews it; a held lot can't be released. It
  reads the measured mass and the oligonucleotide's at every save, not the
  difference the action writes, so an edit out of its tolerance is held as
  it's saved, triggers on or off; `mass_is_checked` holds a measured mass
  the oligonucleotide has no mass to compare with. The difference is exact,
  and compared to a micro-dalton: 5555.03254 against 5554.98250 is 0.05004
  Da, outside a tolerance of ±0.05001. Inotersen's average mass is 7183.08 g/mol;
  Prakash et al. (2014, Nucleic Acids Res 42:8796, Table 1) found 7182.2,
  0.88 Da light, within it. Volanesorsen's 7164.3 on a lot of inotersen is
  18.78 Da light, and held.
- **An oligonucleotide lot's yield in nmol**: OD260 × 10⁶ / ε260, an OD260
  unit being an absorbance of 1 over 1 cm in 1 mL. The ε260 is the lot's own,
  such as its supplier's, else its oligonucleotide's, which the oligo plugin
  works out by nearest neighbours only for natural DNA and RNA (ADR 0146): an
  MOE gapmer's needs its supplier's. M13 forward's ε260 is 182,800
  L/(mol·cm), so 1 OD260 is 5.47 nmol, as Eurogentec's worked example gives.
- **A plasmid prep's concentration**: A260 × dilution × 50 ng/µL / path
  length in cm, double-stranded DNA's 50 µg/mL for an absorbance of 1 over
  1 cm (QIAGEN; Sambrook & Russell, 2001), as a plasmid's sequence is
  double-stranded by `iki/plasmid`'s rules. Only while its concentration
  method is A260; a fluorescent dye's is entered as measured. Pure DNA's
  A260/A280 is about 1.8 and its A260/A230 2.0 to 2.2 (Thermo Fisher's
  NanoDrop bulletin T009).
- **A protein lot's or conjugate's concentration**: A280 × dilution /
  (ε × path length), with ε in mL/(mg·cm), the absorbance of 1 mg/mL over
  1 cm. It's the lot's measured ε280 if it has one, which a conjugate's must
  (its payload absorbs too); else what it's a lot of: its molar ε280 (Pace et al.,
  1995, Protein Sci 4:2411: 5500 for each Trp, 1490 for each Tyr, 125 for
  each cystine) over its molecular weight. A protein's are its sequence's,
  with its cysteines as cystines or reduced as the lot says; as cystines,
  each of its cystines takes two hydrogens (2.016 g/mol) off. A complex's, such
  as an antibody's, an Fc fusion's or a multispecific's, are kept with each
  thiol free (ADR 0161); as cystines, each disulfide its bonds place takes two
  hydrogens off and adds 125 L/(mol·cm), as its Composition tab shows, and so
  does each its parts have of their own (a Fab's, in a complex of the Fab and
  its antigen), which its composition counts for the project. When some may
  not be counted (its bonds are said to be incomplete, as an IgG2's are, or its
  parts' weren't counted yet), the run warns. Hen lysozyme's sequence (UniProt P00698, 19–147) gives 37,970 L/(mol·cm) and
  14,313.0 g/mol, less 8.06 for its four cystines: 37,970 / 14,304.94 = 2.6543
  mL/(mg·cm), within 0.6% of the 2.64 measured at 281.5 nm (Aune & Tanford,
  1969, Biochemistry 8:4579). Trastuzumab (KEGG D03257), two heavy and two light
  chains, is 145,197.41 g/mol and 213,380 L/(mol·cm) free; with its 16
  disulfides, 215,380 / 145,165.16 = 1.4837. Only while its concentration
  method is A280; BCA, Bradford and amino acid analysis are entered as
  measured.
- **A conjugate's DAR**: each drug load weighted by its species' share of the
  area, of the intact conjugate (by HIC or intact LC-MS); or, of its reduced
  chains, each chain's mean times the number of copies in its conjugate's
  registered antibody carrier. An IgG has two of each chain and a Fab has one.
  The reduced DAR remains empty until both counts are available. Shares are taken of
  their sum. Its unconjugated share is DAR 0's share of the intact species'
  peak area: the molar share only if each species responds alike at the
  detection wavelength (a payload that absorbs there makes it an estimate). Brentuximab vedotin by HIC (Agilent
  5994-3522EN, Table 1) is 3.29 (Agilent: 3.3), with 8.11% unconjugated;
  disitamab vedotin (Thermo Fisher AN002598) is 3.91; Adcetris's reduced
  chains (Agilent 5991-6559EN), an IgG1's, give 3.99 (Agilent: 4.0). Its rules
  keep a distribution to one kind, intact or chains, and a reduced one to both
  chains. A DAR by UV/Vis is entered, and kept whatever distribution there is.
- **A polymer's dispersity**: its molar-mass dispersity, Đ = Mw/Mn (IUPAC,
  2009), never below 1: the rule `mw_is_at_least_mn` refuses an Mw below its
  Mn. It's 1 when every chain has the same molar mass, which chains of one
  length but different composition needn't. Mn and Mw each say how they were
  measured. PSS's polystyrene standard PSS120219 (Mw 241,000, Mn 236,000) is
  1.021, NIST SRM 1478 (Mn 35,800 by membrane osmometry, Mw 37,400 by
  sedimentation equilibrium, its certificate of 1992) 1.045, and
  PolySciTech's mPEG-PLGA AK026 lot 250221RAI-A (Mn 29,184, Mw 38,560) 1.321.

## A kind of lot of your own

A plugin adds a kind of lot by extending `iki/lots.Lot`, or
`iki/lots.BiologicLot` for a protein's, with a reference to what it's a lot
of, its own amount naming that reference as its `material`, and its own
measurements; it depends on `iki/lots`. Its
lots have the same disposition, certificates and QA, and a search of
`iki/lots.Lot` finds them. The immutable `ConjugateLot.conjugate` points to
the registered conjugate. Its lot keeps the distribution measured on the
physical sample. The immutable `PolymerLot.polymer` points to the registered
polymer design. Each batch records its own Mn, Mw and measurement methods.
Two batches of the same polymer can therefore have different distributions
and dispersities. The deployment checks existing lot references when
upgrading (ADR 0136).

## Licences

The plugin is Apache-2.0. Its scripts use only Python's standard library.

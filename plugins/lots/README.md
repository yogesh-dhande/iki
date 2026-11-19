# lots

Iki's lots plugin: lots of any registered entity. A registry entry is the
molecule, the same for every project; a lot is the material, what was made or
bought of it at one time, which a label and a certificate of analysis name:
how much of it is left, what was measured of it, and whether QA released it
for use ([ADR 0162](../../docs/adr/0162-a-lot-is-a-projects-record-of-an-entrys-material.md)).

Every kind of lot extends one abstract type, so a search of `iki/lots.Lot`
finds them all, and each has the measurements its kind needs:

| Type                    | HRID           | Lot of                                      | Amount kept in | What's measured, and worked out                                                                                                                 |
| ----------------------- | -------------- | ------------------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `iki/lots.OligoLot`     | `OLOT-000001`  | `iki/oligo.Oligonucleotide`                 | nmol, by µM    | Synthesis scale, purification, counterion, purity; yield in OD260 and nmol; mass by ESI-MS against the one its HELM gives                       |
| `iki/lots.PlasmidPrep`  | `PREP-000001`  | `iki/plasmid.Plasmid`                       | µg, by ng/µL   | Prep scale and method, kit, host strain; concentration from A260; A260/A280, A260/A230, supercoiled share, endotoxin; how it was verified       |
| `iki/lots.ProteinLot`   | `PLOT-000001`  | `iki/protein.Protein`, `iki/core.Complex`   | mg, by mg/mL   | Concentration from A280 and its sequence's ε280; purity and aggregates by SEC; endotoxin; formulation                                           |
| `iki/lots.ConjugateLot` | `CJLOT-000001` | any entity, until conjugates are registered | mg, by mg/mL   | As a protein lot's, with its own measured ε280; DAR from its drug load distribution (HIC, LC-MS, reduced chains), unconjugated share, free drug |
| `iki/lots.PolymerLot`   | `PYLOT-000001` | any entity, until polymers are registered   | mg             | Mn, Mw, Mp, dispersity (Mw/Mn), how they were measured and against which standards                                                              |
| `iki/lots.GenericLot`   | `GLOT-000001`  | any entity                                  | mg, by mg/mL   | Concentration and purity                                                                                                                        |

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
  movement of its ledger, with who and why; a transfer into a lot pools its
  concentration (ADR 0157).
- **Its disposition**, which QA sets (the plugin's `iki/lots.qa` role):

  | State       | Locks it | Holds its amount | Signed                        | Set                                                                                                                                                                                                           |
  | ----------- | -------- | ---------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | Quarantined |          |                  |                               | Where every lot starts, as GMP keeps material until it's tested and released (21 CFR 211.84). It can be drawn from, to be tested. Set again from Released or On hold, with a reason, to correct it.           |
  | Released    | ✓        |                  | QA release (approval)         | From Quarantined or On hold, on a certificate of analysis, by someone in QA other than who made it, signing in again (21 CFR Part 11), in Iki itself rather than through an agent. Not while a rule holds it. |
  | On hold     |          | ✓                |                               | With a reason, such as a deviation under investigation.                                                                                                                                                       |
  | Rejected    | ✓        | ✓                | QA rejection (responsibility) | With a reason. Kept, with its records, but not used.                                                                                                                                                          |
  | Expired     | ✓        | ✓                |                               | Once its expiry or retest date has come.                                                                                                                                                                      |

  Whoever made it hears when it's released, and when it's put on hold or
  rejected (which they can't turn off). The states are the abstract type's,
  and hold the amount of every kind, each in its own unit (ADR 0162).

## Actions

Each kind with something to work out has an action, run on the backend tier
in `lot-tools` (Python alone), and two triggers: when a lot is recorded, and
when what it's worked out from changes. A project's Admins turn triggers on.
What can't be worked out (a measurement left out, a molecule's mass not yet
computed) is left empty, and the run says why. A computed value whose inputs
are gone is emptied.

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
  your specification) until QA reviews it, with the run as its reason; a held
  lot can't be released. Inotersen's average mass is 7183.08 g/mol;
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
  1 cm (QIAGEN; Sambrook & Russell, 2001). Only while its concentration
  method is A260; a fluorescent dye's is entered as measured. Pure DNA's
  A260/A280 is about 1.8 and its A260/A230 2.0 to 2.2 (Thermo Fisher's
  NanoDrop bulletin T009).
- **A protein lot's or conjugate's concentration**: A280 × dilution /
  (ε × path length), with ε in mL/(mg·cm), the absorbance of 1 mg/mL over
  1 cm. It's the lot's measured ε280 if it has one, which a conjugate's must
  (its payload absorbs too); else what it's a lot of: its molar ε280 (Pace et al.,
  1995, Protein Sci 4:2411: 5500 for each Trp, 1490 for each Tyr, 125 for
  each cystine) over its molecular weight. A protein's are its sequence's,
  with its cysteines as cystines or reduced as the lot says. A complex's, such
  as an antibody's, an Fc fusion's or a multispecific's, are kept with each
  thiol free (ADR 0161); as cystines, each disulfide its bonds place takes two
  hydrogens (2.016 g/mol) off and adds 125 L/(mol·cm), as its Composition tab
  shows. Hen lysozyme's sequence gives 37,970 / 14,313.0 = 2.6528 mL/(mg·cm),
  within 0.5% of the 2.64 measured at 281.5 nm (Aune & Tanford, 1969,
  Biochemistry 8:4579). Trastuzumab (KEGG D03257), two heavy and two light
  chains, is 145,197.41 g/mol and 213,380 L/(mol·cm) free; with its 16
  disulfides, 215,380 / 145,165.16 = 1.4837. Only while its concentration
  method is A280; BCA, Bradford and amino acid analysis are entered as
  measured.
- **A conjugate's DAR**: each drug load weighted by its species' share of the
  area, of the intact conjugate (by HIC or intact LC-MS); or, of its reduced
  chains, twice the light chain's mean and twice the heavy chain's, as an IgG
  has two of each. Shares are taken of their sum. Its unconjugated share is
  DAR 0's of the intact species. Brentuximab vedotin by HIC (Agilent
  5994-3522EN, Table 1) is 3.29 (Agilent: 3.3), with 8.11% unconjugated;
  disitamab vedotin (Thermo Fisher AN002598) is 3.91; Adcetris's reduced
  chains (Agilent 5991-6559EN) give 3.99 (Agilent: 4.0). Its rules keep a
  distribution to one kind, intact or chains, and a reduced one to both
  chains. A DAR measured another way, such as by UV/Vis, is entered.
- **A polymer's dispersity**: Đ = Mw/Mn (IUPAC, 2009), never below 1: the
  rule `mw_is_at_least_mn` refuses an Mw below its Mn. PSS's polystyrene
  standard PSS120219 (Mw 241,000, Mn 236,000) is 1.021, NIST SRM 1478 (Mn
  35,800 by osmometry, Mw 37,400 by sedimentation) 1.045, and PolySciTech's
  mPEG-PLGA AK026 lot 250221RAI-A (Mn 29,184, Mw 38,560) 1.321.

## A kind of lot of your own

A plugin adds a kind of lot by extending `iki/lots.Lot`, or
`iki/lots.BiologicLot` for a protein's, with a reference to what it's a lot
of, its own amount and its own measurements; it depends on `iki/lots`. Its
lots have the same disposition, certificates and QA, and a search of
`iki/lots.Lot` finds them. Once a plugin registers conjugates or polymers,
`ConjugateLot.conjugate` and `PolymerLot.polymer` narrow from any entity to
its types, `iki/lots` depending on it, and the deploy checks that the lots
already recorded fit (ADR 0136). Until then they name the conjugate or
polymer however it's registered, or the antibody a conjugate is made from.

## Licences

The plugin is Apache-2.0. Its scripts use only Python's standard library.

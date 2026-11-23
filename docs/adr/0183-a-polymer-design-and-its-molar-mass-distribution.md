# 0183. A polymer design and its molar-mass distribution

- Status: Accepted
- Date: 2026-11-22
- Refines: [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md)

## Context

A repeat unit describes atoms after incorporation into a chain. A monomer
record identifies the compound a lab used to make it. Several precursors
can contribute atoms to one repeat unit. BigSMILES describes the possible
connections in an ensemble. The relative populations and their molar
masses require measurements of a sample.
[BigSMILES v1.1 specification](https://olsenlabmit.github.io/BigSMILES/docs/line_notation.html)

## Decision

Register a synthetic polymer using its submitted BigSMILES representation
and the declared structural details that distinguish its design. Keeping
the submitted text preserves the order of stochastic objects, repeat
units, terminal descriptors and end groups. Equivalent spellings can
receive different registry entries. A lab can reuse its agreed spelling
across projects.

Register at least one precursor monomer as a compound reference. The
precursor list is a fixed fact of the registered design. An incorrect
precursor assignment requires a superseding registration.
Architecture, sequence distribution, end groups and other structural
details can describe distinctions that an abbreviated representation
leaves open. The inspector reports what it could check and displays the
repeat and terminal fragments it read. Inspection results belong to the
project record and can be refreshed as the inspector gains support for
additional syntax. The source notation stays available
when it contains a construct the inspector cannot interpret.

Each synthesis or reference material has an `iki/lots.PolymerLot` with a
typed reference to the design. The lot keeps its measured number-average
and weight-average molar masses, the measurement methods and calibration,
and the dispersity calculated as Mw/Mn.
[IUPAC recommendations on dispersity](https://publications.iupac.org/pac/81/2/0351/index.html)

## Consequences

A styrene compound, a polystyrene design and a physical polystyrene standard
have separate records with navigable references. A second standard can
cite the same design and record its own molar-mass distribution. For example,
the reported Mn of 35,800 g/mol and Mw of 37,400 g/mol for NIST SRM 1478
give a dispersity of 1.045 to three decimal places.
[NIST SRM 1478 certificate](https://tsapps.nist.gov/srmext/certificates/1478.pdf)

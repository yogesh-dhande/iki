# 0262. A mouse strain can be the lab's own, and a carrier says what it is

- Status: Accepted
- Date: 2026-11-26
- Refines: [0187](0187-mouse-strains-and-banked-germplasm.md)
- Builds on: [0260](0260-a-labs-storage-layout-and-containers-are-its-own.md)

## Context

ADR 0187 identified every mouse strain by its MGI accession. A colony also
keeps strains MGI doesn't list: in-house crosses, new founder lines and
strains received before publication. MGI identifies a strain; JAX, MMRRC and
EMMA each sell stocks of one, under their own numbers. The MGI import let an
operator type a JAX stock number, which was then saved with the MGI report's
provenance although the report has none.

Cryopreserved germplasm is kept in straws or cryovials. A straw is thawed
whole and is kept in a goblet or cassette, often in a dewar canister; a vial
of suspension can be drawn in part and sits in a box. A facility keeps the
donors' tissue or DNA beside them.

## Decision

**A strain is registered in MGI or locally.** `MouseStrain`'s identity is
where it's registered (`mgi` or `local`) with its MGI accession or the lab's
own strain ID, exactly one of them. Both identify the strain across the
registry's projects. Repository stocks are registry facts: the JAX stock
number, with its link, and other repositories' stocks as `REPOSITORY:ID`.

**The MGI import records only what the MGI report says.** It registers the
accession and designation with the report row's digest. A JAX stock comes from
the JAX catalogue and is entered on the strain.

**A cryostock says its carrier.** `carrier` is a straw or a cryovial. The µL
ledger is unchanged: a straw's filled volume is drawn whole into the result
that uses it, a vial's suspension in part. A carrier is drawn from, never
split or transferred, so each result keeps the carrier it came from.

**A mouse cryobox holds cryostocks and any inventory container.** Its
positions are the holder's: a vial box's grid, or a goblet's or cassette's
slots as one row. It sits wherever an inventory box does, including directly
in a dewar (ADR 0260).

## Consequences

A local strain can be genotyped, banked and traced like a catalogued one. If
MGI later registers it, the MGI entry is a separate strain, and existing
records keep the local strain they were made from. A lab that doesn't track
volume still enters the carrier's filled volume to link its use to a result.
Splitting the contents of a carrier after thaw makes new records of the lab's
own, linked by the draw.

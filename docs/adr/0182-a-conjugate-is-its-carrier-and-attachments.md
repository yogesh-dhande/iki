# 0182. A conjugate is its carrier and attachments

- Status: Accepted
- Date: 2026-11-22
- Refines: [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md)

## Context

A chromatogram can give a different mean drug-to-antibody ratio for each
lot of the same conjugate. In contrast, two defined oligonucleotides
with the same ligand at different termini are different molecules.
Registration needs to distinguish these cases and preserve links to
the carrier and attached compounds.

## Decision

The identity of `iki/conjugate.Conjugate` consists of one registered
carrier and the set of its attachment rows. Each row identifies a compound,
the stated site, its loading when fixed and its elemental loss per copy when
known. A site may be unlocalized. Sites are compared as entered, so a lab
should use a consistent chain and position convention. An attached compound
can represent a complete linker–payload. A discrete PEG chain can be
registered as a compound. For a polydisperse PEG, the row identifies its
precursor and states the grade.

The registry compares elemental losses by the number of each element and
isotope. For example, D2O and [2H]2O have one identity, as do H2O, OH2
and HHO. An unspecified loss differs from an explicit loss of zero atoms
(`none`). The entry retains the first submitted rows. A project that registers
its own record with a different spelling retains those rows in its creation
history. On upgrade an existing canonical key keeps its entry. Otherwise
the oldest active equivalent entry receives that key. Pre-existing duplicate
entries keep their identifiers and links for the registrar to reconcile.

An action computes a theoretical elemental formula for one defined species
by adding the carrier and each attached compound in its stated number of
copies, subtracting the declared leaving atoms per attachment. It records
the basis beside the formula and average and monoisotopic masses. For a
protein or antibody this model starts from reduced polypeptide chains.
For an oligonucleotide it starts from the registered free-acid formula.
Each project can rerun the action when its source records are completed.

A distribution of loadings, a PEG grade with a spread of chain lengths, or
missing composition data leaves the single-species formula and masses empty.
The composition tab shows the carrier, attachments and the reason.

## Consequences

The conjugate's registry entry is the material reference of
`iki/lots.ConjugateLot`. Each lot keeps its own distribution, measured DAR,
unconjugated fraction and concentration. For a reduced-chain distribution
the lot calculation uses the registered carrier's heavy- and light-chain
copy counts. Different physical lots can cite the same molecular identity
and retain different analytical results.

# 0147. Oligonucleotides are found by their bases

- Status: Accepted
- Amended by [0285](0285-a-sequence-names-its-residues.md): every sequence names its residues field
- Date: 2026-11-18
- Refines: [0037](0037-entity-search.md), [0144](0144-canonical-helm-is-an-identity.md)

## Context

An oligonucleotide's identity is its canonical HELM (ADR 0144), so a primer
with a 5′ biotin isn't the plain primer, nor a probe with its FAM and BHQ the
oligonucleotide without them. Scientists still look for oligonucleotides by
their bases: every probe against a target, every gapmer with a motif, the
plain and the labelled primer together. A HELM string is no help there: the
bases are scattered between sugars and linkers (`[MOE]([5meC])[sP].[dR](A)`),
and a modified base (5-methylcytosine) isn't its letter.

The dna plugin's primers have their bases as their identity, a different
type with a different identity, so a modified primer can't be one of them.

## Decision

**An oligonucleotide's entry keeps its bases**, computed from its monomers:
each base's natural analogue, 5′ to 3′, as HELM's libraries give it
(5-methylcytosine is C; T stays T, on a ribose too; a base with none is N).
Sugars, linkers and end groups aren't in it. It's indexed, as a string up to
600 bases, so `contains` and `starts_with` searches are fast (ADR 0037): a
search for `ACGT` finds the plain primer and the biotinylated one, and every
gapmer with that motif.

**An oligonucleotide is an `iki/core.Sequence`**, so searching sequences finds
it beside DNA and proteins, by name or description.

**It isn't a dna Primer**, and a search of primers doesn't find it. A cross-type
sequence search (one condition on DNA sequences', primers' and
oligonucleotides' bases at once) would need the same field on
`iki/core.Sequence`, which its subtypes name differently (`bases`,
`sequence`); that's a change to the core type, left for when it's needed.

## Consequences

- Plain and modified oligonucleotides are found together by their bases,
  and kept apart by their identity.
- An oligonucleotide longer than 600 nucleotides can't be registered: none
  synthesised as one is, and longer molecules are DNA or RNA sequences.
- Finding an oligonucleotide and a dna Primer with the same bases takes two
  searches.

## Alternatives considered

- **Searching the HELM text**: finds nothing across sugars, and a C would
  miss its 5-methylcytosine.
- **Bases as the identity, modifications as fields**: one record for the plain
  and labelled primers, which are different molecules with different weights,
  bought and used separately: what 61e set out to prevent.
- **Oligonucleotides as dna Primers with a HELM field**: the primer's identity
  is its bases, so the same conflation.

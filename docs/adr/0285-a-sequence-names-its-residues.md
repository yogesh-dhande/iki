# 0285. A sequence names its residues

- Status: Accepted
- Date: 2026-11-27
- Amends: [0147](0147-oligonucleotides-are-found-by-their-bases.md)

## Context

`iki/core.Sequence` is anything with a sequence of residues, and declares
only a name, a description and an organism. Each subtype keeps its residues
in a field of its own: a DNA sequence's and a primer's `bases`, a protein's
`sequence`, an oligonucleotide's `helm`. ADR 0147 left a field on the core
type "for when it's needed". Alignments, composition, exports and agents
reading a sequence through `iki/core.Sequence` had to guess which field to
read, and a lab's own sequence type, naming its field otherwise, was missed.

Moving every subtype's residues into one core column would change four
plugins' identities and their registry tables, and a HELM string isn't the
same kind of value as a DNA one. What's needed is to know which field it is.

## Decision

**Every concrete type extending `iki/core.Sequence` names its residues
field** under `roles` (ADR 0284), its own or a parent's
(`residues-unbound`): DNA sequences, plasmids and primers `bases`, proteins
`sequence`, oligonucleotides `helm`. The field's format is the residues'
alphabet. Its identity and storage don't change.

## Consequences

- Anything reading a sequence reads its residues by its role, and a lab's own
  sequence type is read the same way.
- ADR 0147's cross-type search of residues remains a search per type; the
  role says which field each one searches.

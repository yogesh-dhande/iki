# 0138. An identity reads sequences and sets of parts as what they are

- Status: Accepted
- Date: 2026-11-18
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md)

## Context

A registry entry is found again by the SHA-256 of its identity's values
(ADR 0115). Version 1 hashed each value as written, so one molecule could
get several identities:

- MKWV, mkwv and MKWV\* gave three different hashes, though they're one
  peptide. Lower case marks masked regions, and a translation that keeps its
  stop ends in `*` (F-003). DNA and primers were case-sensitive too.
- A circular duplex entered as AAGTCC, as its rotation GTCCAA, or as its
  other strand GGACTT gave three different hashes (F-056).
- An identity couldn't hold a list, so a complex couldn't be its parts. A
  complex is the same whatever the order its parts are listed in.

The published conventions differ on details. SEGUID v2 (Pereira et al., 2024) hashes a circular single strand by its least rotation, a circular
duplex by the least rotation of either strand, and a linear duplex by the
lesser of its two strands. Benchling and Geneious find duplicate plasmids
across rotations and strands, but don't say exactly how. GenBank keeps the
orientation and origin the submitter gave.

## Decision

**Identity version 2.** `VERSION` in `iki_spec.identity` is 2, and every
hash carries `"v":2`. A value is read by its field before it's hashed:

- **Case.** A string with a sequence format (`dna_sequence`, `rna_sequence`,
  `protein_sequence`) is upper case.
- **Stops.** A protein's terminal stops go: a stop ends a chain and isn't
  one of its residues. So MKWV\* and MKWV\*\* (a translation that ran past
  its stop codon pair) are MKWV. A stop inside the sequence stays, since
  MK\*WV is a different product (a nonsense mutant or a readthrough), so it
  isn't MKWV.
- **Alphabets.** DNA keeps T and RNA keeps U, each complemented in its own
  alphabet with IUPAC ambiguity codes (R↔Y, K↔M, B↔V, D↔H; S, W and N are
  their own complements). A DNA and an RNA are different molecules, and
  they're different types anyway, so neither is converted into the other.
- **Topology.** A sequence field's `topology` option names an enum field
  beside it with a `circular` value. A circular sequence is its
  lexicographically least rotation, found by Booth's algorithm in linear
  time. A cyclic peptide is one too: rotation only, since a peptide read
  backwards is another peptide.
- **Strands.** A DNA or RNA field's `strands` option names an enum field with
  `double` and `single` values. A double-stranded sequence is the lesser of
  its two strands, each read 5′ to 3′: for a circular one, the lesser of each
  strand's least rotation (F-056: AAGTCC, GTCCAA and GGACTT are all AAGTCC).
  A **linear duplex** is the lesser of the two strands, as SEGUID's
  ldseguid has it, because both strands are the same molecule read from its
  other end. A rotation of a linear sequence is a different sequence. A
  **single strand** is read only by rotation, if it's circular: its
  complement is a different molecule, which it would pair with. So GGACTT,
  as a single-stranded circle, is ACTTGG, not AAGTCC.
- **The fields it's read by are part of the identity.** A field that
  `topology` or `strands` names must be in the type's identity, required and
  immutable. Otherwise a circular AAGTCC and a linear one would collide.
  `iki/dna.DNASequence`'s identity is now `[bases, topology, strandedness]`,
  so a single-stranded M13 genome and its duplex are different entries.
  GenBank imports set `strandedness` from LOCUS (`ss-DNA`, `ds-DNA`).
- **Sets of parts.** A list in an identity is a multiset: each item encoded,
  sorted by its encoding, duplicates kept. The list can hold references, or
  values an identity can hold, or rows (ADR 0137) by the row fields its
  `identity` names (all of them by default). A complex registered with its
  parts in another order, or with other chain labels, finds its entry; two
  copies of a part aren't one. Lists of files can't be in an identity, since
  a list holds a file's ID, not its content's hash. An identity's lists
  refer only to entries, as its references do.

**What was submitted is kept.** The stored value is never rewritten to its
canonical form. An entry keeps its values as first registered: from that
origin, on that strand, in that case. A project's record of it made from
values spelled otherwise keeps those values, as given, on its first revision
(`submitted`, shown in its history and the API). The features a sequence
viewer shows along such a sequence are placed along the entry's: rotated by
the origin's offset and, from the other strand, reflected, with their strand
flipped. So the coordinates always match the bases they're shown along.
Registering again doesn't refuse an identity value spelled otherwise as a
change to the entry. It's the same value.

**Hashes are Iki's, not SEGUID's.** The canonical form follows SEGUID v2's
conventions, but the hash is still ADR 0115's SHA-256 of canonical JSON,
naming the type and every identity field. A sequence's SEGUID, where it's
wanted, is a derived field a plugin computes.

There's no reconciliation step. Before M1 no deployment holds entries
(databases may be rebuilt), so no version 1 hashes need rehashing.

## Consequences

- One molecule has one entry, however it's typed, from wherever it starts.
  Registration finds it with the same indexed lookup as before.
- A new hash version changes every hash. Any other implementation of the
  spec must implement version 2: `spec/tests/test_identity.py` has the test
  vectors.
- Each registration of a circular sequence costs a linear pass, and a
  duplex's costs two.
- Feature placement knows only the sequence viewer's features (`start`,
  `end`, `strand`). A plugin with other coordinate fields along a sequence
  places them itself, or declares them through a sequence viewer.
- Sticky ends and other partly double-stranded molecules aren't modelled. A
  linear duplex is read as fully paired.

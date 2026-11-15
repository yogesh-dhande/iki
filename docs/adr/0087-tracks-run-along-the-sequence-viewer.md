# 0087. Tracks run along the sequence viewer

- Status: Accepted
- Date: 2026-11-15
- Refines: [0085](0085-types-configure-ikis-own-viewers.md)

## Context

ADR 0085 expected per-residue values, such as a hydropathy profile, to come
as viewers of their own. But a value means something only beside the residue
it belongs to: a hydrophobic stretch is worth seeing because of the residues
in it and the features over it. ExPASy's ProtScale, Geneious and Benchling
all draw such profiles along the sequence, not apart from it.

## Decision

**A sequence viewer names its tracks.** Under `tracks`, each names a json
field holding one number per residue, or null where there's none, and
optionally a label. The validator checks that the field's schema is an
array of numbers, or of numbers and nulls. If the schema's items have a
`minimum` and `maximum`, the track's scale runs between them, so the same
profile reads the same on every entity; otherwise it fits the values. The
scale always includes zero.

**Each track is drawn under each row of residues,** a bar per residue in
the residues' own columns, rising above a baseline for values over zero and
falling below it for those under. Hovering a bar says its residue and
value. A legend says what each track is, its scale, and where its highest
and lowest values are, which is how a screen reader learns what the bars
show. A track whose field doesn't hold one number per residue isn't drawn,
and the legend says so.

## Consequences

- A plugin gets a profile drawn by computing a field and naming it: the
  protein plugin's `compute_properties` sets `hydropathy`, ProtScale's
  Kyte–Doolittle profile with its default window of nine.
- A track is part of the entity, so it has its history and is pinned with
  it in saved views (ADR 0086).
- A value per residue in an entity's values is about 5 bytes each. Profiles
  of the longest proteins, about 35,000 residues, fit; values along a
  genome belong in files, for a viewer that reads them.

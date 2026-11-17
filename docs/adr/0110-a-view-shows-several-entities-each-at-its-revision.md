# 0110. A view shows several entities, each at its revision

- Status: Accepted
- Date: 2026-11-17
- Refines: [0086](0086-saved-views-are-entities-pinned-to-a-revision.md)

## Context

ADR 0086 made a saved view an entity that shows one entity, at one
revision, in one viewer. The figures people make most often show more than
one thing: two structures superposed (apo and holo, a wild type and its
mutant, a prediction against the crystal structure), an antibody with its
antigen, a protein's structure beside its sequence with its domains.
PyMOL and ChimeraX sessions hold several models; Mol\* superposes
structures in one scene; Benchling's alignments show sequences side by
side. A figure of several entities has to pin each of them, since each
changes on its own.

## Decision

**A view shows an ordered list of entities, from 1 to 20.** `targets` lists
them, `target_revisions` the revision of each, and `viewers` the viewer each
is shown in, in the same order. Twenty is more than a figure holds and few
enough for Mol\* to draw at once. An entity may be in a view twice, in two
viewers, but not twice in one. The entities and their viewers can't change,
as before; saving moves the revisions.

**Each entity is in its own viewer, except structures, which share one
scene.** Every entity in the structure viewer is drawn in one Mol\* scene,
each as its own component. Each other entity has a viewer of its own, on
the view's page in the view's order, under a heading that names it. The
sequence viewer draws one sequence; several sequences side by side, aligned
column for column, are an alignment, which is an entity of its own (ADR
0088), so the sequence and alignment viewers show one entity each.

**The state is version 2: what each viewer keeps of each entity.**
`state.targets` has one object for each entity, in order. The sequence viewer
keeps the feature it showed and the alignment viewer where its cursor was,
as before. The structure viewer keeps, for each structure, its colour,
whether it's hidden, how it was moved (a 4×4 matrix, as Mol\* keeps it),
what it was superposed on and how closely (RMSD over how many residues), and
the residues selected, as ranges of each chain's author numbering. Its
camera is the scene's, in `state.camera`. Any entity can have a `label`,
such as apo or holo. Saving keeps the matrix rather than redoing the
superposition, so a view opens exactly as it was saved. The rule on
`core.View` checks that each entity's state is its viewer's, and that a
structure is superposed on another structure of the view. Views saved
before keep version 1, their one entity's state at the top, and open as
they did.

**Iki superposes structures as Mol\* does.** The view's page offers to
superpose its structures on the first. The frame pairs each one's first
polymer chain with the first one's by aligning their sequences, and fits
their α carbons (Mol\*'s `alignAndSuperpose`), then moves it. The page says
the RMSD over how many residues for each, and saving keeps it. A structure
with no polymer chain, such as a ligand's conformers, isn't moved and the
page says so. Moves made with Mol\*'s own controls are saved the same way,
since the frame reads each structure's transform, whatever made it.

**A view is read only by people who can read every entity it shows.**
Access applies it wherever entities are read (`access.visible`, and lineage
and tombstones alike): to anyone else the view doesn't exist, as with any
entity they can't read (ADR 0035). So a view never shows, names or counts
what someone can't read, through its page, a search, the grid, the API,
MCP or a bundle. This follows the rule that a reference to something
a person can't read reveals nothing (ADR 0035, ADR 0067). It differs from
a collection, which shows each reader the members they can read (ADR
0038): a view without one of its entities is a different figure, not
part of the same one. An entity purged with its plugin still counts as
readable, its tombstone shown as purged. Saving a view of an entity
someone can't read is refused as a reference to nothing they can read,
saying nothing of its revisions.

**Its page says when an entity has moved on.** Each entity says the
revision the view saved and, when it has changed since, the revision it's
at now. The switch shows every entity at its latest revision, and saving
then pins those.

**Saving a view adds other entities with the picker references use.** The
save dialog on a viewer's tab lists the entity and offers to add others,
each at its latest revision, in a viewer its type has. Each viewer's tab
lists every view the entity is in, in that viewer, and says what else it
shows. The API takes the lists as values, as for any entity, and MCP's
`create_view` takes the entities by ID or HRID, each with its viewer and,
if not its latest, its revision.

**One-entity views became views of a list of one.** The migration moves
each view's `target`, `target_revision` and `viewer` into its lists and
marks the old fields removed. History keeps the old fields as they were
recorded; each revision also gets the same values in the new fields, so
bundles and the revisions API read any revision in one shape. The same
values in a new shape is the second change history allows a migration,
after a quantity in a new unit (`ConvertUnit`). Rolling back keeps each
view's first entity.

## Consequences

- A figure of several entities is evidence of what each was when it was
  made, as a figure of one is.
- `core.View` is at schema version 3. A bundle from a deployment still at
  version 2 is refused until its plugin versions match, as for any type
  (ADR 0096).
- Reading entities checks, for views, that every entity they show is
  readable: one more subquery, only for queries that can return a view.
- A view can't yet lay its viewers out side by side, or render to an
  image; both come with figures for reports.
- Superposing on chains other than the first, or by structure alone
  (TM-align) for proteins whose sequences differ too much to align, is
  left to Mol\*'s own superposition controls in the frame, whose result
  saving keeps.

# 0086. Saved views are entities pinned to a revision

- Status: Accepted
- Date: 2026-11-14
- Refines: [0085](0085-types-configure-ikis-own-viewers.md)

## Context

People look at a sequence or a structure to make a point: this feature,
this pocket, from this side. They want to come back to it, share it, and put
it in a report or a figure. Benchling saves a map view; PyMOL and ChimeraX
save sessions; Mol\* saves snapshots. In a regulated lab, a figure in a
report has to show what the record was when the figure was made, not what
it became after an edit.

## Decision

**A saved view is an entity of the core plugin, `core.View`.** It has a
name and a description, the entity it shows (`target`), the revision it
showed (`target_revision`), the viewer (`viewer`) and what the viewer
showed (`state`). Being an entity, it has a project, permissions, history,
comments and search, as everything else does. Its target and viewer can't
change; a view of something else is a new view.

**It opens on its revision.** A view's page shows the target as it was at
`target_revision`, as the view saved it, and says so. A switch shows the
latest revision instead. Saving is explicit, and each save is a revision of
the view: what the viewer shows now, on the revision it shows. Nothing
moves a view to a newer revision on its own.

**Each viewer keeps its own part of the state.** The sequence viewer keeps
the feature it showed, by kind, name and place, not by its index in a list
that edits reorder. The structure viewer keeps where the camera was.
`state.version` says which shape the state is, so views saved now keep
opening as the viewers grow. The server checks, as a rule on `core.View`,
that the revision exists, that the target's type has the viewer, and that
the state holds only what that viewer keeps. A person can only save a
view of what they can read; for anything else, the rule says nothing of it.

**A viewer's tab lists its saved views**, newest first, and offers to save
what it shows, on the revision it shows.

## Consequences

- A view is evidence of what was shown: its revision, its creator, and its
  own history of saves.
- Views of a target follow the target's readers: a view of something a
  person can't read shows them nothing of it.
- A type that drops a viewer leaves its views saying so, rather than
  showing something else.
- A view doesn't yet render to an image; that comes with figures for
  reports.

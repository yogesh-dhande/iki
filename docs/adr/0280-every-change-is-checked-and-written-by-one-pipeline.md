# 0280. Every change is checked and written by one pipeline

- Status: Accepted
- Date: 2026-11-28
- Refines: [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0127](0127-plugins-rules-are-checked-at-every-save.md),
  [0080](0080-triggers-run-actions-when-entities-change.md)

## Context

ADR 0035 makes the edit rules hold for every caller, and ADR 0127 checks every save
against its type's rules. An edit of a record went through one service that did all
of it: it locked the record, refused what can't change, cleared the results of the
checks reading what it changed (ADR 0080), worked its computed values out (ADR 0297),
checked the core rules and the plugins' rules, wrote the values, the revision and the
history, recorded the files it named, told the assignees and the notifications its
type declares, held the record for a rule that holds, and fired its rechecks.

Other changes are made by other operations: a record's creation, a state change, a
signature that sets a state, archiving, a move to a superseded entry's replacement, a
registrar's curation, a holder filling in a registry fact, a lab list's options. Each
of these wrote its revision with the same private helpers, and did whichever of the
other steps its author remembered. Several checked less than an edit does. Because a
new operation can always do the same, a fix to one path left the others as they
were.

21 CFR Part 11 and GxP expect a record's audit trail and its checks to hold for every
change, whatever made it. Putting every write behind one path is the usual way to make
that true by construction rather than by review.

## Decision

**Every change to an entity's values or revisions goes through one pipeline**
(`iki_server.entities.pipeline`), in two stages that every operation calls.

Each operation still authorizes its change, locks what it changes, and plans it:

- a `Plan` names the entity, its revision's kind, the values it writes, who makes it,
  how and why;
- it also names the values the change reads as changed without writing them: the
  registry facts an edit fills in, the states an edit returns, and the entry values a
  move gives the record;
- it carries what the revision records (the project an entry's change came through,
  the entry it moved from, the state it sets and the signatures that set it);
- it carries the reason the record may be held for.

**`evaluate(plan)` checks a change before anything of it is written.**

1. It clears the results of the checks reading what the change changes, and marks the
   record **Needs re-verification** (ADR 0080).
2. It works the computed values out from what the record will hold (ADR 0297).
3. It checks the core types' rules and the plugins' rules that read what changes. A
   new record is checked against every rule.
4. A rule that refuses refuses the change. A rule that holds asks for a reason, and
   the record is held for it.
5. A state that locks the record isn't set while a rule would hold it (ADR 0128).

A state change and a signature that sets a state are evaluated as an edit is. So is a
move to a replacement entry, by the values its new entry gives the record.

**`commit(plan, checked)` writes it.** It writes the values, each touched table's
history row, and the revision, which webhooks and triggers are told of. It records the
files the values name, puts a new record in the project it's made in, and tells the people an assignment field now names (ADR 0122)
and the notifications the type declares. It clears the breaches of the rules the
record keeps now, holds the record by the rules that hold it, and runs again the
checks whose results it cleared, as whoever made the change.

A change of no values is a revision all the same, through `commit`: archiving,
restoring, superseding an entry, moving a record. An edit with nothing of its own to
write makes no revision, but still keeps its rules and holds.

`commit_alike` writes the same values to many records at once, in a few queries for
all of them. It's what a registry entry's change clears on the records holding it.

**Some plans are evaluated by the change around them.** For these, `commit` takes no
`checked`:

- a state an edit returns, which the edit's evaluation checks with its values;
- a registry entry's own values, which its records' rules are checked for: the saving
  record's by its save, the others' by their entry's fan-out (ADR 0115);
- a lab list's option, whose code and label its list checks (ADR 0270);
- a deploy's reworked computed values, which the deploy checks once they're all worked
  out (ADR 0297).

**Nothing else writes revisions.** An architecture test reads the server's code and
fails when any other module:

- calls the pipeline's writers;
- makes a `Revision` row;
- sets an entity's revision number;
- inserts into `iki_revision` in SQL.

Three modules are allowed to, each because the pipeline can't do its job for it:

- a bundle's restore (ADR 0096), which puts back a record's revisions as they were;
- a bulk import (ADR 0056), which inserts its records set-based;
- a plugin migration's data operations (ADR 0212), which run against the migration's
  historical models.

The test also fails if one of the three stops writing revisions, so the list can't go
stale.

## Consequences

- A save's rules hold whichever operation makes it. A new operation gets every step
  by calling the two stages, and can't write without them.
- Moves, state changes and signatures are now checked against the core rules and the
  database's `unique` and `no_overlap` rules too, before the database refuses them.
- A move writes what it clears in its own revision, as an edit would, rather than in
  one after it.
- The steps are in one module, so changing one (a new kind of notification, say)
  changes it for every operation.
- A registry entry's change still reaches the records holding it through the
  operations that change entries: a registrar's curation and a holder's fill call the
  fan-out (ADR 0115) themselves.

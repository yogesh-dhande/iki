# 0120. A state can lock a record, and the database holds the lock

- Status: Accepted (state fields and their locks amended by [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md))
- Date: 2026-11-17
- Refines: [0118](0118-a-record-moves-through-its-types-states.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0056](0056-bulk-imports.md),
  [0066](0066-a-run-reads-only-whats-in-its-project.md)

## Context

Once a sample's results are approved or released they mustn't change, by
anyone or anything, unless someone authorized reopens it and says why.
That's Part 11's protection of records, and how LIMS behave: LabWare
authorizes a sample and its results freeze, Benchling locks an entry once
it's signed. Iki writes a record's values from many places: the web app,
the API, agents, runs' results, bulk imports, moves between registry
entries (ADR 0115) and bundles. Any of them that forgot a check would let
a released record change.

## Decision

**A state can lock the records in it**: `locks: true`. While a record is
in such a state, its values can't change, it can't be archived or
restored, and it can't be moved to another registry entry. The only way
to change it is a transition out of the state, which its plugin limits to
roles and can require a reason for: a reviewer's "send back", QA's
"reopen". Sharing a locked record with another project still works:
that changes who reads it, not what it says.

**The services refuse each path**, saying which state locks it and what
takes it out: "SAM-000012 is Released, which locks it: it can’t be
changed until “Reopen” takes it out". An edit and an archive are refused
with 409 `refused`, and `can.edit` is false while a record is locked. A
run that would update a locked record is refused when it's launched; one
launched before the record locked has its results rejected as a conflict
(ADR 0066). A bulk import line that matches a locked record fails the
import, writing nothing (ADR 0056). A registry entry's move skips its
locked records rather than repointing them. A bundle import (ADR 0096)
brings a record's own revisions from the deployment it left, transitions
included, so it sets the record's state, and its lock, as the bundle has
them.

**The database refuses too.** `iki_entity.locked` follows the record's
state: a transition into a locking state sets it, one out clears it, and
both change the state while the record's row is locked. `sync_plugins`
puts a trigger on every table holding values of records that have states
(each type's table, and its lists of references or files), which refuses
an insert, update or delete for a locked record, whatever wrote it. A
migration that rewrites values without changing them, a unit conversion
(ADR 0033), sets `iki.unlocked` for its own transaction.

A locking state may declare a reasoned return on edit. Iki then performs the
authorized transition and edit in one transaction. The database lock still
covers every write to the protected values. [ADR 0179](0179-a-corrected-result-goes-back-for-review.md)

Each value revision after a lock is lifted carries a reason. The rules for signing a return or
reversal apply to every record type. [State locks and signatures amendment](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md#amendment-to-state-locks-and-signatures)

## Consequences

- A locked record's values are what they were when it locked, whichever
  of Iki's paths, or a path added later, tries to change them.
- Every correction to a locked record is a transition, by someone with
  its role, with the reason its plugin asks for, in the history.
- A pipeline that recomputes values has to reopen locked records first,
  as a person would.
- Each write to a guarded table reads one row of `iki_entity`, by its
  primary key.

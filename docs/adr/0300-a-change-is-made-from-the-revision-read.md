# 0300. A change is made from the revision the person read

- Status: Accepted
- Date: 2026-11-27
- Refines: [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0289](0289-a-change-names-the-revision-it-replaces-in-if-match.md)

## Context

ADR 0035 makes every change a revision and requires `If-Match`, so one
person never silently overwrites another's work, and ADR 0289 makes it the
one precondition for everything with a revision. The server can only check
the revision it's sent, though. The guarantee holds only if the web app sends
the revision the person actually read.

The web app built that header by hand at a dozen places, and each dialog
chose for itself how to keep the revision. Three ways coexisted: keep what
was read when the dialog opened (a list option's edit, an amount, a hold's
review), remount the dialog when the record moved on (a signature), or send
the record as the page has it now. The third is wrong: TanStack Query refetches
a record when the tab regains focus, so a dialog left open while a colleague
saved sent the colleague's revision with values the person never saw. Setting
a state, archiving, freezing a collection, sending a request and changing a
registry entry's facts all did this, and the server's check passed. Nothing in
the code told the next author which way was right, and more dialogs are coming
(lab-role actions, plugins' declared actions).

The two forms for a registry entry's facts, on the entry's page and on a lab
list's page (an option is an entry, ADR 0270), are the one operation on the
server (`entries.curate`) but were two forms with different handling of the
same refusal: one said to close it and start again, the other offered to load
the latest and dropped what had been typed.

## Decision

**One hook, `useRevisioned`, for every dialog and form that changes something
with a revision.** It keeps what's changed as it was read when the dialog
opened, and sends that revision (`ifMatch`). A refetch while it's open never
moves it. It returns:

- `read`: the thing as read, which the dialog shows and builds the change from;
- `ifMatch`: the header for that revision;
- `changedSince`: whether the page's copy moved on since;
- `changed` and `rereading`: whether it has said, or is about to say, that it
  changed;
- `submit(send)`: sends the change from `read`, unless the page's copy moved
  on. Then it sends nothing, takes the new revision as read, and says it
  changed; the person confirms again.

A refusal as stale (412) is handled the same way, once the page has read it
again. A form passes `onCatchUp`, which brings the new values in where the
person hadn't typed, keeps what both changed as this person typed it, says
which fields changed, and keeps the reason. A dialog whose change depends on
more than the record's revision says so with `version`: a signature includes
its registry entry's revision, and a hold's review the hold's `review_version`.
A change of either is a change. A signature or a hold's review asks for its
reason again after it catches up, because the reason was given for what the
person saw.

A form whose record can't be changed by them any more while it's open
(locked by a state, archived, or their access changed) says so as soon as the
page reads it: what happened, who did it and when where its latest revision
says, and that what was typed is still there to copy. It offers no save, so
nothing is sent only to be refused. The edit page reads the record again
whenever the tab comes back into view, so this shows before the person acts.

So the change is always made from what the person saw, and they're told when
that changed, before anything is sent from a revision nobody reviewed.

**The SDK has the two helpers anyone writing to Iki needs:** `ifMatch(revision)`
and `isStale(error)` (status 412), and `ifNoneMatchAny()` and
`isPreconditionRequired(error)` for ADR 0289's other cases. They're additive
to `@ikisci/sdk`. A request that isn't a person's reviewed change (saving a
view's camera, taking open work, a spreadsheet import's steps, which follow
the import's own revision) sends `ifMatch` directly.

**An eslint rule refuses a raw `If-Match` or `If-None-Match` literal** in the
web app outside `revisioned.ts`, so a new dialog can't build the header by
hand and skip the hook.

**One `FactsForm` changes a registry entry's facts,** on the entry's page and
a list's page, and adds a list's option. It's built on `useRevisioned`, so
both pages handle a colleague's change the same way.

**Pickers and page imports.** Two smaller internals settle with this. Every
combo box that finds as people type is one `SearchPicker<T>`, which says only
where it finds and how its choices read. A sheet's list cell keeps its own
for the grid's keyboard. And no module imports a page the router loads
lazily, with a budget on what the first page loads. That one keeps ADR 0029's
promise that pages load when they're first opened.

## Consequences

- The class of bug where a dialog sent a revision nobody reviewed is gone,
  not fixed at five places. A new dialog gets the right behaviour by using
  the hook, and the lint rule leaves no other way.
- Every dialog that changes a record gets the same, explicit "it changed"
  step, including dialogs that used to disable themselves for good after a
  412 (a hold's review). An open dialog whose record changed costs the person
  one more confirm, never a lost update.
- Third-party clients get `ifMatch`/`isStale` from the SDK instead of
  copying the header format.
- Pre-M1, there's nothing to keep: `useOpenedRevision` and the local
  `isStale` helpers are removed.

## Alternatives considered

- **Remount the dialog on a version key**, as the signature dialog did. It's
  right for one dialog, but only if each author remembers to key it, and it
  throws away what was typed in a form. A hook that the lint rule enforces
  doesn't depend on that.
- **Turn off refetch-on-focus inside dialogs.** It would hide the colleague's
  change rather than say it, and a refetch has other causes (a mutation's
  invalidation).
- **Send the revision as the page has it and rely on the server.** That's
  the bug: the server can't tell a revision the person saw from one a
  refetch swapped in.

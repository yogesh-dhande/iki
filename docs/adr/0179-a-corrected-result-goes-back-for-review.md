# 0179. A corrected result goes back for review

- Status: Accepted
- Date: 2026-11-22
- Refines [0120](0120-a-state-can-lock-a-record-and-the-database-holds-the-lock.md),
  [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0128](0128-guards-say-when-a-state-may-be-set.md) and
  [0149](0149-a-result-leaves-its-service-only-by-release.md).

Updated by [ADR 0211](0211-lots-inventory-samples-and-requests.md). A QC deliverable now returns from Submitted or Reviewed to Recorded for correction. A reviewed sample returns to Retesting with a signed reason and starts a fresh testing cycle.

## Context

An analyst submits a measurement, a reviewer checks it, and QA releases
it to the project that asked. A third person can edit the measurement in
Draft. Checking only the creator and the submitting analyst would let that
person review their own work. After review, correcting even a transcription
error took two saves. Someone had to return the record to Draft and then
make the edit, and the first save could succeed even if the edit failed.

## Decision

A locking state may name the state to return to when a person corrects a
value. For example, the result's review field declares the following state.

```yaml
reviewed:
  label: Reviewed
  set_by: [reviewer]
  locks: true
  signatures: [{ meaning: review }]
  on_edit: draft
```

The edit form tells the editor where the record will go before they save
and asks why the value is changing. The editor sends the reason and the
revision they saw with the ordinary PATCH. Under the record lock, Iki
checks the editor's permission to change the values and to enter the
destination, including its guards. The destination must be live, unlocked,
and enterable without signing. A separate lock with no correction path
continues to protect the record. The API exposes the correction action
through `can.correct`. If several fields return, Iki sets them in their
declaration order. Each destination's guards see the preceding transition,
so a later guard can still refuse the save. Archiving, restoring and
registry moves require an unlocked record.

The return and the edit commit together. The state change and value edit
each get a numbered history entry, in that order, with their author and
reason. A validation failure or stale revision rolls back the whole save.
Submitting the same stored value leaves the review in place. Run results
and bulk imports cannot reopen a locked record.

Guards can read `edits.<state field>.<state>`, the value edits since the
most recent entry into that state. Each edit identifies its editor and
the record-owned fields changed. A shared registry entry has its own
history. A record starts in its initial states at creation.
Returning to a state starts a fresh window. A state the record has never
entered has an empty list. A reviewer who changes a value and changes it
back still appears in the list. For example, this guard on an analysis
result's Reviewed state checks the current round of work.

```cel
edits.review.draft.all(e, e.by != actor.person)
```

An old signature continues to verify against the revision it signed.
For an AnalysisResult, a correction requires a new submission and review.
When a result's release field has a separate state with a `review`
signature and the release itself requires signing, QA's signed document
identifies the reviewer's revision, its content digest and the signatures
that established review. The requesting project's request detail includes
`result_releases`. Each receipt names the delivered revision and, when
signed, the revision QA signed. The reviewed revision identifies the
snapshot the reviewer saw. Each is readable through the result's numbered
revision endpoint.

## Consequences

For a typical release, the reviewer signs revision N. That signature puts
the result in Reviewed at N+1. QA signs N+1 and the release creates N+2,
which the requester sees as Released. The three numbers identify different
acts over the same measurement. If an edit intervenes, the later release
must refer to the new review. A signed result delivered this way stays
locked in its released state.

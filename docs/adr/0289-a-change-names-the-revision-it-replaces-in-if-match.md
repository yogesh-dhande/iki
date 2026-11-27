# 0289. A change names the revision it replaces, in If-Match

- Status: Accepted
- Date: 2026-11-27
- Refines: [0027](0027-rest-api-conventions-and-service-accounts.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0139](0139-a-records-amount-is-a-ledger-of-what-moved.md),
  [0178](0178-spreadsheets-are-imported-by-mapping-previewing-then-one-run.md),
  [0180](0180-where-material-is-and-how-its-received-moved-and-counted.md)

## Context

ADR 0035 settled optimistic concurrency for records: a read returns the
revision as its `ETag`, an edit sends it back as `If-Match`, a missing one is
refused (428) and a stale one too (412). As more things gained revisions, the
API grew four ways of saying the same thing:

- `If-Match` on 14 operations: record and registry entry edits, states,
  signatures, spreadsheet imports, and a lab list option's edit;
- a custom `If-Balance-Revision` header, holding an amount's `updated_at`
  string, on a recount;
- revisions in request bodies: a receipt's and a count's `plan_revision`, a
  line's `line_revision`, a saved mapping's `target_revision`;
- nothing at all, on other changes to revisioned things: superseding an entry,
  archiving, restoring or superseding a list option, moving a record to the
  entry that superseded its own, discarding a spreadsheet import, deleting a
  saved mapping.

A stale revision answered 412 `stale` in some places, 409 `stale_balance`,
`count_lines_changed` or `hold_changed` in others, and 412
`precondition_failed` for a malformed header. The OpenAPI document declared no
response headers, so no `ETag`, and no 412 or 428 responses. A client, the
SDKs and MCP needed to know each endpoint's mechanism by heart. After M1,
making a precondition required, or moving one out of a body, breaks every
client.

## Decision

**What has a revision.** A record (entity), a registry entry, a lab list's
option (an entry), a spreadsheet import, a saved spreadsheet mapping, a
receipt and each of its lines, a count (reconciliation) and each of its lines,
and a record's amount. Each one's representation carries `revision`, a number
that goes up by one with each change to it, and its ETag is that number,
quoted: `"7"`. Reading one on its own (`GET /entities/{ref}`, `/entries/{ref}`,
`/lists/{name}/options/{code}`, `/spreadsheet-imports/{id}`,
`/receipts/{id}`, `/reconciliations/{id}`, `/entities/{ref}/amount`) answers
its `ETag`, and so does each change to it. A receipt's and a count's lines
are read inside them, with their own `revision`. A receipt's `plan_revision`
and a line's `line_revision` are now each one's `revision`. An amount, whose
`updated_at` served as its revision, has a revision of its own.

**A change that replaces what's recorded names the revision it replaces, in
`If-Match`, on the thing it addresses.** That is every `PATCH`, `PUT` and
`DELETE` of something with a revision, and every action that changes one:
setting a state, taking, submitting, signing, reviewing a hold, freezing a
collection, moving a record to a superseding entry, superseding an entry,
archiving, restoring or superseding a list option, recounting an amount,
changing, previewing, committing or discarding a spreadsheet import, saving
or deleting a saved mapping, describing, completing or cancelling a receipt
(and previewing its put-away), correcting, removing or restoring a receipt
line, and resolving, completing or cancelling a count.

- No `If-Match`, `If-Match: *`, a list of ETags or a weak one answers **428**
  `precondition_required`. `*` would skip the check (ADR 0035), and one
  change is made from one reading.
- An ETag that isn't the current one answers **412** `stale` with the
  current `ETag`, and changes nothing. Read it again, then decide.
- Every stale revision is 412 `stale`. `precondition_failed`, `hold_changed`,
  `stale_balance` and `count_lines_changed` are gone. A receipt or count that
  moved on still says which lines changed, in `affected_lines`.

**A change that only adds needs none,** as a draw never did: a draw, a
transfer, a split into aliquots, a scan into a receipt or a count, a
receipt line's first check, a new record or option. Two of them at once are
two additions, and neither loses the other. A first check of a line someone
has already checked still answers `found: "again"` and leaves theirs
(ADR 0180); correcting a check takes the line's `If-Match`.

**When nothing is recorded yet, `If-None-Match: *` says so** (RFC 9110). A
recount of a record with no amount sends it instead of `If-Match`, and is
refused (412 `stale`) if someone recorded one first. It replaces
`If-Balance-Revision: unrecorded`.

**A change that reads other things names their revisions in its body.** One
request has one `If-Match`, for the thing it addresses. The others it
depends on are named where they are used, and checked in the same
transaction:

- reassigning many records: `expected_revisions`, by record (ADR 0122);
- reordering a lab list: `expected_revisions`, by option code, as each option
  that moves gets a revision. Without it the second of two curators
  reordering at once would silently undo the first's order;
- scanning a count: `reviewed_lines`, the lines it re-observes;
- signing: `entry_revision`, the registry entry the signer reviewed with the
  record, which the signature records (ADR 0126);
- reviewing a hold: `review_version`, which the audit records as what the
  reviewer saw (ADR 0127);
- replacing a saved mapping (`PUT /spreadsheet-mappings/{id}`, with the
  mapping's `If-Match`): `import` and `import_revision`, the import whose
  reviewed mapping replaces it (ADR 0178). It used to be a `POST` to the
  import with `replace` and `target_revision`, which put the precondition of
  what was overwritten in the body.

Each is required. Leaving one out answers 428 `precondition_required` and a
stale one 412 `stale`, naming which (`stale`, `affected_lines`). The one
exception is a count scan's `reviewed_lines`, where an empty list is a real
answer (the scan re-observes nothing recorded): leaving it out reads as
empty, so a scan of a slot whose lines it didn't name is 412 `stale`, with
those lines. A saved mapping's inputs refuse members they don't take, so a
client still sending `replace` gets 422 rather than a new mapping.

**A preview that came out differently is a 409, with the new preview.** A
move plan (`moves_plan_changed`), a starter's plan (`starter_plan_changed`)
and a bundle's plan (`stale_plan`) are what a change would do, not a
revision of anything: the answer carries the plan to review again.

**A read may carry `If-Match` to stay on one revision.** The rows of a
spreadsheet import's preview are read a page at a time. Sending the
import's ETag on each page answers 412 if a newer preview replaced the one
being read, as RFC 9110 (§13.1.1) allows and S3 does for ranges.

**Settings have no revision.** A project, team, registry, webhook, saved
search, budget, quota, worker, user, service account, dashboard or intake
choice takes no precondition. An Admin changes them, a `PATCH` changes only
the fields it names, and each change is in the audit log (ADR 0025). GitHub's and Slack's APIs don't take one on settings either.
Accepting an optional `If-Match` on them later is additive.

**The OpenAPI document says so.** Each operation that takes `If-Match` or
`If-None-Match` declares it, and its 412 and 428 responses. Each read and
change of something with a revision declares its `ETag` response header, as
does each 412. A test fails when an operation takes a precondition without
declaring them, or a revisioned resource's read doesn't declare its `ETag`.

**Clients spell it once.** The Python SDK takes `revision=` on every change
it makes and sends it as `If-Match`; a recount of nothing recorded takes
`revision=None`. The MCP tools take `revision` likewise. The web app sends
the revision a form or dialog was opened at (`useOpenedRevision`).

## Consequences

- Positive:
  - One mechanism: a client reads `revision`, sends it back as `If-Match`,
    and handles 428 and 412 the same way everywhere. The SDKs and MCP have
    one helper for it.
  - Changes that were unprotected (superseding an entry, archiving a list
    option, discarding an import, deleting a mapping, reordering a list) can
    no longer silently undo someone else's.
  - The document tells a generated client which operations need a
    precondition and where to find the ETag.
- Negative / trade-offs:
  - Every changed endpoint's clients change at once: the web app, both
    SDKs, MCP and their tests. Pre-M1 nothing else depends on them.
  - An amount's revision is a new column, advanced by every movement.
  - Settings stay last-write-wins on the fields two Admins change at once;
    the audit shows both.

## Alternatives considered

- **ETags from a hash of the representation, on everything.** No revision
  columns, and settings would be covered too. But a representation shows
  what the reader may do and derived values (holds, guards, what's
  awaiting), which change without the thing changing, so every change would
  risk a false 412, and the ETag would differ by reader.
- **Optional `If-Match` everywhere, checked when sent.** Additive and easy,
  but ADR 0035 requires it for records because a client that forgets it
  overwrites someone's work, and a lab's records need the same guarantee
  whatever they are.
- **A compound ETag for a record and its registry entry.** It would cover
  signing in one header, but every edit of a record would fail when only
  its entry changed. The entry revision a signature records stays in the
  body.
- **Moving the preview rows' `If-Match` to a query parameter.** RFC 9110
  already defines it for reads, and a parameter would need its own stale
  answer.

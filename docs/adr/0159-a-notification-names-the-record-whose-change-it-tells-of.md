# 0159. A notification names the record whose change it tells of

- Status: Accepted
- Date: 2026-11-19
- Refines: [0129](0129-types-declare-their-notifications.md),
  [0142](0142-a-request-tells-its-service-and-the-project-that-asked.md),
  [0149](0149-a-result-leaves-its-service-only-by-release.md)

## Context

A type's notification can be about one record and tell of a change to
another: a request's `released` notification is about the request, and
tells of a result's release (ADR 0142); a `field` notification on a sample
tells of a change to the protein it refers to. ADR 0129 folds a person's
notifications of one kind into their open one, unread, not emailed and
recent, by the records it's about.

So when a second result of a request was released while its first
result's notification was still open, nothing changed. The notification was
about the request already, so it was left as it was: no new result was
named, and nothing was sent live. The audit listed the person as told. ADR
0149 says every release is told.

## Decision

**A notification names the record whose change it tells of.** As well as
the record it's about, it lists the record that changed, when that's
another: the released result, or the protein a sample refers to. The
person must be able to read both, as ADR 0142 says already. The named
record is listed with it in the inbox, as the records it's about are, and
isn't counted among them.

**Something new updates the open notification.** If the person's open
notification doesn't name the changed record yet, it's updated to name it
too, and the person hears of it live, as a new comment updates a thread on
GitHub. Its title and count stay as they are, as it's about the same
records. If it names it already, nothing changes.

**The audit says how each person was told.** Each person in
`notification.sent`'s `told` has `notice`: `new` for a new notification,
`updated` for their open one updated, or `unchanged` for an open one that
told them of it all already.

## Consequences

- Each result released is told, however many are released before the
  first notification is read.
- A notification can list more records than its count, the ones it names
  beside those it's about.

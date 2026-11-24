# 0207. Commit the plan that was reviewed

- Status: Proposed
- Date: 2026-11-23

## Context

An operator can review a plan and confirm it later. During that interval another
operator or a background task can change the choices. Two plans can have the same
totals while naming different records or destinations. When Iki records an
approval and its reason, both need to refer to the choices the operator saw.
[Bundle imports](0096-bundles-carry-entities-between-deployments-with-their-history.md) · [Spreadsheet imports](0178-spreadsheets-are-imported-by-mapping-previewing-then-one-run.md) · [Storage reconciliation](0180-where-material-is-and-how-its-received-moved-and-counted.md)

## Decision

An approval authorises the exact plan presented for review. This applies when
saving or clearing an intermediate decision and when completing the work. Each
workflow can bind the approval to the review in either of two ways.

- Submit the concrete choices shown to the operator, including the affected
  records and their destinations. In the write transaction, lock the relevant
  records, check that those choices still apply and commit them together.
- Return a revision or plan identifier with the review. The client sends that
  identifier with the approval. Check it under the same lock as the write.
  Advance the revision whenever a change affects what the operator approves.
  If a write depends on two independently changeable records, check both.

Refuse a stale approval before recording any decision, attribution or effects.
Show the changed plan and ask for a fresh decision, including a new reason or
acknowledgement when the workflow requires one.

## Consequences

The approval, its author and the resulting changes refer to the same review.
Operators may need to repeat a review when the plan changes.

## How to test it

For each endpoint that records an approval, use two authorised clients. Have the
first review a plan, then let the second save a different valid choice. Submit
the first client's original request. Include an example with equal totals and
different records or destinations.

Read back the records, decisions, authors, reasons, revisions and audit events
after the refusal. They should match their state immediately before the stale
request. Then review the replacement and verify that a fresh approval applies
the displayed choices and attributes them to its author.

When introducing a required precondition, exercise the first-party callers
through their normal entry points, including the web app, SDKs, command-line
tools and deployment checks. Keep the raw HTTP examples consistent with the
request accepted by the server.

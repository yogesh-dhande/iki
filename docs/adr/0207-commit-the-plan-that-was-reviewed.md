# 0207. Commit the plan that was reviewed

- Status: Proposed
- Date: 2026-11-23

## Context

Two tabs can preview different sheets of the same import. In a reproduction, the
first tab approved a one-row preview of sheet A after the second tab had prepared
a one-row preview of sheet B. The import wrote B's row and stored the reason
entered for A. The row's provenance identified B correctly. The approval still
referred to content the person had never reviewed. [Spreadsheet imports](0178-spreadsheets-are-imported-by-mapping-previewing-then-one-run.md)

A storage reconciliation showed the same issue after every line was valid.
One editor reviewed a move to A3. Another editor rescanned the tube at A4 and
authorised that move. The first editor’s old completion then recorded A4 under
their identity with the note “Completed count after reviewing A3”. In a separate
test, the line's Resolve endpoint attached the first editor's name and note
“Reviewed A3” to A4 before anyone completed the count. In a bulk
move, a next-free allocation similarly replaced a displayed pair of slots.
In a signed bundle import, a registry change swapped which of two records
kept its source identifier while every aggregate count remained equal.
[Storage reconciliation](0180-where-material-is-and-how-its-received-moved-and-counted.md) · [Bundle imports](0096-bundles-carry-entities-between-deployments-with-their-history.md)

## Decision

**An approval authorises the exact content that was reviewed, and is void if
that content changes.** Every write that records who approved what is an attestation.
The server binds each such write to the revision the person was looking at.
This includes saving or clearing a decision as well as completing the plan. Field-level validation answers “Is each value valid?” The identity of
the reviewed revision answers “Is this the thing the person approved?” A valid
replacement passes the first check and still requires a fresh approval. Iki
binds a write to its review in either of two ways.

- The client submits the concrete choices it displayed, including the affected
  records and their destinations. Under the write lock, the server checks that
  each choice still applies and commits those choices together.
- The server gives the reviewed plan an identity, such as an import's preview
  round, a reconciliation revision, or the identity of an approved bundle
  plan. The client returns that identity with its approval. Under the same
  lock used for the write, the server checks that the identity still names the
  current reviewed plan.

A change that affects a reviewed choice invalidates its approval, even if the
replacement has the same number of rows, the same totals, or the same label.
The server refuses the stale commit before writing records or recording the
approval. The client presents the current plan for fresh review and accepts
a new decision and reason.

## Consequences

The recorded approval and the resulting changes can be traced to the same
review. Someone whose plan went stale must look at the changed choices again.

## How to test it

Enumerate every endpoint that records a person's identity or note. Exercise
each one while a second authorised editor changes the content underneath it.
Include intermediate decisions and clears. Keep the first editor's request
open, have the second editor authorise a different but valid replacement,
then submit the original request. Use equal counts where possible so the
test distinguishes plan identity from totals.

Verify that the refusal preserves the records, decisions, authors, reasons,
revision and audit history. Then have the first editor review the replacement
and submit a fresh decision. Verify that the resulting write and audit refer
to exactly what that editor saw.

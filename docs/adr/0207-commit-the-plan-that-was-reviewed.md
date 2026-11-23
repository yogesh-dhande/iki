# 0207. Commit the plan that was reviewed

- Status: Proposed
- Date: 2026-11-23

## Context

Two tabs can preview different sheets of the same import. In a reproduction, the
first tab approved a one-row preview of sheet A after the second tab had prepared
a one-row preview of sheet B. The import wrote B's row and stored the reason
entered for A. The row's provenance identified B correctly. The approval still
referred to content the person had never reviewed. [Spreadsheet imports](0178-spreadsheets-are-imported-by-mapping-previewing-then-one-run.md)

## Decision

**An approval authorises the exact content that was reviewed, and is void if
that content changes.** At commit, the server verifies the plan shown to the
person. Iki supports two ways to bind the write to that plan.

- The client submits the concrete choices it displayed, including the affected
  records and their destinations. Under the write lock, the server checks that
  each choice still applies and commits those choices together.
- The server gives the reviewed plan an identity, such as an import's preview
  round. The client returns that identity with its approval. Under the same
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
A regression test should hold one person's approval open, replace the plan
with an equally sized plan affecting different records, then submit the old
approval and verify that it writes nothing.

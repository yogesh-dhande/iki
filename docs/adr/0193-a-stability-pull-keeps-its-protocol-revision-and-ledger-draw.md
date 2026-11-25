# 0193. A stability pull keeps its protocol revision and ledger draw

- Status: Accepted
- Date: 2026-11-23
- Builds on [0139](0139-a-records-amount-is-a-ledger-of-what-moved.md), [0179](0179-a-corrected-result-goes-back-for-review.md) and [0180](0180-where-material-is-and-how-its-received-moved-and-counted.md).

Updated by [ADR 0211](0211-lots-inventory-samples-and-requests.md). Stability pulls now use `iki/inventory.Tube`; its immutable contents reference identifies the study lot.

## Context

A stability coordinator needs to know what to take from storage, which
window applies and whether the sample was pulled on time. A later protocol
revision must leave the original schedule and observations readable.

## Decision

A study names its material, lot, protocol revision, storage conditions,
time points, tube and volume assignments and tests. One action computes
the pull dates and creates all the pulls together. It writes against the
study revision used to compute the schedule. Concurrent runs cannot
generate a second schedule for that record. The protocol is immutable.
A replacement design can link to the previous study and its records
remain available.

A pull is a target of a draw from the scanned storage tube. The type's
`draw_target` declaration makes the existing amount service enforce the
assigned tube, its lot, the planned quantity, the authorized role and
one draw per pull. It preserves the source lot's normal disposition,
expiry and draw-purpose checks. The same transaction locks source, lot and
target before recording the movement, so a retry cannot debit the tube
again. Other amount draws keep their existing policy. Storage shows the
tube's position and custody history. The amount movement identifies the
tube, lot, pull, amount, scan, actor and time.

A coordinator reconciles the pull after the draw. Its guard checks the
ledger date in UTC and requires a note for a draw outside the scheduled
window. A draw excludes a Missed transition. A pull with no draw can be
marked Missed only after the window and with a reason. Completion locks
the record. A study can contain both in-window and documented deviations
without moving any scheduled dates.

A test action copies the method and reporting unit from the pinned study
into a measurement of a completed pull. The project deployer chooses
Research or Regulated QC before scheduling. A study pins its choice at
schedule time, covering measurements made later. The research route
needs one signature. The QC route needs three, with the independent
reviewer and QA separated from the analyst, editors and each other.
Corrections return to Recorded, with a reason, until the result is final.
A study tab compares signed-off or released results within the same
condition, attribute, method and unit.

## Consequences

The existing draw endpoint and forms work for stability, and a failed
reconciliation leaves an auditable draw that the coordinator can finish.
Labs supply their own material, test methods and pull windows. The plugin
ships a cited design example. A dedicated versioned starter would add value
when a site has a validated protocol whose storage assignments and test
methods it wants to distribute together.

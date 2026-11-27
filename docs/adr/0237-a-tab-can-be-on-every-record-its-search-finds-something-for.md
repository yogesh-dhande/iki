# 0237. A tab can be on every record its search finds something for

- Status: Accepted
- Date: 2026-11-26
- Refines: [0117](0117-components-show-many-entities-in-more-places.md),
  [0211](0211-lots-inventory-samples-and-requests.md)

## Context

`iki/equipment` pins the calibration in force when a result was measured (an instrument use)
and opens an impact case for each result a failed calibration may have affected. The
result can be any record: a QC deliverable, a plate assay's fit, a sample. Its two
components showed that evidence on the instrument's and the service record's pages, and on
one result type's: `iki/requests.QCDeliverable`.

That one placement made equipment depend on `iki/requests`. A lab that only books and
services its instruments installed service requests to get them. Meanwhile an assay result
measured on a failed instrument had no tab saying so, because it wasn't a QC deliverable.

A component's tab is on the pages of one type and its subtypes. Nothing let a tab follow
a reference that can name a record of any type.

## Decision

**A tab for `iki/core.Entity` is on every record's page where its search finds something
for the record.** Its search names the record with `{{ entity }}`, which the validator
requires (`tab-on-every-record`). The page asks for one entity of the tab's search, as the
reader may read it, before drawing its tabs. A record it finds nothing for has no such tab.
A search the server refuses keeps its tab, so the tab's page can say why. If the component
also has a tab for one of the record's own types, the nearer one is used, as before.

**Equipment's evidence is on every measured record.** Its Instrument evidence and
Calibration impact tabs name `iki/core.Entity` instead of QC deliverables. Equipment
depends only on `iki/lab_roles`.

**A calendar shows every booking on its month's days.** It asks for the bookings that
start before the month ends and end after it starts, and draws each on every day it
covers. Before, a booking was drawn on the day it started and asked for by its start, so
an overnight run from the last night of a month wasn't in the next month's first day.
`ends_at` is indexed for that search.

## Consequences

- A record's page makes one request for each installed tab on every record. Equipment
  adds two.
- A lab can install equipment without requests, or the inventory that requests needs.
- The calendar's project and home sections still appear only once they find bookings
  (ADR 0117). A deployment that doesn't want them doesn't need a setting to hide them.
- Equipment is now 0.4.0.

## Alternatives considered

- **Optional plugin dependencies.** A plugin could name a dependency it uses only if it's
  installed, and its places on that plugin's types would apply only then. That's a
  dependency resolver, lock and build change for one placement. It would still put the
  evidence on QC deliverables only.
- **A tab on every record, always drawn.** Every record's page would have Instrument
  evidence and Calibration impact tabs, nearly always empty.
- **The record's Lineage only.** Lineage lists the uses and impacts that name a result,
  among everything else related. An open impact case on a result that QA is about to
  release needs a place of its own.

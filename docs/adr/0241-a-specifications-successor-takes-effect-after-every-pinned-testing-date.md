# 0241. A specification's successor takes effect after every pinned testing date

- Status: Accepted
- Date: 2026-11-26
- Refines: [0192](0192-lot-release-testing-pins-a-specification-and-issues-a-signed-certificate.md)

## Context

A release case pins the specification version in force on its testing date. Publication
appends approved versions to the material's schedule in effective-date order. To keep a
case's pin true, a successor had to start on a future date (UTC). Two common procedures
didn't fit:

- A version effective on approval starts the day QA signs it.
- A laboratory adopting Iki imports the approved versions it already had, with their past
  effective dates.

What a successor must never do is change which version a case's testing date fell under.
Before a pinned date, publication is history the case didn't use, and it is safe. A
future-only rule kept the pins safe only because every case's testing date is today or
earlier.

## Decision

**The invariant is the pinned testing dates, not tomorrow.** Opening a case records the
latest testing date pinned from the schedule, `tested_through`, in the same update that
takes the schedule's lock. A successor starts after its predecessor and after
`tested_through`, under any policy. Concurrent publication and case opening still meet at the
schedule's revision, as before.

**The deployment chooses how soon a successor may start** (`successor_effective_dates`):

- `future`, the default and the procedure until now: after today.
- `same_day`: today, if no case has already tested under its predecessor today.
- `backdated`: an earlier date. The publication run must state why, in
  `backdated_because`. The run keeps the reason with its inputs and requester. A reason is
  refused when nothing is backdated, so it always means something.

A schedule's first version may take any date, as before. QA's signed approval of each
version, and publication's append-only list, are unchanged.

## Consequences

A laboratory can import its approved specification history before any case is opened, and
can make a version effective on the day it is approved. A case's certificate always names
the version its testing date fell under, whatever is published afterwards. Opening the first
case on a later testing date updates the schedule's revision. A run of the same schedule
launched before that commits is refused as changed and is run again.

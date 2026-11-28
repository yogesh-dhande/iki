# 0194. Equipment use pins calibration, and failures open impact cases

- Status: Accepted
- Date: 2026-11-23
- Relates to [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md),
  [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0179](0179-a-corrected-result-goes-back-for-review.md)
- Amended 2026-11-27: a use measured after a failed calibration isn't covered
- Refined by:
  [0239](0239-qa-opens-an-impact-case-for-a-result-no-use-was-recorded-for.md),
  where Equipment QA opens a case for a result no use was recorded for, and a result has
  one case for each failure

[ADR 0222](0222-facilities-choose-automatic-calibration-tracing.md) moves the
automatic trigger described below to a facility-owned example. Equipment keeps
the service history, measurement evidence and explicit impact trace.

## Context

An engineer needs to know when an instrument is due for calibration and
maintenance. An analyst needs the calibration evidence that covered any
measurement, and QA needs to trace results to a failed check. The written
calibration program includes a schedule, acceptance limits and remedial
actions. The local procedure determines the instrument-specific limits and
interval. [21 CFR 211.160(b)(4)][cfr], [NIST calibration interval guidance][nist]

## Decision

`iki/equipment` owns Instrument and Booking alongside the booking calendar. An
EquipmentService records a scheduled date, service kind and local procedure
revision. An assigned engineer in the instrument's project records the
as-found outcome, performed time, report and next due time for successful
work. Only that person may perform the engineer's signature step. The
instrument page shows the newest evidence, its review state and validity
alongside the existing service states. Each appointment pins its setup's
Research or Regulated QC review flow. Research needs one signature. Regulated
QC requires signed submission, independent review and independent release.
Signed final evidence is locked.

InstrumentUse links a measured record to the instrument and to its signed,
passing calibration. The action reads both record revisions and writes
their IDs, the calibration and result revisions, and the measurement time.
A calibration covers the interval from its performed time inclusive to its
next due time exclusive. A separate record lets the service connect any
measured record, including an already signed result or an instrument file
import. It also lets readers recover the specific revision when a result
has changed since the measurement.

An out-of-tolerance calibration records the earliest possible start of the
error and the rationale. The engineer can stop use immediately with the
existing Instrument service state. On a failed Calibration state the
optional project trigger walks reference edges from that instrument. The
trace also runs manually. For each matching use it creates a case referencing
the signed failure revision and the pinned use. The impact window starts at
the documented suspect time, includes discovery and remains open until the
next passing check with signed evidence. A failure and use pair identifies
one case. If re-tracing, include the existing impacts so already signed
dispositions are preserved. A new case is pending until signed off or
released. QA records the disposition and supporting reason using the
failure's review procedure.

Amended 2026-11-27: a use measured after a failure isn't covered. The action
reads the calibration in force at the measurement, and the instrument's own
Calibration state, which its run already includes; it searches no service
history. Each use records whether a calibration covered it, and if not, why.
It is never refused for this, since its result still needs a disposition:

- A signed passing calibration whose validity includes the measurement covers
  it, unless the instrument is marked Failed.
- With a signed failed calibration selected, a use measured at or after its
  suspected onset is recorded as not covered ("Plate reader 2 failed
  calibration on 2026-03-01."), and the same run opens the result's impact
  case for that failure, with this use as its evidence. A result has one case
  for each failure (ADR 0239), keyed by the two, whoever opens it: a trace, an
  instrument use or Equipment QA. The first stands (`on_match: keep`, ADR
  0082), so a second use of the result, or a later trace, finds it.
- With the instrument marked Failed and the passing calibration before the
  failure selected, the use is recorded as not covered ("Plate reader 2's
  calibration is marked Failed."). The run can't see the failure, so the
  failure's trace, once it is signed, opens the case, as for any use.

A use measured while the instrument is marked Failed is treated as after the
failure even when the record is late and the measurement came before it. QA
dispositions such a result as unaffected; a use is never counted as covered
when the instrument says it has failed.

## Consequences

The original result and any delivery receipt keep their revisions and
signatures. Any subsequent QA disposition is independently signed and linked
to the result, with both visible to the instrument project. The team must
reconcile uses missing from Iki, run another trace for late records and
communicate corrections to recipients of previously released results. The
trace reports its scope and fails visibly if the configured trigger exceeds
its project input limits.

A lab configures the review choice and roles according to its instruments
and qualifications. An equipment starter can help when a site has a standard
instrument catalog and service procedure library.

[cfr]: https://www.govinfo.gov/content/pkg/CFR-2024-title21-vol4/xhtml/CFR-2024-title21-vol4-sec211-160.html
[nist]: https://www.nist.gov/calibrations/recommended-calibration-interval

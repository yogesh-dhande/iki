# 0194. Equipment use pins calibration, and failures open impact cases

- Status: Accepted
- Date: 2026-11-23
- Relates to [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md),
  [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0179](0179-a-corrected-result-goes-back-for-review.md)

## Context

An engineer needs to know when an instrument is due for calibration and
maintenance. An analyst needs the calibration evidence that covered any
measurement, and QA needs to trace results to a failed check. The written
calibration program includes a schedule, acceptance limits and remedial
actions. The local procedure determines the instrument-specific limits and
interval. [21 CFR 211.160(b)(4)][cfr], [NIST calibration interval guidance][nist]

## Decision

`iki/equipment` uses the existing LIMS Instrument and Booking. An
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

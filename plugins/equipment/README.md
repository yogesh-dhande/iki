# Equipment service

Instrument and Booking share a calendar with the instrument's service and calibration history.
The calendar shows bookings by instrument, project or person and can narrow its search to a month.
Project members can open a booking from the calendar. Instrument maintenance and use are recorded alongside it.

Set the deployment's `review_flow` to `research` or `regulated_qc` during
setup. The scheduling action copies that choice onto each new appointment.
Changing the setting applies to future appointments. The Research engineer
signs the recorded work. In Regulated QC, the engineer signs submission, an
independent person with `iki/equipment.reviewer` signs review, and a third
person with `iki/equipment.qa` signs release. A correction before QA release
returns the record to Scheduled and starts a new review cycle.

## Service and use

1. On the appointment, record the time, performer, outcome and original
   report. For successful work, enter when the next check is due. Choose
   calibration intervals based on the written program, the instrument and
   the measurement requirements. [NIST calibration interval guidance][nist-interval]
2. Use the instrument's Service schedule tab to see upcoming and overdue work.
   Its status uses the latest observed outcome, signed evidence, expiration
   time and the Instrument's own service and calibration states.
3. When a measurement is recorded, run **Record instrument use** from the
   instrument. Select the measured record, the signed passing calibration
   and the measurement timestamp with a time zone. Iki checks the match and
   pins the result and calibration revisions. The Instrument evidence tab
   shows that evidence on the instrument and on the QC deliverable.
   For an identity-bearing measured record, the immutable record UUID still
   identifies the exact project record the run read.

Each instrument's service history is specific to its project. The same lab
roles govern both the existing Instrument states and the signed appointment's
performance requirements. The booking service checks the Instrument's
mechanical service state, so the engineer sets it out of service at discovery
if there is a known calibration failure.

## Investigate a failed calibration

1. At discovery, flag the Instrument out of service. Preserve the as-found
   observation as an out-of-tolerance service record. In **Review measurements
   from**, enter the earliest plausible onset, using the most recent
   documented known-good check when the onset is uncertain. Explain the window
   and the immediate response, then finish the required signatures.
2. If the project administrator enabled **Trace uses when an instrument
   fails calibration**, setting its Calibration state to Failed after the
   signed record is available traces the project’s linked history. For
   a flag set before sign-off, or when evidence arrives later, run **Trace
   failed calibrations**. Include the signed calibration history, uses and
   prior impacts for the selected instrument. The action opens one impact
   case for each use at or after the suspected onset and before the next
   signed passing calibration. Until a recovery is available, the window
   remains open. Narrow large manual traces by selecting use records in
   batches while keeping the complete calibration history for each batch.
3. Open the **Calibration impact** tab to review the results that need a
   disposition. A new case is pending review. Give each case a reasoned
   disposition of Unaffected, Repeat required or Invalidated. Research needs
   a QA sign-off. Regulated QC uses engineer submission, independent review
   and independent QA release. The case keeps the original result, its
   signed revision and the failing report available for verification.
4. If a result had already been delivered, QA communicates the disposition
   to the recipient through its quality process. The scope of a trace is
   the linked uses that were visible to its launcher in the instrument's
   project. Reconcile untracked measurements and any other projects that
   used the equipment before closing the investigation. If the automatic
   trace exceeds its input limit or cannot find signed evidence, the trigger
   records the failure and the worklist needs a manual trace.

The lab controls its return-to-service decision. Record and sign the passing
check, then update the instrument's service and calibration states. The
signed service reports and impact cases remain independently accessible.
[21 CFR 211.160(b)(4)][cfr]

## Example evidence

The archived certificate for **NIST SRM 930e**, Glass Filters for
Spectrophotometry, shows a public optical reference with wavelength and
maximum spectral bandpass pairs. Table 1a includes 440.0 nm with a 2.2 nm
maximum bandpass and 546.1 nm with a 6.5 nm maximum bandpass. The certificate's
expanded uncertainties describe the reference material. Use the local
procedure for the instrument's acceptance limits and its next due time.
The guide and tests use synthetic instrument-specific schedules, records and
outcomes. [NIST SRM 930e archival certificate, Table 1a][srm]

A starter would need the lab's instruments, written procedures, service dates
and authorized engineers. Site administrators set those up using the existing
instrument catalog, the review setting and the role grants.

[cfr]: https://www.govinfo.gov/content/pkg/CFR-2024-title21-vol4/xhtml/CFR-2024-title21-vol4-sec211-160.html
[nist-interval]: https://www.nist.gov/calibrations/recommended-calibration-interval
[srm]: https://tsapps.nist.gov/srmext/certificates/archives/930e.pdf

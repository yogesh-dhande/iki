# 0238. A facility's own program decides what service is due

- Status: Accepted
- Date: 2026-11-26
- Refines: [0194](0194-equipment-use-pins-calibration-and-failures-open-impact-cases.md),
  [0222](0222-facilities-choose-automatic-calibration-tracing.md)

## Context

`iki/equipment` assumed every instrument is in a calibration program, and every
maintenance procedure has an interval. A new instrument started Due for calibration and
stayed so. Completed maintenance had to give a next due time. A lab that books a centrifuge
or a balance that's checked before each use saw both flagged as due, forever. Only a PDF,
text or CSV file could be the evidence for a service. A service engineer's workbook, or a
photo of a signed paper record, couldn't.

A failed calibration's trace also didn't say what it had reviewed. It checked the uses the
investigator selected against each failure's window. Its log gave only totals. The
investigator couldn't tell from it which interval each failure covered, or that no
recovery had been selected, which leaves the window open.

The audit (slice 11) found the review procedure was already a facility's choice: the
`review_flow` setting selects Research (the engineer's signature, then QA's sign-off of an
impact) or Regulated QC (submission, independent review and QA release). Each record keeps
the procedure it was scheduled under. That stays.

## Decision

- **An Engineer or QA can say an instrument needs no calibration**, with a reason
  (`calibration: not_required`). A new instrument is still Due until someone says so,
  so a regulated lab's default is unchanged. The service schedule says no calibration is
  due for it. Booking never depended on calibration, and still doesn't.
- **A passing calibration must give a next due time. Completed maintenance may.** If it
  gives one, it's after the work. A failed check or a repair still confers no validity.
  The schedule calls signed maintenance without an interval completed, not due.
- **A service's evidence can be a workbook or an image** (XLSX, PNG, JPEG, TIFF) as well as
  a PDF, text or CSV file.
- **A trace says what it reviewed.** For each failure, its log gives the interval it
  checked: from the suspected start to the next selected passing calibration, or open if
  there was none. It also gives how many of the selected uses fell in that interval, and
  says that only the selected uses were checked.

## Consequences

- A lab with instruments outside its calibration program can keep them in Iki without
  false Due flags. Their reasons stay in the instrument's history.
- Calibration evidence and its validity checks are as strict as before.
- An investigator can tell from the run which part of the history a trace covered, and
  when to trace again.

## Not decided here

- The service kinds (calibration, maintenance), their outcomes and an impact's
  dispositions are fixed lists. A lab-defined vocabulary would need a general primitive
  for a deployment's own enum values, for every plugin, not one for equipment.
- An impact case needed an instrument use. Equipment QA now opens one for a result no use
  was recorded for ([ADR 0239](0239-qa-opens-an-impact-case-for-a-result-no-use-was-recorded-for.md)).
- `record_instrument_use` checks the one calibration selected. Telling the person about a
  later failure whose window covers the measurement would need the action to read the
  instrument's later service records.

## Amendment. A balance a result relies on stays in the calibration program

The Context above gave a balance that's checked before each use as an instrument
outside the calibration program, and the instrument type's Not required state said the
same. That's wrong for a balance any result relies on. 21 CFR 211.160(b)(4) calibrates
instruments at suitable intervals under a written program, and USP <1251> keeps periodic
calibration as well as the checks before use. Not required is for an instrument no
method relies on the measurements of, such as a vortex mixer, or a centrifuge whose
speed and temperature no method sets. The type's state now says so.

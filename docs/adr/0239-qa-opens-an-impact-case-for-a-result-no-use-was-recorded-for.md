# 0239. QA opens an impact case for a result no use was recorded for

- Status: Accepted
- Date: 2026-11-27
- Refines: [0194](0194-equipment-use-pins-calibration-and-failures-open-impact-cases.md),
  [0238](0238-a-facilitys-own-program-decides-what-service-is-due.md)

## Context

A failed calibration's trace opens an impact case for each instrument use in the failure's
window. It finds only the uses someone recorded. A plate read on the reader whose use was
never recorded in Iki can still have been affected. The analyst's bench logbook, the
instrument's own audit trail or a run sheet may show it was. Until now an investigator had
no way to give that result a case. They had to record a use after the fact, against a
calibration that had in fact failed, or decide the result's fate outside Iki.

A trace also opened one case for each use. A result read twice in the window got two cases
for the same failure, each needing its own disposition and signatures, though the
investigation is of the result.

## Decision

- **Equipment QA opens a case for a result no use was recorded for** with the action Open
  an impact case. It names the failed calibration, the affected result, when the result was
  measured and why it was affected (`reason`, required). The failure must be a signed
  out-of-tolerance calibration of the instrument, and the measurement at or after its
  suspected onset, and not in the future.
- **Only Equipment QA can open one.** The case names its opener (`opened_by`), a field only
  people with the `iki/equipment.qa` role in the project can be named in. A run launched by
  anyone else writes nothing, and says why.
- **A case says how it was opened.** `opened` is `traced` or `manually`. A traced case names
  its instrument use. A manual case has none, and keeps who opened it and why. When it was
  opened is the record's creation, which the server records, not a time the action writes.
  The calibration impact tab marks a manual case, its opener and the reason.
- **It follows a traced case's procedure.** It pins the failure's revision and review flow,
  so it needs the same reasoned disposition and signatures: QA's sign-off in Research;
  engineer submission, independent review and QA release in Regulated QC. Its opener is its
  creator, so in Regulated QC they can neither review nor release it.
- **One case for each result a failed calibration affects.** No two cases have the same
  failure and result (`one_case_per_result`), whether traced or opened manually, and the
  database holds it. A trace opens a result's case from its earliest use in the window, and
  skips a result that already has one among the selected existing cases. A trace that wasn't
  given that case is refused, naming it, and writes nothing.

## Consequences

- An investigator can bring every affected result into the same signed disposition, whether
  or not its use was recorded. An auditor can tell a manual case from a traced one, and see
  the evidence its opener gave.
- A result read more than once in a failure's window has one case and one disposition,
  pinned to its first read. Its later reads stay on their own instrument-use records.
- A trace that would open a second case for a result now fails, until its existing case is
  selected.

## Not decided here

- Opening a case manually doesn't check that no instrument use exists for the result. A use
  recorded later is skipped by the trace, as the result already has its case.

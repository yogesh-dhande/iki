# 0261. A bank check follows the procedure its project grants

- Status: Accepted
- Superseded in part by [0302](0302-a-project-chooses-each-types-review-procedure-and-the-server-pins-it.md) and [0305](0305-a-type-is-a-kind-of-work-not-a-review-procedure.md): a check's procedure is pinned, its grants say only who acts
- Date: 2026-11-26
- Refines: [0186](0186-cell-banks-and-the-checks-that-applied-to-a-culture.md)

## Context

A cell bank check records one test of one bank: what the testing laboratory
found, by which method, on which date, with its report. Labs differ in what
they do with it next. A research lab keeps the report and moves on. Another
has one person sign the result off. A GMP lab qualifying a master bank under
ICH Q5D has an analyst submit it, a second person review it and QA release
it. Labs also test more than STR identity and mycoplasma: sterility, species
identity, viral safety and karyotype are routine for a bank, and not every
test has a repeat date.

ADR 0186 shipped the two signed procedures and let a culture cite only a
signed check. A lab without electronic signatures could record checks but
never cite one, and could record only two kinds of test.

## Decision

**The test and its evidence are the record.** A check has its bank, test,
date, method, result and report for any of seven tests: STR authentication,
species identity, mycoplasma, sterility, viral testing, karyotype, or another
the lab names. Each test has its own result vocabulary: match or mismatch for
identity, positive or negative for contamination, conforms or does not
conform for karyotype, and inconclusive for any. The repeat date is optional.

**Project grants choose the procedure, as before, now with three.**

| Procedure    | Grants                             | Final state    |
| ------------ | ---------------------------------- | -------------- |
| Record only  | Cell bank check recorder           | Accepted       |
| Research     | Cell bank research signatory       | Signed off     |
| Regulated QC | Cell bank analyst, reviewer and QA | Released by QA |

Accepting a check needs its report attached and no prior submission. It
locks the check without a signature. The Research and Regulated QC states,
signatures, independence guards and corrections are those of ADR 0186.
Each final state is final: a wrong result is followed by a new check.

**A culture cites a final check.** It cites an STR or mycoplasma check of its
own bank that is accepted, signed off or released by QA, tested on or before
the culture's date and, if it has a repeat date, due on or after it.

The cell line roles stay the plugin's own. Recording, signing and releasing
a bank's qualification is a cell culture responsibility that a lab often
gives to different people than its assay analysts and result QA.

## Consequences

A regulated project grants only the regulated roles, so its checks can't be
accepted or signed off alone, and its cultures cite released checks. A
record-only lab cites its accepted checks, each locked with its report. A
check's history shows which procedure finished it. A project that grants
roles from two procedures lets either finish a check, as ADR 0186 allowed for
two; its admin chooses the grants.

Citing a check without a repeat date relies on the lab's own procedure for
how long that result applies.

## Amendment of 2026-11-27 — the lab's own tests are a list

"Another the lab names" was a free-text name beside the Other test, which
ADR 0270 retires: "Endotoxin", "endotoxin" and "LAL" were three tests that a
search or a bank's qualification couldn't group, and an Other test could
record any result at all.

The six tests stay the plugin's, because rules read them: each has its result
vocabulary, and a culture cites an STR and a mycoplasma check by its test. An
Other check names a test from the lab's list of bank tests
(`iki/cell_lines.CellCheckTest`), which its curators keep. Each option says
which results it gives (identity, contamination or conformance, with the
vocabularies above), fixed once it's added because the checks recorded with
it rely on it; a wrong one is superseded. The rule that a result belongs to
its test holds for the lab's tests too. The list starts with endotoxin,
post-thaw viability and tumorigenicity, each judged against the lab's
criterion.

The lab's tests aren't made the only list, with the six among its options,
because a culture's rule would then read the test's facts through the check,
two references away, and rules read one.

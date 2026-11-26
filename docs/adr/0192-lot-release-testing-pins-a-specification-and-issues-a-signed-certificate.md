# 0192. Lot release testing pins a specification and issues a signed certificate

- Status: Accepted
- Date: 2026-11-23
- Refines: [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md),
  [0171](0171-a-types-records-export-as-files-iki-writes-from-its-fields.md) and
  [0179](0179-a-corrected-result-goes-back-for-review.md)
- Amended: 2026-11-26 — an investigation's own testing, an inconclusive conclusion and
  its investigator (see Amendment)

## Context

A lot's certificate must show which specification applied and which measurements supported
the decision. An out-of-specification observation can be attributable to an assignable
laboratory cause. A documented investigation and signed invalidation preserve that
observation along with the repeat used for disposition.
[FDA guidance on investigating OOS results](https://www.fda.gov/media/158416/download)

## Decision

The QC release plugin uses a project-owned specification schedule per material. A version
declares one dated set of procedures and acceptance criteria. QA signs it before publication
adds it to the schedule, and that signature locks the version. A new version takes effect
after earlier ones. A release case pins the schedule and the version in effect on its
testing date. The database enforces one schedule per material and one case per lot within
the project, including when two actions run concurrently.

The case title starts with the lot number printed on its physical label so QA can match
the container to the case in the list. The testing date follows the number. The same lot
number identifies the tested batch on the certificate.
[WHO model certificate of analysis](https://www.who.int/publications/m/item/trs1010-annex4)

The testing action records the measured value and whether it meets the selected criterion.
A result may come from a manually reported measurement or a reviewed plate-assay endpoint
on that physical lot. An OOS value opens an investigation in the same transaction. The
original observation is immutable. A repeat cites the prior attempt and its signed, evidenced
invalidation. A confirmed or unresolved exception blocks a passing certificate.

The research flow needs one sign-off. The regulated QC flow requires the analyst's signed
submission, an independent review and a separate QA release. The case fixes the flow at
opening. Corrections to a submitted or reviewed record return it to Recorded with a reason.
Terminal signatures lock an investigation decision or certificate. QA approval of a
specification is the authorization to put a controlled version into effect in either flow.

The certificate action seals the case and assembles the full attempt sequence, pinned
specification, observed values and signed investigation decisions. The record export reads
the signed revision's data and makes the existing electronic signatures available with it.
Lot QA uses the issued certificate as evidence when setting the lot's disposition. Supplier
certificates continue to be kept on the lot.

## Consequences

Explicit test and investigation selections make the scope of the certificate visible. The
stored attempt sequence checks that nothing was omitted. Approval and publication are
separate recorded steps. The teaching example covers numeric inclusive criteria and a
single release case per lot and project. Product-specific protocols for resampling,
rework, qualifying qualitative tests or repeated commercial release can extend this design.

## Amendment. An investigation tests under its plan, may be inconclusive, and has an investigator

FDA's OOS guidance (§IV.B) separates testing during an investigation from the decision to
replace the original result. Until now Iki had only the second: a repeat after a signed
invalidation. Tests run while the investigation was open had nowhere to be kept.

**An investigation records its testing plan.** The plan is written under the lab's
approved procedure, such as a retest of the same preparation and how many. Until it's
written, `record_test` refuses an investigational test. An **investigational** test names
the open investigation of the attribute's most recent reportable result, and that result
is out of specification. The test is kept in the case's attempt sequence and pins the
investigation's revision, and so the plan it was run under. Its outcome is compared with
the criterion. It opens no second investigation, and it replaces nothing. Once the
investigation is submitted (Regulated QC) or signed off (Research), it takes no more
tests. A **reportable** repeat still requires the signed, evidenced invalidation and still
cites the original. The certificate's completeness check requires every investigational
test, and the investigation each was run under. The certificate pins the plan and each
test's purpose.

**A conclusion may be inconclusive.** FDA's guidance (§V) treats an investigation that
finds no assignable cause as one that leaves the original result standing in the batch's
disposition. An inconclusive investigation is signed like any other. It doesn't permit a
reportable repeat, and it prevents a passing certificate, as a confirmed one does.

**An investigation has an investigator.** The person field assigns the record while it's
Recorded. In Regulated QC the investigator must hold the analyst role, and the independent
reviewer and QA must each be someone other than the investigator, as well as the
submitter, observer and editors.

A lab whose SOP needs more than this, such as a second analyst's retest under a
separately approved protocol, records that protocol as the plan. Each retest remains
an investigational test under it.

# 0192. Lot release testing pins a specification and issues a signed certificate

- Status: Accepted
- Date: 2026-11-23
- Refines: [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md),
  [0171](0171-a-types-records-export-as-files-iki-writes-from-its-fields.md) and
  [0179](0179-a-corrected-result-goes-back-for-review.md)
- Amended: 2026-11-26 — a certificate states its final results apart from its evidence (see Amendment)

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

## Amendment. A certificate states its final results apart from its evidence

The certificate's one report held everything: failed attempts, analysts and
investigations. Its signed export was what the lab sent, so a customer received the
lab's internal OOS record, and no issuer. A research sign-off read the same as a
quality unit's release.

**A certificate has two parts, both signed.** `report` is the internal evidence, as
before. `coa` is what the certificate states, following ICH Q7 §11.4:

- the issuer;
- the lot number with its made, retest and expiry dates;
- the specification version;
- the testing date;
- for each criterion, its method, limits or requirement, and final reportable result.

The signed export now writes `coa`. The signatures bind the revision, so they cover both
parts. The record and its certificate tab show the evidence beside it.

**Iki checks two things before preparing.** The issuer is the `certificate_issuer`
setting: the manufacturer's name, address and telephone (ICH Q7 §11.43). A regulated
certificate can't be prepared without it. A research certificate is titled "Research
certificate of analysis" and says it was issued on one reviewer's sign-off, not released
by a quality unit.

**The issued state is the one the procedure named.** Both parts record the state that
issues the certificate in its flow. The certificate view shows "Issued" only in that
state, rather than in either flow's final state.

Preparing still takes the case's tests and investigations as selected inputs. An action
can't gather a record's related records itself, and the completeness check refuses any
selection that leaves one out. Gathering them for the issuer would need actions to take
related records the way triggers can (ADR 0123). That is a core change.

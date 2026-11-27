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

A certificate of analysis is what QA releases to the lot's recipient. The evidence behind
it stays in the batch record (ICH Q7 §6.6 and §11.4; 21 CFR 211.188 and 211.194). The
customer file carries only the released results, and the recipient can verify that file
alone.

**Two records, each signed.** The **certificate record** (`ReleaseCertificate`) is
internal, and it holds two parts. `report` is the evidence: every attempt with its analyst,
the pinned criteria and each signed investigation. `coa` is what the certificate will
state, following ICH Q7 §11.4:

- the issuer;
- the lot number with its made, retest and expiry dates;
- the specification version;
- the testing date;
- for each criterion, its method, limits or requirement, and final reportable result.

The record is signed under the case's procedure, and it has no export.

`issue_certificate` creates the **certificate of analysis** (`CertificateOfAnalysis`)
from a finally signed record. The new record holds only `coa`, the SHA-256 of its
canonical JSON, and the procedure. It names none of the tests, investigations or people
behind it.

Its reviewer issues a research certificate, and Release Testing QA issues a regulated
one. Each signs it as a separate record. Its signed export, in HTML or JSON, therefore
carries that signature alone. The signed content holds what the certificate states, so a
recipient can check the signature against the file they hold. Any edit fails that check.

**Linked inside Iki only.** The certificate record holds the same digest, `coa_digest`.
The certificate of analysis is derived from that record, so its lineage leads back to the
evidence for an audit. Neither link is in the shared file.

`issue_certificate` refuses a record that isn't finally signed, or whose `coa` doesn't
match its digest. The digest is unique, so a certificate is issued once. A second copy is
another export of the same record.

**Iki checks two things before preparing.** The issuer is the `certificate_issuer`
setting: the manufacturer's name, address and telephone (ICH Q7 §11.43). A regulated
certificate can't be prepared without it. A research certificate is titled "Research
certificate of analysis" and says it was issued on one reviewer's sign-off, not released
by a quality unit.

**The final state is the one the procedure named.** The evidence records the certificate
record's final state in its flow, and the certificate its issuing state. The certificate
view shows each as final only in that state.

Preparing still takes the case's tests and investigations as selected inputs. An action
can't gather a record's related records itself, and the completeness check refuses any
selection that leaves one out. Gathering them for the issuer would need actions to take
related records the way triggers can (ADR 0123). That is a core change.

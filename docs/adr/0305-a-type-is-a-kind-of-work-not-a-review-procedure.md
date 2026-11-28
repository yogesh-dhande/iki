# 0305. A type is a kind of work, not a review procedure

- Status: Accepted
- Date: 2026-11-27
- Reverses in part: [0195](0195-core-facility-requests-return-signed-deliverables.md)
  and [0235](0235-a-services-own-request-types-carry-its-fields.md) (a request and
  deliverable type per flow), [0198](0198-a-project-applies-a-versioned-assay-starter.md)
  and [0223](0223-applying-an-assay-starter-configures-the-project.md) (a result type per
  flow), [0236](0236-a-lab-chooses-each-samples-testing-procedure.md) (a sample type per
  procedure), [0240](0240-a-project-chooses-its-release-stability-and-verification-review.md)
  (a certificate type per flow)
- Refines: [0211](0211-lots-inventory-samples-and-requests.md),
  [0261](0261-a-bank-check-follows-the-procedure-its-project-grants.md)

## Context

Several plugins made a type for each review procedure: Plain, Research and QC requests
and deliverables; research and QC assay results, each with a requested variant;
research and QC certificates and OOS investigations; plain and reviewed samples. The
procedure was then chosen by whoever picked the type. A core facility that served both
discovery and QC customers needed two request types and two deliverable types per
service, and an example service could only be one or the other. With procedures
declared once and pinned by the server
([ADRs 0301](0301-a-review-procedure-is-declared-once-and-a-type-says-what-it-needs.md)
and [0302](0302-a-project-chooses-each-types-review-procedure-and-the-server-pins-it.md)),
a type per procedure only repeats what the pin says, and lets the wrong one be chosen.

## Decision

**A subtype is for content: what the work asks for, records or produces. A procedure
is never a reason for one.** The flow types are replaced by one type each, reviewed
under the procedure pinned on it:

| Was                                                                 | Is                                                        |
| ------------------------------------------------------------------- | --------------------------------------------------------- |
| `PlainRequest`, `ResearchRequest`, `QCRequest`                      | `Request`, which opens the work for its deliverables      |
| `PlainDeliverable`, `ResearchDeliverable`, `QCDeliverable`          | `Deliverable`, pinned by its request                      |
| `ResearchAssayResult`, `QCAssayResult` and their requested variants | `AssayResult`, and `RequestedAssayResult` for a request's |
| `ResearchCertificate`, `QCCertificate`                              | `ReleaseCertificate`, pinned by its case                  |
| `ResearchOOSInvestigation`, `QCOOSInvestigation`                    | `OOSInvestigation`, pinned by its case                    |
| `PlainSample`, `ReviewedSample`                                     | `Sample`                                                  |

A request is pinned when it's sent to its service, from the service project's choice
for its type, since the service owns how its work is reviewed (ADR 0132). Its
deliverables follow it. A service's own request and deliverable types (ADR 0235) stay:
they carry what the service asks and returns, and either procedure can review them.
A service that hasn't chosen can't receive a request: sending it is refused, 409
`procedure_not_chosen`. A requested assay result is the service's own work, so it's
pinned at create by the service project's choice for assay results, as its other fits
are, not by the request.

A type merged into one keeps its own words for what is signed: `review.statements`
gives a signed state's attestation, such as a deliverable's release "I release the
independently reviewed deliverable to the requesting project." Others are the
procedure's.

A sample keeps its custody and testing in `status` and its review in `review`, each its
own field (ADR 0125). The unsigned completion of a plain sample, a plain deliverable and
a record-only bank check is one flow of core's result review, **Accept without
sign-off**: an unsigned state, `accepted`, that locks.

A bank check is pinned like any record. Its project's grants say who may act, not which
procedure the check follows. Its bank roles stay its own (ADR 0304).

## Consequences

- One request type serves a core facility's discovery and QC customers. Its service
  project chooses the procedure, and the deliverable can't be signed under another.
- An analyst can't create a plain sample for a QC request: there is one Sample, pinned
  to the procedure its project chose.
- HRID prefixes of the removed types are freed; the remaining types keep theirs.
  Before M1 the affected plugins' migrations are regenerated.
- Whether a result answers a request is still a subtype here. Making it a capability is
  separate work.

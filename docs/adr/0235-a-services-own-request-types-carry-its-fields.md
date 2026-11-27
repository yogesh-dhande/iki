# 0235. A service's own request types carry its fields

- Status: Accepted
- Amended by [0283](0283-a-subtype-narrows-what-it-inherits.md): `refines` narrows any inherited part, of which these are two
- Date: 2026-11-26
- Refines: [0164](0164-a-subtype-says-which-roles-its-rows-take.md),
  [0195](0195-core-facility-requests-return-signed-deliverables.md),
  [0211](0211-lots-inventory-samples-and-requests.md),
  [0232](0232-a-service-orders-its-own-work-from-primitives.md)

## Context

Every request in `iki/requests` carried one protein production core's fields, one
sequencing core's and one analytical panel's: a choice of service, a requested mass, a
sequencing method and four analyses. Every deliverable carried a protein, a lot number, a
delivered mass, read files, a run accession, a purity, an intact mass, a peptide coverage
and an endotoxin result. Their rules and guards were written into each signature class.
A lab whose core does other work saw these fields on every form. A core whose panel
differs had no way to say so without editing Iki's plugin.

iLab and Benchling give each service its own request form and its own result fields.
ADR 0232 already lets a lab's request type add the service's own fields. What it couldn't
do was check a deliverable against them. A deliverable subtype inherits its parent's
`request` reference, so its rules read the parent request type's fields only. Nor could a
lab stop its core recording the parent's deliverable, without the service's checks,
against its request.

## Decision

**The request family keeps the contract every service shares.** A request keeps its
title, what it sends, the date it's needed by, its instructions, the requester's standing
and the service's status, priority, schedule, analyst and notes. A deliverable keeps its
outcome, its report and the material it covers. Plain, Research and QC keep their
release classes and signatures. Every signature now checks that a deliverable's material
is still on the request, or an aliquot or sample of it (`still_requested`, reading
`covered_subjects`), before the signer signs in again. Iki checks the same as the result
is released.

**A service's fields are its own request and result types.** Three examples ship them,
each a starting point a lab copies:

| Example                                                                 | Request                                            | Result                                                                      |
| ----------------------------------------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| [`acme/protein_production`](../../examples/plugins/protein_production/) | Production request (Research): one protein, a mass | Production deliverable: the protein, the lot and its mass                   |
| [`acme/sequencing_service`](../../examples/plugins/sequencing_service/) | Sequencing request (QC): its tubes and method      | Sequencing deliverable: read files or a public run accession                |
| [`acme/analytical_testing`](../../examples/plugins/analytical_testing/) | Analytical request (QC): the analyses to run       | Analytical result: one requested analysis, its measurement and pass or fail |

Their checks are rules. A rule that must hold when a person signs says
`recheck_before_signing: true`, so a request changed since it was recorded can't be
signed with an old answer.

**A subtype narrows an inherited reference.** `refines` with `to` gives the types an
inherited reference, or list of references, refers to in the subtype's records. Each is
one of the types its parent's refers to, or a subtype of one. A production request
refines `records` to the protein to make. A production deliverable refines `request` to
production requests. Saving checks references by the record's own type, forms offer only
those types, and rules and guards read the narrower type's fields:
`record.request.analyses`. The column stays its declaring type's, as a refined list of
rows does (ADR 0164).

**A request is answered by the result types made for it.** Of the result types whose
request reference can refer to a request, those referring to the type nearest the
request's own answer it. A result of another type is refused as it's recorded. A result
type refined to a service's request type is therefore the only one for its requests. Its
parent's type answers every other request it did before. A request type that no result
type is refined to keeps its parent's. ADR 0232's ranked Sequencing request is still
answered by Plain deliverables.

## Consequences

- A core that asks nothing service-specific uses Plain, Research or QC requests as they
  are. The dev cluster's worked core facility does so, with the lot, mass and run
  accession in each delivery report.
- A QC request can't be answered by a plain or research deliverable, and a refined
  request type can't be answered by its parent's deliverable. Neither can skip the
  service's own checks.
- A lab's evidence checks are rules, so they are checked as a result is saved, not only
  as it's signed. An analyst records a result once its measurement exists.
- Moving a lab's core onto its own types is a migration of its records to the new types.
  Before M1 there are none.
- The examples' fields, rules and README replace the paragraphs of ADR 0195 that
  described Iki's own production and sequencing fields.

## Alternatives considered

- **Extensions of QCRequest and QCDeliverable.** An extension adds fields to every
  record of a type, in every project. A deployment whose core measures a panel would put
  it on every QC request, including plate assays'.
- **Guards added by a subtype to its parent's states.** It would check only at
  signatures, and it adds a second way to declare a state's guards. Rules with
  `recheck_before_signing` already check at saves and signatures.
- **A request type naming its result types.** It says the same as the refinement, from
  the other side, and a result type would still read its parent request type's fields.

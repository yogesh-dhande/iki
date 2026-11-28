# Requests

`iki/requests` is for one project asking another team to do work. The request and deliverable families are specified in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

A request type declares the record fields it sends with `sends`. For example, the asking project can send two expected tubes. The receiving project can reconcile the shipment, assign the work and keep scheduling details in a private WorkItem. A work item can be scheduled and started, or finished straight from the queue, as a desk-based task is. The asking project sees the deliverable when the service releases it. [Ownership and intake](../../docs/adr/0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)

A service's requests are Requests, or a type of its own that extends Request, and what it returns for one is a Deliverable, or a type of its own that extends Deliverable. A report for commissioned material identifies one subject in its immutable `material` field. The service can complete the request once it has returned every recorded deliverable and covered each commissioned subject. A report on an aliquot covers its commissioned source tube, including after further splits. Several reports on the same tube cover that tube once.

## How deliverables are reviewed

A service chooses how what it delivers is reviewed, under core's result review (`iki/core.result_review@1`), by applying the **Service deliverables** starter (`iki/requests.delivery_review`) in Project settings → Starters, or an Org Admin sets the deployment's default in Admin → Review procedures. A service that hasn't chosen can't receive requests: sending one is refused, 409 `procedure_not_chosen`.

A request is pinned to the service's choice as it's sent, and every deliverable for it follows that flow, even if the service changes its choice later. [ADR 0305](../../docs/adr/0305-a-type-is-a-kind-of-work-not-a-review-procedure.md)

| Flow                    | Return to the asking project                                                                                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Accept without sign-off | Someone at the service accepts the recorded deliverable, which locks it. No one signs.                                                                       |
| Research (suggested)    | A reviewer who didn't create or edit the deliverable signs it off.                                                                                           |
| Regulated QC            | Whoever made it signs its submission, an independent reviewer signs its review, and someone in QA who did neither signs its release: three different people. |

The steps are played by the shared lab roles (Analyst, Reviewer, Sample and result QA). Each signature, and the final step, checks that the request is still open and in progress at the service, and that the deliverable's material is still on the request, or is an aliquot or sample of it.

## A service's own fields

These types hold what every service shares. A core that records more about its work, such as the protein and mass to make, the sequencing method or the measurements of an analytical panel, declares its own request and deliverable types. Its request type extends Request with the fields the asking project gives. Its deliverable type extends Deliverable with the service's evidence, and refines `request` to the service's request type. Its rules read that type's fields, and only its deliverables answer its requests. [ADR 0235](../../docs/adr/0235-a-services-own-request-types-carry-its-fields.md)

Start from one of these examples:

- [Protein production](../../examples/plugins/protein_production/README.md): one protein, a requested mass, and the lot the core returns.
- [Sequencing service](../../examples/plugins/sequencing_service/README.md): tubes, a method, and each tube's reads or public run accession.
- [Analytical testing](../../examples/plugins/analytical_testing/README.md): the analyses to run, and each signed measurement.

## Try a facility request

The [core facility examples](../../examples/core-facility-requests/README.md) include a protein request and a sequencing request, with their input records, delivery reports and sources.

# Requests

`iki/requests` is for one project asking another team to do work. The request and deliverable families are specified in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

A request type declares the record fields it sends with `sends`. For example, the asking project can send two expected tubes. The receiving project can reconcile the shipment, assign the work and keep scheduling details in a private WorkItem. With `iki/equipment`, the instrument
a work item runs on is a Booking for it, which shows on the work item's lineage. A
request and its work items share one priority scale: routine, high and urgent. A work item can be scheduled and started, or finished straight from the queue, as a desk-based task is. The asking project sees the deliverable when the service releases it. [Ownership and intake](../../docs/adr/0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)

Request and Deliverable are abstract parents. The concrete request determines which deliverable can answer it. A report for commissioned material identifies one subject in its immutable `material` field. The service can complete the request once it has returned every recorded deliverable and covered each commissioned subject. A report on an aliquot covers its commissioned source tube, including after further splits. Several reports on the same tube cover that tube once.

| Request         | Deliverable         | Return to the asking project                                                                              |
| --------------- | ------------------- | --------------------------------------------------------------------------------------------------------- |
| PlainRequest    | PlainDeliverable    | The service explicitly delivers the record.                                                               |
| ResearchRequest | ResearchDeliverable | A reviewer signs the result.                                                                              |
| QCRequest       | QCDeliverable       | An analyst signs submission, an independent reviewer signs review and a third person in QA signs release. |

Each signature checks that a deliverable's material is still on the request, or is an aliquot or sample of it.

## A service's own fields

These types hold what every service shares. A core that records more about its work, such as the protein and mass to make, the sequencing method or the measurements of an analytical panel, declares its own request and deliverable types. Its request type extends Plain, Research or QC request with the fields the asking project gives. Its deliverable type extends the matching deliverable with the service's evidence, and refines `request` to the service's request type. Its rules read that type's fields, and only its deliverables answer its requests. [ADR 0235](../../docs/adr/0235-a-services-own-request-types-carry-its-fields.md)

Start from one of these examples:

- [Protein production](../../examples/plugins/protein_production/README.md): one protein, a requested mass, and the lot the core returns.
- [Sequencing service](../../examples/plugins/sequencing_service/README.md): tubes, a method, and each tube's reads or public run accession.
- [Analytical testing](../../examples/plugins/analytical_testing/README.md): the analyses to run, and each signed measurement.

## Try a facility request

The [core facility examples](../../examples/core-facility-requests/README.md) include a protein request and a sequencing request, with their input records, delivery reports and sources.

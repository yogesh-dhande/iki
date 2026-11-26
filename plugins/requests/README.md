# Requests

`iki/requests` is for one project asking another team to do work. The request and deliverable families are specified in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

A request type declares the record fields it sends with `sends`. For example, the asking project can send two expected tubes. The receiving project can reconcile the shipment, assign the work and keep scheduling details in a private WorkItem. A work item can be scheduled and started, or finished straight from the queue, as a desk-based task is. The asking project sees the deliverable when the service releases it. [Ownership and intake](../../docs/adr/0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)

Request and Deliverable are abstract parents. The concrete request determines which deliverable can answer it. A report for commissioned material identifies one subject in its immutable `material` field. The service can complete the request once it has returned every recorded deliverable and covered each commissioned subject. A report on an aliquot covers its commissioned source tube, including after further splits. Several reports on the same tube cover that tube once.

| Request         | Deliverable         | Return to the asking project                                                                              |
| --------------- | ------------------- | --------------------------------------------------------------------------------------------------------- |
| PlainRequest    | PlainDeliverable    | The service explicitly delivers the record.                                                               |
| ResearchRequest | ResearchDeliverable | A reviewer signs the result.                                                                              |
| QCRequest       | QCDeliverable       | An analyst signs submission, an independent reviewer signs review and a third person in QA signs release. |

## Try a facility request

The [core facility examples](../../examples/core-facility-requests/README.md) include a protein request and a sequencing request, with their input records, delivery reports and sources.

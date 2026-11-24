# Requests

`iki/requests` is for one project asking another team to do work. This manifest declares the dependencies for the request and deliverable types specified in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

A request type declares the record fields it sends with `sends`. For example, the asking project can send two expected tubes. The receiving project can reconcile the shipment, assign the work and keep scheduling details in a private WorkItem. The asking project sees the deliverable when the service releases it. [Ownership and intake](../../docs/adr/0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)

Request and Deliverable are abstract parents. The concrete request determines which deliverable can answer it.

| Request         | Deliverable         | Return to the asking project                                                                              |
| --------------- | ------------------- | --------------------------------------------------------------------------------------------------------- |
| PlainRequest    | PlainDeliverable    | The service explicitly delivers the record.                                                               |
| ResearchRequest | ResearchDeliverable | A reviewer signs the result.                                                                              |
| QCRequest       | QCDeliverable       | An analyst signs submission, an independent reviewer signs review and a third person in QA signs release. |

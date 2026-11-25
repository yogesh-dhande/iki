# Requests

`iki/requests` is for one project asking another team to do work. The request and deliverable families are specified in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

A request type declares the record fields it sends with `sends`. For example, the asking project can send two expected tubes. The receiving project can reconcile the shipment, assign the work and keep scheduling details in a private WorkItem. The asking project sees the deliverable when the service releases it. [Ownership and intake](../../docs/adr/0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)

Request and Deliverable are abstract parents. The concrete request determines which deliverable can answer it. A report for commissioned material identifies one subject in its immutable `material` field. The service can complete the request once it has returned every recorded deliverable and covered each commissioned subject. A report on an aliquot covers its commissioned source tube, including after further splits. Several reports on the same tube cover that tube once.

| Request         | Deliverable         | Return to the asking project                                                                              |
| --------------- | ------------------- | --------------------------------------------------------------------------------------------------------- |
| PlainRequest    | PlainDeliverable    | The service explicitly delivers the record.                                                               |
| ResearchRequest | ResearchDeliverable | A reviewer signs the result.                                                                              |
| QCRequest       | QCDeliverable       | An analyst signs submission, an independent reviewer signs review and a third person in QA signs release. |

## Try a facility request

For production, register the yeast alcohol dehydrogenase 1 sequence from
`examples/P00330.fasta`. Send a Research request for 1 mg and upload
`examples/P00330-production.txt` in the core as the delivery report for the
training lot and 0.8 mg delivery. The requested quantity, delivered quantity,
and lot are synthetic. The protein and sequence come from the UniProtKB
entry under its attribution license.
[UniProtKB P00330](https://www.uniprot.org/uniprotkb/P00330/entry),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

For sequencing, register a training tube named CEY652 and send it with a
Regulated QC request for long-read sequencing. In the core, receive it and
use `examples/ERR13376132-delivery.txt` for the tube's report with public
run accession `ERR13376132`. The accession and sample identifier refer to
a published yeast dataset. The request, custody, and signatures are training
events. [NCBI Sequence Read Archive](https://www.ncbi.nlm.nih.gov/sra/ERX12747051)

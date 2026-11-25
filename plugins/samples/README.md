# Samples

A sample is a portion actually taken from a tube or lot for a request, run or plate. The testing workflow is specified in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

Consider a lab drawing material for a plate. Its pending sample identifies the source and purpose. One ledger draw debits that source and records the quantity, unit, operator and time on the sample. A draw from a storage tube includes its scan. A direct draw from a lot uses the lot's own unit and permissions.

The receipt allows the sample to enter Received. During testing, an analyst records measurement rows and signs the submission. An independent reviewer checks and signs the same evidence. Results recorded closes the reviewed revision. A correction reopens testing with a reason and requires fresh signatures. Batch specifications and the decision to release material belong to the lot. [Lots](../lots/README.md)

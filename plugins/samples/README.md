# Samples

A sample is a portion actually taken from a tube or lot for a request, run or plate.
Who owns it, and how its draw is recorded, is specified in
[ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

Consider a lab drawing material for a plate. Its pending sample identifies the source and
purpose. One ledger draw debits that source and records the quantity, unit, operator and
time on the sample. A draw from a storage tube includes its scan. A direct draw from a lot
uses the lot's own unit and permissions. The receipt allows the sample to enter Received.

## Choose its testing procedure

Each sample follows the testing procedure of its type, chosen as it's created
([ADR 0236](../../docs/adr/0236-a-lab-chooses-each-samples-testing-procedure.md)):

- **Reviewed sample** (`SAM-`): an analyst records measurement rows in testing and signs
  the submission. An independent reviewer checks and signs the same evidence, completing
  the sample in Reviewed. The reviewer can't be the submitter, or anyone who edited the
  measurements in that testing cycle. A correction returns the sample to testing with a
  signed reason, and both sign again. Use it for a QC lab's samples, and wherever a second
  person must review the measurements.
- **Plain sample** (`SMP-`): the analyst completes the sample (Done) once it's received,
  with or without measurements, and nobody signs. Use it for an aliquot drawn as an
  assay's input, whose result is reviewed on its own record. The analyst reopens it with
  a reason to correct it.

Both lock once complete. The assigned analyst and reviewer each see the sample in their
work when it's theirs, and are told it's ready for them. Search **Sample** to see every
kind of sample together.

Batch specifications and the decision to release material belong to the lot.
[Lots](../lots/README.md)

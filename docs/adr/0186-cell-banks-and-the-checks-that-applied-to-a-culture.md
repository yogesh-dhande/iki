# 0186. Cell banks and the checks that applied to a culture

- Status: Accepted
- Date: 2026-11-22
- Builds on: [0115](0115-registries-keep-identities-projects-keep-records.md), [0123](0123-queries-walk-the-graph-of-what-depends-on-what.md), [0179](0179-a-corrected-result-goes-back-for-review.md), [0180](0180-where-material-is-and-how-its-received-moved-and-counted.md)

## Context

A catalogue accession identifies a cell line across labs. The results of an
authentication or mycoplasma test apply to the material tested. Searching
for the most recent result for the catalogue line can therefore attach a
different bank's check, or a later check, to a historical sample.

## Decision

Cell lines ship as a separate first-party plugin alongside microbial and
mouse strains. Each has its own identifiers and events. Cell banking uses
Iki's existing registry, inventory, signatures, files and graph.

The primary Cellosaurus accession is a parental line's registry identity.
Each supplier and catalogue ID pair is a second registry identity whose
fixed fact is the line it supplies. This retains multiple supplier references
for one line and prevents a pair from identifying two lines. A master bank
or a local derivative can cite the specific source of its parental material.
A local engineered line has its own record, parent and list of documented
changes. Independently isolated clones with the same reported change can
have different histories.

A master bank records the line, banking date and frozen passage number. A
working bank names its master bank of the same line. Each vial extends the
existing storable tube type. A thaw names the vial and keeps its bank's
passage number. Each subsequent passage names the previous culture, keeps
the same bank and advances the passage number by one. An analysis sample
links to the culture from which it was taken.

A check records the tested bank, method, result, test date and next due
date. Project role assignments select either the single-signature research
workflow or independent, signed submission, review and release. Each
culture can cite the specific finalized checks that cover its date and bank.
Citing a check preserves the reported outcome, including an adverse result.
Later tests add records and do not replace earlier evidence.

## Consequences

From a sample, the upstream graph reaches the culture's origin and the
checks its history cites. Following a vial opens its movement ledger.
Runs and attached files connect observations to the originating sample.
Projects can register historical cultures whose authentication or
mycoplasma records are missing by leaving those references empty.

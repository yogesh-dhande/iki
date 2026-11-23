# 0195. Core facility requests return signed deliverables

- Status: Accepted
- Date: 2026-11-23
- Refines: [0132](0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md),
  [0149](0149-a-result-leaves-its-service-only-by-release.md),
  [0180](0180-where-material-is-and-how-its-received-moved-and-counted.md)

## Context

A production facility needs a registered protein and the requested mass. A
sequencing facility needs the selected service and the tubes it will receive.
Both have to accept and assign the work, sign for the evidence, and return the
delivery to the requesting project.

## Decision

Example LIMS adds two concrete types that inherit `AnalysisRequest`. They
use the existing intake, queue, assignee, receipt, cross-project field
permissions, and delivery notifications. A service admin chooses Research or
Regulated QC in the service project's request intake by accepting the
corresponding request type. Intake for the parent `AnalysisRequest` also
accepts its subtypes under ADR 0132. The type chosen for each sent request
fixes its flow. Each deliverable type requires an immutable reference to the
matching concrete request type.

Protein production requests name a registered protein and a positive
requested mass. A successful deliverable records the same protein and the
produced lot number and mass as shown on the delivery report. Sequencing
requests name the sequencing method and the tubes the facility expects. A
successful sequencing deliverable identifies a requested tube and includes
its sequence files or its public run accession. Each deliverable includes a
report the service owns. An unsuccessful delivery retains its report and
outcome, letting the service explain the failure.

For Research, a reviewer signs off the recorded deliverable and sends it
back. In Regulated QC, an analyst signs the submission, an independent
reviewer signs the review, and a third person in QA signs release. A reasoned
correction before release returns the record to Recorded under ADR 0179.
An editor of that revision is ineligible to review or release it. Signing
requires an open request in progress. Each signature checks that the named
tube or protein is still on the request.

The result and its report stay in the service's project until the signature
that releases it. The requesting project then receives the locked signed
result and the release receipt under ADR 0149. The service marks the request
Delivered after the intended individual deliverables have come back. The
status describes the request as a whole and its assignment queue.

## Consequences

- A service can receive tubes and issue results for individual tubes using
  the existing request UI and storage custody records.
- The facility uploads its report and data files or supplies the accession
  of the archived reads with its report.
- A versioned starter would demonstrate a two-project handoff with role
  assignments and files already in place. Intake and ordinary record
  creation cover production and sequencing setup today.
- The example uses [UniProtKB P00330](https://www.uniprot.org/uniprotkb/P00330/entry)
  and [sequencing run ERR13376132](https://www.ncbi.nlm.nih.gov/sra/ERX12747051).
  Its lots, amounts and custody events are labeled as training data.

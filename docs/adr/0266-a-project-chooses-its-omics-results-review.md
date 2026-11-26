# 0266. A project chooses its omics results' review

- Status: Accepted
- Date: 2026-11-26
- Refines: [0240](0240-a-project-chooses-its-release-stability-and-verification-review.md)
  and [0258](0258-mass-spectrometry-results-name-their-report-format.md)

## Context

Proteomics search results and biologics mass spectrometry results are reviewed
under Research, Regulated QC or Record only
([ADR 0258](0258-mass-spectrometry-results-name-their-report-format.md)). Each
plugin's `review_flow` setting chose the procedure for the whole deployment, so
a discovery group and a QC lab sharing one deployment had to share a procedure.
[ADR 0240](0240-a-project-chooses-its-release-stability-and-verification-review.md)
gave release testing, stability and prep verification a per-project choice
through starters, with the flow pinned on the record the procedure starts with.

## Decision

Each plugin offers a starter: `proteomics-search-review` and
`biologics-ms-review`. A project's Admin chooses Research, Regulated QC or Record
only, and picks who holds each lab role the flow's signatures need, as
[ADR 0240](0240-a-project-chooses-its-release-stability-and-verification-review.md)
describes. A project that hasn't chosen uses the deployment's setting.

- The import pins the flow on the result it records, as the result did before.
  A result keeps the procedure it was recorded under after the project changes
  its choice.
- Research asks for a reviewer. Regulated QC asks for an analyst, a reviewer and
  QA, three different people.
- Record only asks for no one and grants no lab roles: its results stay
  Recorded, and no one signs them. It's a named choice the project's Admin
  makes and the audit log keeps, not a weaker default.
- The starter page draws each flow's own route through the result's single
  review field: Record only's is the Recorded state alone.

`Record only` is a flow label every starter can offer, beside `Research` and
`Regulated QC`.

## Consequences

A project of discovery proteomics can keep its search results as evidence
while a QC project in the same deployment releases its peptide maps under
Regulated QC. The guards, signatures and independence checks are unchanged.

A starter whose flow needs no roles shows that it grants none, in place of the
role choices.

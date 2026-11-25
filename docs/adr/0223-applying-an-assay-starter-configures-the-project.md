# 0223. Applying an assay starter configures the project

- Status: Proposed
- Date: 2026-11-25
- Refines: [0198](0198-a-project-applies-a-versioned-assay-starter.md)

## Context

An assay project can adopt a review procedure before it receives its first
sample. The original dose-response recipe also submitted a training request
from a second project, created inventory, drew material and uploaded a reader
file. A lab with its own data had to authorize and keep those records to
choose its review flow.

## Decision

The dose-response starter version `2.0.0` applies to one project. Its admin
previews a review flow and lab-role grants, then applies the reviewed plan.
The write records the recipe version, flow, grants and audit event together.
Research needs an analyst and reviewer. Regulated QC also needs QA and
independent signers. A fit keeps its selected flow and the evidence its
signers reviewed. A later choice governs new work. [Plan review](0207-commit-the-plan-that-was-reviewed.md)

The starter requires the installed assay types and roles. Application works
before a lab has enabled file storage or configured request intake. It
creates zero example records, files or material movements.

The Tox21 walkthrough owns its inputs and is loaded explicitly into teaching
projects. Its two-project handoff uses the same request, receipt, fitting and
signature services that labs use with their own material.

## Consequences

A project admin can configure internal assay work with access to that
project alone. A lab offering a service chooses its own request sources and
intake policy. The worked experiment remains reproducible from the checkout,
with the published reader data and the training inventory identified separately.

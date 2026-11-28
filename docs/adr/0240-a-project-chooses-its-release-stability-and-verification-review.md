# 0240. A project chooses its release, stability and verification review

- Status: Accepted
- Superseded in part by [0301](0301-a-review-procedure-is-declared-once-and-a-type-says-what-it-needs.md) (procedures declare their routes), [0302](0302-a-project-chooses-each-types-review-procedure-and-the-server-pins-it.md) (the server pins), [0304](0304-lab-roles-are-shared-and-a-procedures-slots-are-bound-at-setup.md) (shared QA) and [0305](0305-a-type-is-a-kind-of-work-not-a-review-procedure.md) (one certificate type)
- Date: 2026-11-26
- Refines: [0198](0198-a-project-applies-a-versioned-assay-starter.md),
  [0223](0223-applying-an-assay-starter-configures-the-project.md),
  [0192](0192-lot-release-testing-pins-a-specification-and-issues-a-signed-certificate.md),
  [0193](0193-a-stability-pull-keeps-its-protocol-revision-and-ledger-draw.md),
  [0184](0184-a-plasmid-prep-links-to-reviewed-verification-evidence.md)

## Context

Lot release testing, stability studies and plasmid prep verification each offer two review
procedures. Research has one reviewer's sign-off. Regulated QC has a signed submission, an
independent review and a separate QA release. Each record pins the procedure it was opened
under: a release case for its investigations and certificate, a scheduled study for its
measurements, a verification for itself.

Until now a deployment setting chose the procedure for every project. One deployment often
serves a discovery group and a QC laboratory. The discovery group would need a deployment
change, made by whoever runs the installation, to stop needing three signers. The QC
laboratory would need the same change to require them. Neither is the person who owns the
procedure.

Dose-response assays already let a project's Admin choose. A versioned starter previews the
flow and the lab-role grants it needs, and applies them under the plan that was reviewed.
The application is audited (ADRs 0198, 0207 and 0223). LabWare and Benchling configure review
procedures per laboratory or project, not per installation.

## Decision

**Each of the three procedures is a starter.** `lot-release-testing` (Lot release testing),
`stability-study` (Stability study) and `plasmid-prep-verification` (Plasmid prep
verification) are versioned recipes. Their plugins declare them. A project's Admin applies
one in Project settings → Starters, as for dose-response: choose Research or Regulated QC,
choose who holds each lab role, preview, then apply what was previewed. Applying records
the version, flow and grants together in one audit event.

**A starter asks only for the roles its flow uses.** Research release testing needs a QC
reviewer, and Release Testing QA to approve its specifications, as in either flow. Research
stability needs a coordinator and a reviewer. Research verification needs a verification
reviewer. Regulated QC adds the analyst and the QA who releases. The analyst, reviewer and
QA must be three different people, checked as for dose-response. Research stability and
verification need no QA grant, and no Research flow needs an analyst's submission. The
Research and Regulated QC routes remain exactly as before. Their guards, signatures,
independence checks and correction paths are unchanged.

**The choice is pinned where the procedure starts, not when a result is created.** The
project's choice is the run setting for that plugin's actions in that project, and the
deployment setting is the default for projects that haven't chosen. Opening a release case,
scheduling a study and recording a verification each pin it on that record. A later choice
governs records opened from then on. A case, study or verification already in progress
finishes under the procedure it began with. Its investigations, certificate and measurements
follow its pin, not the project's current choice. A dose-response fit is different: it is
its own record, so a new fit must be the chosen flow's result type (ADR 0223).

**The starter page draws each flow's own route.** A certificate has a type per flow. A
stability measurement and a plasmid verification have one state field with a route per
flow, chosen by their pinned flow. The starter names each flow's states. The page draws only
those states and determines signers from those states alone.

## Consequences

A QC laboratory and a discovery group can share a deployment. Each one's Admin chooses the
procedure for that project and can see who holds each role. A regulated certificate is only
issued in a project whose Admin chose Regulated QC, or in a deployment whose operator set
it as the default. Release Testing QA, Stability QA and Lot QA remain separate grants. A
person who approves a certificate doesn't thereby gain authority over the lot's
disposition.

Changing a project's choice doesn't rewrite any record or invalidate any signature. To bring
an open study under a new procedure, issue a new protocol revision as a new linked study,
as before.

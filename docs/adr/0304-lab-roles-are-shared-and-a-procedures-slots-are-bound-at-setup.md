# 0304. Lab roles are shared, and a procedure's slots are bound at setup

- Status: Accepted
- Date: 2026-11-27
- Refines: [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md),
  [0211](0211-lots-inventory-samples-and-requests.md),
  [0240](0240-a-project-chooses-its-release-stability-and-verification-review.md)
  (separate QA grants per plugin)

## Context

`iki/lab_roles` declares a coordinator, engineer, analyst, reviewer and QA, and ADR 0211
says plugins use them by qualified name, with only domain-specific qualifications kept
apart. The plugins didn't follow it. Eleven declared their own `qa`, ten their own
`reviewer` and eight their own `analyst`, and stability its own coordinator. Equipment
used the shared QA for an instrument and its own for a service. A site QA lead needed up
to eleven grants for one job, and a project's Admin had to know which of several QAs a
plugin meant.

ADR 0116's reason for declaring roles in plugins still holds: a role's meaning is
reviewed with the code that checks it. What it meant to make possible, sharing a role
across plugins, mostly didn't happen.

GitHub's custom repository roles build on a base role instead of redefining it.

## Decision

**A procedure names slots, not roles.** `analyst`, `reviewer`, `qa`, `engineer`,
`author`: a slot is the part someone plays in the procedure (ADR 0301). A starter binds
each slot to a lab role. By default a slot is bound to the `iki/lab_roles` role of its
name; a starter may bind it to another, and a project's Admin or the deployment's
default may choose another declared role when applying (ADR 0302). The binding is the
project's, for the type, and the server resolves `by` and each signature's role through
it when someone acts. A type's own person fields and guards can name a slot
(`slot: reviewer`), and then mean whoever the binding says.

**A plugin's own role with a generic name needs a reason.** A plugin that depends on
`iki/lab_roles` and declares a role named like one of its roles must say why it's
separate, with `distinct:`: "Qualifying a cell bank is given to different people than
assay review." The validator warns without one. Iki's plugins keep their own roles only
where that holds: Lot QA, which decides a lot's disposition (a certificate's approver
doesn't gain it), and the cell bank roles of ADR 0261. The other copies are removed, and
their types use the shared roles through slots.

**One name for the stamped flow.** Lots' `verification_flow` was the only setting under
another name. Its plugin setting is removed with the rest (ADR 0302), and its records'
flow is `review_flow`, as every type's is.

## Consequences

- A QA lead holds one QA role for the results, deliverables, certificates and stability
  measurements of a project, unless its Admin binds a slot to a narrower role.
- ADR 0240's separate Release Testing QA and Stability QA grants become a project's
  choice of binding, not a plugin's fixed split. Lot QA stays its own.
- A lab that needs a separate qualification declares a role in its plugin, with its
  reason, and binds the slot to it.

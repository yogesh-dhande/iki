# 0198. A project applies a versioned assay starter

- Status: Accepted
- Date: 2026-11-22
- Refines: [0188](0188-a-plate-read-keeps-the-wells-behind-a-fitted-endpoint.md)

## Context

A facility running its first dose-response plate needs a reader import, a fit,
people authorized to review it, and a requesting project to receive the signed
result. Two facilities using the same plugins can have different review
requirements. A person who has already submitted a result needs to know whose
signature comes next, even if an administrator changes the setup.

## Decision

A starter applies a versioned recipe to a **service project**. The deployment
installs the plugins and their schema migrations. A plugin advertises the
starter ID and version in its manifest, for example
`starters: {dose-response: "1.0.0"}`. Iki lists a recipe when its version is
supported by the server and its required plugins and types are active. Example
setup runs trusted server code. Scientific actions use the plugin's worker
environment. A project
administrator chooses the flow, requesting project and lab role assignments.
This uses the types already installed and the ordinary project membership,
intake, file and record services. The project receives example inputs that
members can run and sign themselves. Each signature requires its holder to
authenticate and sign the revision they inspected.
[Lab roles](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md),
[requests](0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)
and [signatures](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md)

The first recipe is `dose-response` version `1.0.0`. It uses
`iki/example_lims.AnalysisRequest` for receipt and delivery and
`iki/plate_assays` for the plate, reader measurements and fit. The two
projects must be distinct, share a registry and permit sharing. Creating the
example also needs permission to edit the requesting project. The synthetic
reader data, the measured wells, and the analyst's exclusion decisions can
all be followed from the eventual result.

### Selecting and changing a flow

The project setting selects a declared concrete result type. The fit records
the choice with its measured evidence. A research result starts in Recorded
and needs one review signature to enter Signed off. For regulated QC, the
analyst signs the submission, an independent reviewer signs the review and
a third person in QA signs the release. The existing guards check the
creator, submitter, reviewer and editors as appropriate. A signed requested
result goes back to the project that sent the request, with a receipt for
the delivered revision.
[Result delivery](0149-a-result-leaves-its-service-only-by-release.md)

Applying a different choice changes which type can be created for a new fit.
Each existing result keeps its type, signature requirements and history.
Consequently a QC result that was already submitted still needs independent
review and QA release after a switch to research. Corrections to an editable
submitted or reviewed QC result return it to Recorded under the same flow.
The project choice takes precedence over the deployment setting for the fit.
The run snapshots that setting, including it in the result reuse key. If a fit
tries to create a result under an obsolete choice, its output is refused.
Launching a new fit uses the current choice.
[Corrections](0179-a-corrected-result-goes-back-for-review.md)

### Setup API

`GET /api/v1/starters?project={uuid}` lists installed starters, their versions,
types, role requirements and review flows. Each flow gives its requested
`result_type` and `required_roles` by qualified name. Each role states its
`minimum_project_role`. The project's current application appears in
`applied`, with the original `requester_project` available to an administrator
who can edit both sides of the setup. Omit the project to browse the installed
catalog.

`POST /api/v1/starters/{id}/preview` takes the body below, and
`POST /api/v1/starters/{id}/apply` the same body with the `plan` its preview
returned (ADR 0207). For example, a
project selecting regulated QC can ask for the following assignments. Replace
the placeholders with UUIDs. Each role names memberships returned by the
service project's members endpoint.

```json
{
  "project": "<service project UUID>",
  "requester_project": "<requesting project UUID>",
  "version": "1.0.0",
  "flow": "regulated_qc",
  "role_assignments": {
    "iki/example_lims.coordinator": ["<receiving membership UUID>"],
    "iki/plate_assays.analyst": ["<analyst membership UUID>"],
    "iki/plate_assays.reviewer": ["<reviewer membership UUID>"],
    "iki/plate_assays.qa": ["<QA membership UUID>"]
  }
}
```

Both responses identify the starter, version, projects, flow, result type,
role assignments and example IDs. Preview reports whether applying would
change the setup. `grants_to_add` maps roles to membership IDs and
`examples_to_create` names the examples the operation will create. `plan`
identifies the setup, what was set up already and every change applying makes.
Apply recomputes the plan under the projects' locks: if it differs from the
`plan` sent, as when another administrator removed a grant or applied the
starter first, nothing changes and it answers 409 `starter_plan_changed` with
the plan as it is now, for review. Repeating an identical apply yields
`changed: false` and the same example IDs. An application adds the requested grants to existing
memberships. A switch reuses the example records. The audit trail records
who applied the recipe and which version and flow were selected in
`starter.applied`, alongside the project’s individual role-grant events. Applying to
a new service configures a narrow intake for the chosen requester. When a
service already has an incompatible intake, the preview explains what its
administrator needs to change before applying.

### Type-flow API

`GET /api/v1/entity-types/{qualified_name}/flow` returns the type name, label
and a `fields` array in declaration order. Inherited state fields appear on
the concrete type. A type without state fields returns an empty array.

Each field contains `name`, `label`, `initial`, `states` and `transitions`.
States carry the existing type API's labels, qualified `set_by` roles, lock
and reason requirements, ordered signatures and guard messages. The state’s
`on_edit` names the automatic return when a correction is accepted. For a
diagram, draw a directed edge for each `{from, to, conditional}` transition
and a separate correction edge for each `on_edit`. A conditional edge means
that the guard still depends on information such as the record or actor.
The record's `can.states` and signing endpoints decide what a particular
person can do on a revision.
[State fields](0125-a-state-is-a-field-each-state-says-who-sets-it.md)
and [guards](0128-guards-say-when-a-state-may-be-set.md)

## Consequences

An administrator can change the default for future work without migrating a
record or invalidating a signature. The setup screen can preview the same
operation the CLI applies. The type page can draw a flow even before the
first record exists. Different review policies require installed type
variants, so adding a new policy is a reviewed plugin change.

# 0118. A record moves through its type's states, and only a transition moves it

- Status: Superseded by [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md)
- Date: 2026-11-17
- Refines: [0007](0007-typed-table-per-type-entity-storage.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0080](0080-triggers-run-actions-when-entities-change.md),
  [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)

## Context

A sample in a lab is received, tested, reviewed and released. LIMS model
this as a state machine for each kind of record: LabWare's sample
statuses, STARLIMS's workflows, Benchling's entity statuses and workflow
stages. Each step is limited to a role, and each is recorded. 21 CFR
Part 11 and GxP expect every step to be attributable to someone authorized
for it, with the reasons for corrections kept.

Iki's records could only be edited or archived. Who changed what was
recorded (ADR 0035), but nothing said where a record was in its lab's
process, or who had moved it on.

## Decision

**A type declares its states and the transitions between them, in its
manifest.**

```yaml
states:
  received: { label: Received, initial: true }
  in_testing: { label: In testing }
  in_review: { label: In review }
  approved: { label: Approved, locks: true }
  released: { label: Released, locks: true }
transitions:
  start_testing: { label: Start testing, from: [received], to: in_testing, roles: [analyst] }
  submit: { label: Submit for review, from: [in_testing], to: in_review, roles: [analyst] }
  approve: { label: Approve, from: [in_review], to: approved, roles: [reviewer], not_by: [submit] }
  send_back:
    label: Send back
    from: [in_review, approved]
    to: in_testing
    roles: [reviewer, qa]
    reason: required
  release:
    { label: Release, from: [approved], to: released, roles: [qa], not_by: [submit, approve] }
  reopen: { label: Reopen, from: [released], to: in_testing, roles: [qa], reason: required }
```

- A type has 2 to 30 states and up to 60 transitions. Exactly one state is
  `initial`, and neither it nor a `removed` one locks (ADR 0120, ADR 0121); every state can be reached
  from it. A transition leads from one or more states to another one.
- States are declared on a type none of whose ancestors has them. Its
  subtypes go through the same states and can't declare their own;
  extensions can't declare them. A type with an identity (ADR 0115) has
  states on its projects' records, never on its registry entries.
- Field names starting with `iki_` are reserved.
- The validator says what's wrong: `state-initial`, `state-removed`, `states-inherited`,
  `unknown-state`, `transition-to-itself`, `unknown-transition`,
  `state-unreachable`, and the lab role problems of ADR 0116.

**Each transition names the lab roles that may make it** (ADR 0116): any
one of them is enough. Making one also needs `project.transition`, which
every project role has, so a Viewer who is QA can release without being
able to edit. A project role is never enough: an Admin doesn't release a
sample unless they're QA too. No transition is made in an archived
project, on an archived record, or by a token without the `write` scope.
`reason: required` (or `optional`) asks whoever makes it why, and keeps
the answer. `not_by` keeps people apart (ADR 0119).

**A record's state is a column of its declaring type's table, and only a
transition changes it.** `iki_state` holds the state's name; the database
checks it's one of the type's states, and a new record, however it's
made, starts in the initial one. No edit, import, run or bundle sets it:
`values` never has it. A transition is
`POST /entities/{id}/transitions/{name}`, with the revision you read as
`If-Match` and your reason. It's a numbered revision of its own, of kind
`transitioned`, naming the transition and the states it went from and to,
with who made it, how and why. So a record's history shows each step
among its edits, and its state at any revision is known. The audit log
records each too (`entity.transitioned`, with the reason), so QA can
review who released what across projects, as Benchling's audit trail
shows status changes.

People make transitions, and agents for people (ADR 0072), recorded with
both. A transition that needs a signature (step 55) will be a person's
alone, as a workflow's approval is.

**Everything that shows records shows states.**

- Each record has its `state` (name, label, whether it locks) and
  `locked`. `can.transitions` lists the transitions from its state, each
  with whether you may make it and, if not, why: "It needs someone with
  the QA role in Atlas". Each type lists its `states` and `transitions`.
- The record's page shows its state and those transitions, the ones you
  can't make disabled with the reason beside them, and asks for a reason
  when the transition does. Its history shows each transition, with who
  and why. A type's grid shows the state of each record and filters by
  it.
- A search names `state` as a field of a type with states: `eq`, `ne`,
  `in`, `is_set`.
- Webhooks get `entity.transitioned`, with the transition and the two
  states, never the record's values (ADR 0035). A trigger can run on it,
  `on: entity.transitioned`, for any transition or with `states:` for
  those entering one of them (ADR 0080).
- MCP: `get_entity` says the state and what the person may do;
  `transition_entity` does it.
- Bundles carry each record's state and its transitions, so an imported
  record arrives where it was.

## Consequences

- A lab's process is part of a plugin: reviewed and versioned, the same in
  every project, and readable by anyone who reads the manifest.
- Who moved a record on, when and why, is in the history every client
  reads, beside its edits.
- What a person may do with a record is said by the server, with the
  reason, and every client shows the same thing.
- Changing a type's states is a migration, which can't strand a record in
  a state that's gone (ADR 0121).

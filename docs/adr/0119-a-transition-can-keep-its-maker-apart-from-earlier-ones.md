# 0119. A transition can keep its maker apart from earlier ones

- Status: Accepted
- Date: 2026-11-17
- Refines: [0118](0118-a-record-moves-through-its-types-states.md),
  [0079](0079-workflows-are-dags-of-ordinary-runs.md)

## Context

GxP expects separation of duties: whoever recorded results doesn't review
them, and whoever reviewed them doesn't release them. A lab often gives
one person several roles, an analyst who is also a reviewer, so roles
alone don't keep the duties apart. LabWare and STARLIMS check that the
reviewer isn't the analyst; GitHub doesn't count a pull request's author
among its approvers. A workflow's approval already can't be given by
whoever started the workflow (ADR 0079).

## Decision

**A transition can name earlier transitions whose makers may not make
it**, with `not_by`:

```yaml
approve: { from: [in_review], to: approved, roles: [reviewer], not_by: [submit] }
release: { from: [approved], to: released, roles: [qa], not_by: [submit, approve] }
```

For each transition named, whoever made its latest occurrence on the
record can't make this one, whatever roles they hold. A record sent back
and submitted again by someone else may be approved by its first
submitter: the latest submission is the one judged. Names in `not_by` are
the type's own transitions (`unknown-transition`).

It's checked by the server, under the record's lock, against the
record's revisions, so no client can get around it. Someone who holds the
role but made the earlier step sees the transition offered but disabled,
with why ("You made “Submit for review” on it, so someone else makes
“Approve”: the plugin keeps them apart"); making it anyway is refused
with 409 `refused`, as the record's own condition is, where a missing
role is 403. An agent acting for someone is that person here.

## Consequences

- Two people are needed wherever a plugin says so, and the history shows
  who they were.
- A lab with one person in a role can't release what that person
  submitted. That's the rule working; such a lab writes a plugin without
  `not_by`, where the record still shows who made each step.
- The rule is per record and per transition, not per person across
  records: a reviewer can approve one sample and submit another.

# 0116. Lab roles are declared by plugins and given per project

- Status: Accepted
- Date: 2026-11-17
- Amended by [0210](0210-delegated-actions-require-a-shared-lab-role.md) for delegated actions
- Refines: [0003](0003-roles-and-permissions.md),
  [0079](0079-workflows-are-dags-of-ordinary-runs.md)

## Context

A project's roles (ADR 0003) say what someone may touch: Viewers read,
Editors change, Runners spend compute, Admins manage. A lab also needs to
say who may _decide_: who tests a sample, who reviews another analyst's
results, who releases what was reviewed and who reopens it. Those aren't
levels of access. A QA reviewer often may not edit the data they release,
and a project's Admin is often not QA. GxP and 21 CFR Part 11 expect each
decision to be made by someone authorized for it, and the authorization
itself to be controlled and recorded.

LIMS precedent agrees on the shape. LabWare and STARLIMS give users named
roles (analyst, reviewer, QA) checked at each step of a sample's life,
separately from their menu access. Benchling's workflows name the teams or
people who may review a stage. GitHub keeps its fixed repository roles and
adds narrower ones (code owners, required reviewers) on top, checked where
they matter.

Workflow approvals (ADR 0079) needed a project role, "Runner or above".
Record states (ADR 0117) need the same thing per transition, and
electronic signatures (step 55) will need it per signature.

## Decision

**A plugin declares its lab roles, by name, with a label and a
description.**

```yaml
roles:
  analyst:
    label: Analyst
    description: Tests samples and records their results, then submits them for review.
  qa:
    label: QA
    description: Releases reviewed samples, and reopens released ones when they need correcting.
```

A role's qualified name is `namespace/plugin.name`, such as
`iki/lab_roles.qa`. A plugin refers to its own as `qa` and to another's
as `acme/qms.release_officer`, which needs that plugin in its
`dependencies`, so labs can share one QA role through a common plugin. The
names `viewer`, `editor`, `runner` and `admin` are reserved for the
project roles. A plugin declares at most 50. The validator refuses a role
nobody declares (`unknown-role`) and warns when two of a plugin's roles
have the same label (`duplicate-role-label`). A plugin can't see other
plugins' roles when it's checked, so two of them may each call one QA:
wherever such a role is named, in the dialog that gives it, notifications,
audit records and refusals, its plugin's title follows its label, "QA
(Lots)", as Slack names the app beside a command two apps share. Iki's own
plugins label theirs by what they release, Lot QA and Sample and result QA.

Org Admins don't define roles in the web app. What a role lets someone do
is written in the plugin that checks it, so the two are reviewed together,
in Git, as the rest of a plugin is (ADR 0034). A lab that wants another
role declares it in a plugin of its own.

**A project's Admins give lab roles to its members, beside their project
role.** A grant is a membership's: a person's, or a team's, which everyone
in the team then holds, as they hold its role. Someone may hold several,
directly and through teams; they add up. A lab role counts only while its
holder can read the project, is active, and a plugin declares it.
Removing a membership removes its lab roles, and the removal's audit
record lists them. Service accounts can hold lab roles, as they hold
project roles; whether a service account may then act on one is for the
check that uses it (no token approves a workflow step).

Giving lab roles needs `project.members.manage`: a project's Admins, and
Org Admins for recovery, as for members. It's allowed while a project is
archived, as member changes are.

**A lab role grants nothing by itself.** It's one more condition that a
decision names, never a way around a project role. Holding QA lets you
release only where a plugin says releasing needs QA, and only on what your
project role already lets you see. Being an Admin doesn't hold any lab
role.

**What needs one:**

- **workflow approvals**: `approval: { role: qa }` waits for someone who
  holds QA in the project, other than whoever started the workflow. A
  project role still works as before, meaning that role or above. Only the
  role's holders are told;
- **record transitions** (ADR 0117), each limited to lab roles;
- **signatures** (step 55), whose transitions name a role and a meaning.

`GET /lab-roles` lists every declared role with its description and what
needs it ("approve “Release” in Release a batch"), which the Members page
shows each role with. `GET /projects` gives each project's `my_lab_roles`,
and each member lists their `lab_roles`.

**Each change is audited, announced and told.** Each role given or taken
away is one audit record, `project.lab_role_granted` or
`project.lab_role_revoked`, naming the person or team, the role and its
label, and marked `self` when an Admin gave themselves one. Webhooks get
the same two events. The person, or nobody for a team, gets one
notification per change: "Ada Lovelace made you QA and Reviewer in
Atlas".

**A role a plugin stops declaring stays granted but counts for nothing.**
Upgrading a plugin that drops a role keeps its grants, shown as "no longer
declared", so a role renamed back or a plugin reinstalled doesn't silently
lose who held it, and Admins clear them when they choose. A new grant of a
role nobody declares is refused.

## Consequences

- Lab decisions are checked against named, audited authorizations that a
  plugin documents, without widening any project role.
- A team's lab role covers whoever joins it, which is what a lab with a QA
  team wants, and what its Admins must keep in mind.
- A lab that renames a role in its plugin must give the new one again. The
  old grants remain listed until cleared.
- A role's meaning lives in a plugin, so changing what QA may do is a plugin
  change, reviewed and versioned, not a setting someone can flip.

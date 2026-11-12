# 0067. A project can keep its entities to itself

- Status: Accepted
- Date: 2026-11-12
- Refines: [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0057](0057-reuse-reproduce-exactly-and-seeds.md),
  [0066](0066-a-run-reads-only-whats-in-its-project.md)

## Context

An entity can be in several projects: its owner project, plus any it's
linked into (ADR 0035). Anyone who can read it and edit another project
can link it there, and since ADR 0066 a run in that project may add its
inputs to it first. That suits a lab sharing its own proteins between
studies.

Some data mustn't leave its project. A CRO's or partner's compounds are
covered by a contract, and a blinded study's assignments must stay hidden
from the people analysing it. Someone who works on both a partner's
project and an internal one could still link the partner's entities into
the internal project, or refer to them there. The partner's data would
then reach people the contract doesn't allow.

The setting is about confidentiality, so it mustn't reveal anything
either. Someone working in another project who registers the same
sequence shouldn't learn that a restricted project already has it.

## Decision

**Each project has the setting "Allow sharing this project's entities with
other projects".** It's on by default. Its Admins change it on the
project's settings page, under _Sharing_, or with `allow_sharing` in the
project API. Each change is audited as `project.sharing_allowed` or
`project.sharing_restricted`, and sent to webhooks. It can change while the
project is archived, since restricting sharing only protects its data.
Turning it on again asks first, because it lets the data leave. A project
that doesn't allow sharing shows the badge _Not shared outside this
project_.

**When it's off, no other project may use the project's entities.**

- No one can link them into another project: "PRT-000012 can't be shared
  outside Oncology CRO: its project doesn't allow sharing its entities
  with other projects."
- No run in another project may read them, so `add_to_project` (ADR 0066)
  is refused for them, with the same message.
- No entity in another project may refer to them, or to the project's
  files. Each such reference is refused, saying which project keeps it.
- A run in another project doesn't reuse a run of this one (ADR 0057).
  Reusing it would link what it made into the other project.

**Elsewhere, they're as if the person couldn't read them.** Duplicate
checks, on a create or an import, count only entities the person can read
_and_ the project may use. Someone who registers the same sequence in
another project gets their own entity, as someone who can't read the
restricted project would. Nothing says another one exists. References and
links name the restricted project only to people who can already read it.

**What's already shared stays shared.** Turning the setting off doesn't
unlink anything. Entities already linked into other projects stay there,
and those projects may keep using them, until someone takes them out.
Once taken out, they can't be linked back.

## Consequences

- One check, `access.shareable_into(user, project)`, decides what a
  project may use. Links, duplicates, references, run inputs, reuse and
  imports all apply it, so a new way of sharing must too.
- A restricted project's data leaves only through what its members can
  already do: export, download, or retype it. Those stay recorded in the
  audit log as today.
- A restricted project can still use entities from projects that allow
  sharing.
- An Admin who restricts sharing on a project that already shares entities
  is told that those stay shared. Each entity's page lists the projects it's
  in, and the link API takes it out of them.

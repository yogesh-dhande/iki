# 0149. A result leaves its service only by release, and a request's files stay with it

- Status: Accepted
- Date: 2026-11-19
- Refines: [0067](0067-a-project-can-keep-its-entities-to-itself.md),
  [0132](0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md),
  [0142](0142-a-request-tells-its-service-and-the-project-that-asked.md)

## Context

ADR 0132 makes a request's results the service's until it releases them.
Releasing one links it into the project that asked, audits it as
`request.result_released`, tells that project's people (ADR 0142), and
keeps it there. A released result is a GxP record: it was reviewed, and
QA said it can be used.

Sharing is a second way into another project. Someone who works in both
the core and the project that asked, as core staff often do, could share
a result with the asking project while it was still a draft, as any
record of the core's. A result shared that way was readable there before
QA released it, and its later release found it there already: nothing was
audited, nobody was told, and it could be taken out again.

Files have the same gap. ADR 0132 says what a request brought into a
project goes no further. A record that came with a request couldn't be
shared on, but a file it refers to could be referred to anew, from a
record in the project it came to or in another project, and that record's
readers then read the file.

LIMS practice is that a result isn't reported outside the lab until it's
authorized, and that what a client sent is used for the work it was sent
for, and not passed on.

## Decision

**Until its service releases it, a result is the service's alone.** No
other project may use it: it can't be linked into one, referred to from
one, sent with another request, read by a run in one, or reused by one.
This is part of `access.shareable_into`, the one check ADR 0067 makes
every way of sharing apply, so a new way of sharing applies it too.
Sharing one says: "ARS-000012 can't be shared with Kinase Atlas: it's a
request's result its service hasn't released, and it's the service's
alone until then. Releasing it shares it with the project that asked."

**Released, it's shared as the service's other records are.** The
service may share it with its other projects, as ADR 0067 allows. The
project that asked reads it and passes it on no further (ADR 0132). It
reaches the project that asked by its release alone.

**Every release is audited and told.** If the result is already in the
project that asked, whatever put it there, its release still records
`request.result_released`, tells the people its type names (ADR 0142),
and keeps it there for good. A result released already, set back to an
earlier state and released again, isn't released twice.

**A file that came with a request is read with the records it came on.**
Its readers there read it as before, but no record refers to it anew, in
that project or any other. The project whose file it is still refers to
it as its own. Someone who reads it their own way, through a project or
a record they read other than through the request, may refer to it as
ADR 0067 allows, within one registry. Referring to it otherwise says: "refers
to a file you read through a request between projects: it's read with the
records it came on, and nothing else refers to it."

A reference that names a record the project may not use now says why:
it's an unreleased result, it came with a request, its project doesn't
allow sharing, or it's in another registry.

## Consequences

- A core's draft results can't be shared with its own other projects
  either. Releasing them first is the gate: a result in review isn't one
  anybody should build on.
- A core that needs a sample's file in its own results refers to the
  sample, which names the file, rather than to the file. The asking
  project does the same with a released result's file.
- A result from a bundle (ADR 0096) whose request arrived unsent stays in
  its project: its request was never sent, so it's never released.
- Changes to what a service takes (`project.intake_changed`) are made
  under a lock on the project, and each is audited against the change
  before it, so two Admins changing it at once each leave a true record.

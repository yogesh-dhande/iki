# 0066. A run reads only what's in its project

- Status: Accepted
- Date: 2026-11-12
- Refines: [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0042](0042-action-runs-and-jobs.md)

## Context

ADR 0042 let a run's inputs come from any project the person launching it
can read, while what the run makes belongs to the run's project. Someone who
can read projects A and B could then run an action in A on an entity from B.
Everyone in A would see the run, its log and what it made, which can carry
B's data: a sequence, a structure, an alignment of B's proteins. None of
them could read B.

Checking each output against the readers of every input would have fixed
that, but every list, search, export and webhook would have had to apply
the check. ADR 0044 already stops a run from attaching files the person
could read elsewhere, for the same reason.

## Decision

**Everything a run reads must belong to its project.**

- Each input entity must be linked into the run's project, as its own
  entities are.
- Each file param must be one of the project's files: added to it, or used
  by one of its entities.
- An input from another project is refused (409, with the code
  `inputs_outside_project`), naming each such input.
- A file from another project is refused as invalid. Files aren't linked, so
  the person uploads the file to the project.
- Other problems with the launch are reported first.

**Adding inputs to the project is one explicit, audited step.** Launching
with `add_to_project` links the outside inputs into the run's project
first, then launches, in one transaction. It is the same operation as the
link API: each link is recorded as `entity.linked`, and the projects'
webhooks hear of it. Anyone who may launch a run is at least an Editor in
its project, and can read the inputs, so they may also link them.

- The web app asks first: "Add to Atlas?", naming the inputs and saying
  that Atlas's members will be able to read them.
- The SDK takes `add_to_project=True`.
- Agents using MCP don't get the option. Widening who can read data is the
  person's decision, so the error tells the agent to ask them.

**Reproducing a run needs its inputs to still be in its project.** If one
has been unlinked since, reproducing the run is refused, naming it.

**Unlinking is still allowed.** What earlier runs made stays in the project,
like a copy someone made while they could read the input. The audit log
records the link and the unlink, and provenance shows an input the reader
can no longer see as an entity they can't read.

## Consequences

- A run's readers can read all of its inputs, so runs, logs, artifacts and
  outputs need no checks beyond their project. Every page and API stays as
  simple as it is.
- Sharing data with another project is always a deliberate act on the data,
  never a side effect of running something. Each such act is audited.
- Reused results (ADR 0057) and duplicates (ADR 0035) already linked into
  the run's or creator's project, and still do.
- Scripts that ran actions across projects now get a 409 and must add the
  inputs to the project, once.
- A project can forbid sharing its entities
  ([ADR 0067](0067-a-project-can-keep-its-entities-to-itself.md)), which
  refuses `add_to_project` for them.

# 0042. A run records what was asked; jobs are its attempts

- Status: Accepted
- Date: 2026-11-11
- Refines: [0040](0040-action-and-environment-manifests.md),
  [0041](0041-compute-templates-and-bindings.md)

## Context

A result in Iki has to say where it came from: who asked for it, which
entities at which revisions it was computed from, with which params, by
which code. Work can also be lost partway through, when a spot machine is
taken back or a worker dies, and be tried again. Labs need the record of
what was asked to stay one thing, however many attempts it takes.

## Decision

**An `ActionRun` is one request**, numbered `RUN-000123`. It records:

- the action, the plugin's version and commit, and a copy of the action's
  manifest as installed, so the record stays true after the plugin changes
- the project, who asked, how (web, API, MCP, a trigger) and why
- each input entity with the revision it read (`RunInput`, append-only)
- every param, with defaults filled in
- its status: queued, running, then succeeded, failed, cancelled,
  timed_out or rejected

**A `Job` is one attempt.** It is what workers lease, with what the attempt
needs (tier, environment, resources) and a lease token that changes on every
lease, so only the current attempt can report. A lost or interrupted attempt
becomes a new job, up to the action's retries. Client-tier jobs name their
owner, so only that person's own worker can take them.

**Everything is checked before a run is queued**, so no worker starts work
that can't be committed:

- the caller's role for the tier in the project: Editor for client and
  backend, Runner for container and vm
- that this deployment can run the action: its environment is bound on its
  tier (client-tier actions need no binding)
- each input is an entity the caller can read, of the input's type or a
  subtype, and not archived
- the caller can edit every entity the action updates. A run in your
  project can't change entities you can only read.
- each param fits its type and limits, and required params are given

Entities created by a run belong to its project. Inputs may come from any
project the caller can read; the run records them as they were read.

**Cancelling** takes the same role as running. A queued run is cancelled at
once. A running one is marked, and its worker stops it.

## Consequences

- Provenance can point at a run, and through it at the exact inputs, params
  and code behind a value (commit 21c writes results).
- Plugin upgrades don't rewrite history: each run keeps the action as it
  was.
- Run numbers come from their own sequence, created once and never dropped,
  like entity HRIDs.

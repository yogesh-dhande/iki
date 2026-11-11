# 0044. A run's results are checked, then written all or nothing

- Status: Accepted
- Date: 2026-11-11
- Refines: [0042](0042-action-runs-and-jobs.md),
  [0043](0043-workers-lease-jobs.md)

## Context

An action's command is plugin code running on a machine Iki doesn't
control. Whatever it reports goes into a lab's record of its science, so
it can only change what its action declared. A result has to say where it
came from, and a half-written result is worse than none.

## Decision

**The command writes `output.json`** (`run-output/v1`):

- `updates`: new values for input entities, by input name
- `creates`: new entities, each listing the input entities it was made from
- `metrics` and `messages` for the run's record

The worker sends it with `POST /worker/v1/jobs/{id}/complete`.

**It's checked against the action as launched before anything is
written.** The output may only:

- update inputs the action says it updates, the entities it was given
  there, and only the declared fields
- create the declared types (or their subtypes), within the declared
  cardinality, each derived from inputs the action names. When those
  inputs hold one entity, `derived_from` can be left out.

**Then it's written in one transaction**, or not at all:

- Every value is checked against its entity type as installed now.
- It's written as the person who launched the run, with `via="run"`.
  Runs may set computed fields; people can't.
- Each entity the run touched gets a `RunEffect` with the revision it
  made. Provenance follows these to the run that set a value.
- Each new entity gets `Lineage` rows to the inputs it came from, at the
  revisions the run read.

**The launcher's access is checked again.** If they were deactivated or
lost their role, or the project or an input entity was archived meanwhile,
the run ends `rejected` and nothing is written.

**How a run ends when its results aren't written:**

- `failed` (`invalid_output`) when they don't fit, with a JSON pointer to
  each problem. This is the plugin's bug, so it isn't retried.
- `rejected` (`schema_changed`) when they don't fit because the plugin was
  upgraded while the run ran.
- `rejected` for the access and archiving conflicts above.
- `cancelled`, writing nothing, if it was cancelled before it finished.

**The output is kept whatever happens** (`RunOutput`, append-only), so a
rejected run's work isn't lost.

## Consequences

- A plugin can't write outside its declared effects, even with a bug or a
  compromised worker.
- Entity values that changed after the run read its inputs are still
  written over. Provenance shows which input revisions the run read.
- Result files are uploaded by the worker before it completes, as ordinary
  checked uploads for the launcher. A run may attach only its own uploads
  and files its inputs already had. Attaching any file the launcher can
  read would let a worker share it into the run's project.
- References in results point at entities the launcher can read.

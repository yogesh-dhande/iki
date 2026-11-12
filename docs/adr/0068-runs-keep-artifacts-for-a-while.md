# 0068. Runs keep artifacts for a while

- Status: Accepted
- Date: 2026-11-12
- Refines: [0036](0036-files-content-addressed-uploads.md),
  [0044](0044-run-results-are-written-all-or-nothing.md)

## Context

A run's command often writes files besides its results: an alignment, MSAs,
a ranking table, a debugging log. People need them to check a result or see
why a run failed, but they aren't values of any entity. Actions could already
declare them (`artifacts: { keep: [...], retention: 365d }`), but nothing
kept them.

Files in Iki are content-addressed and never deleted (ADR 0036). A blob may
back several files, and a file backs every revision that ever referred to
it. Artifacts are the opposite: many, often large, and needed for a while.
Keeping every MSA a structure predictor writes would cost more than the
predictions.

## Decision

**A run keeps the files its action's `artifacts.keep` patterns match.**

- Once the command ends, whether it succeeded, failed or ran out of time,
  the worker uploads each file in `$IKI_OUT_DIR` that a pattern matches.
  Not when the run was cancelled or the worker is stopping.
- Patterns match a path one segment at a time: `*` and `?` within a
  segment, `[...]` a set, and a `**` segment any number of segments. Iki
  (`iki_spec.globs`) and the worker match them the same way, and Iki checks
  each path again.
- Symbolic links aren't followed or kept. Paths are relative, with no `.`
  or `..` parts.
- An attempt keeps at most 1,000. A file that can't be kept is said in the
  attempt's log, and doesn't fail the run.
- Batchable actions can't keep artifacts: a packed job's files wouldn't
  belong to any one run.

**Artifacts aren't files.** Each is an upload of its own kind, checked
against its SHA-256 like any other, then kept under a key of its own
(`artifacts/<id>`), not by its content. Deleting it deletes nothing else. It
belongs to the run and its attempt, so anyone who can read the run can
download it, from the run's page, the API, or the SDK
(`runs.download_artifact`). It isn't in the project's files, and entities
can't refer to it. A result an entity needs is a file field, as before.

**Each is kept for its retention, then deleted for good.**

- Retention is the action's `retention`, or the deployment's default
  (`files.artifactRetention`, 90 days), and never less than the
  deployment's minimum (`files.artifactMinRetention`). A deployment that
  must keep records for ten years sets the minimum to `3650d`.
- `iki-server maintain` deletes artifacts whose retention is over, every
  version of each object, so a versioned bucket keeps none of them. The
  pods' role needs `s3:DeleteObjectVersion` and `s3:ListBucketVersions`.
- Each deletion is audited once per run, as `run.artifacts_deleted`, with
  how many, how many bytes, and their paths.
- The artifact's record stays: its path, size, SHA-256, and when it was
  deleted. Downloading it then answers 410.

## Consequences

- People see why a run failed from what it wrote, not only its log.
- Storage for artifacts is bounded by retention, and a regulated deployment
  can keep them as long as its records must be kept.
- An artifact is evidence only while it's kept. Anything a decision rests on
  belongs in an entity's values, where it's kept for good.
- Agents see a run's artifacts in `get_run`, but don't download them: what a
  command wrote is untrusted content.
- Checkpoints (`checkpointable`) are next: they're kept only while a run
  might resume, so they need their own lifetime.

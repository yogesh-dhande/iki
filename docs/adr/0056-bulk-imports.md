# 0056. A run's creates can come from a file, checked in resumable chunks and written all at once

- Status: Accepted
- Date: 2026-11-11
- Refines: [0044](0044-run-results-are-written-all-or-nothing.md), [0052](0052-imports-file-params-and-origins.md)

## Context

A run writes its results from `output.json`, in one transaction, or not at all
(ADR 0044). That holds at most 10,000 creates in 2.5 MB, and each is written as
a person's create is: a handful of queries, and a webhook event. That's right
for a structure prediction's model or a FASTA file of a few hundred
sequences. It isn't for the imports our users run: a vendor's catalogue of
millions of compounds, a sequencing project's assemblies, a panel's worth of
GenBank records. One entity at a time, 10 million entities take most of a day,
and a worker that dies near the end leaves nothing.

Two things don't change at this scale. An import is still all or nothing: a
reviewer in a regulated lab expects an import to be there whole or not at all,
and nobody should act on half of one. And each entity still gets everything a
create gives it: its revision, history, project, provenance (the run that made
it, where it came from) and duplicates found as for any create.

## Decision

**The command names a creates file.** Instead of `creates`, `output.json` may
name `creates_file`: a file in `IKI_OUT_DIR` with one create per line, each as
an item of `creates` is (JSON Lines). The worker uploads it with the run's
other files, through the same multipart uploads, and puts its ID in its place.
The DNA plugin's import switches to one past a thousand records (or a
megabyte of them), reading its file a line at a time, so it imports up to 10
million records in one run.

**The job stays `committing` while the task runner imports the file**, in two
phases:

1. **Checking**, a chunk of 10,000 lines (or 32 MiB) per transaction. Each
   line is checked as an item of `creates` is: against the action as
   launched, then against its type's fields and rules as the launcher, with
   the same code as a create (`entities.prepare`). A line's problems point at
   `#/creates_file/<line>`. Checked lines are staged with COPY in
   `iki_import_line`, and each chunk commits with how far it got (the byte
   offset and line count). A task works for 30 seconds, then enqueues the next,
   so a long import doesn't hold a runner. An import interrupted for any reason
   (its runner's pod evicted, the database restarted) goes on from its last
   chunk, and a big file is never checked twice. After 100 problems it stops
   looking, and the run fails with them.
2. **Writing**, once every line is checked and none has problems: everything
   in one transaction, with `INSERT … SELECT` from the staged lines. That
   covers entities, their tables, history and list items, revisions, project
   links, the run's effects, external origins and lineage. It also includes
   the rest of the output (`updates`), re-checked against the launcher's access
   as any result is.

Duplicates are resolved in SQL as a run's creates resolve them. For types
that reuse duplicates, an entity the launcher can read with the same identity
is reused and linked into the run's project, and so is an earlier line's. While
it writes, the import holds an exclusive advisory lock per identity type. Every
create holds that lock shared, so none can slip a duplicate in meanwhile. HRIDs
come from each type's sequence in line order.

If a plugin changes after checking began, the lines may no longer fit, so the
import checks them all again. It records a hash of the installed plugins and
starts a new round when they differ. Cancelling stops it at its next chunk,
writing nothing. A step that fails unexpectedly is retried with backoff, and
after five in a row the run fails `import_failed`. The task runner's
housekeeping gives a stalled import another step. Staged lines are deleted
50,000 at a time once an import ends.

**Staging is one table, made by a migration.** The app's database role can't
create tables, and shouldn't be able to. `iki_import_line` is keyed by
import, round and line, without foreign keys, so millions of rows don't each
check one.

**Two differences from `creates`, documented in the spec.** Webhooks get one
`entity.imported` event for the run (with counts) instead of `entity.created`
for each entity: a receiver can't usefully take ten million events, and the
run's effects list every entity. And lines can't attach files: each would be
its own upload, and imports of that kind come in `creates`.

**Reading results scales too.** A run's answer lists its first 100 effects,
counts all of them by kind (the import's counts, not a count of millions of
rows on every read), and gives the cursor for `GET /runs/{ref}/effects` to go
on. A new index on `(run, id)` keeps that a range scan. The run page shows the
counts and pages with Show more. An attempt shows how far its import has got.

## Consequences

- An import is still all or nothing. Its write is one long transaction:
  about 20 minutes for 10 million proteins on a laptop's database (25 for the
  whole import), most of it maintaining the trigram and full-text indexes
  every create pays for too.
  Foreign keys are checked after each statement, not all at commit, so their
  checks don't queue in memory.
- `make check-scale` imports 10 million proteins, interrupted halfway, and
  checks every one was written once. CI runs it nightly and on demand. It
  takes too long for every push.
- Checking is one task at a time per import, about 28,000 lines a second. If
  imports need to go faster, the file's byte ranges could be checked by
  several runners at once, since each chunk is already independent.

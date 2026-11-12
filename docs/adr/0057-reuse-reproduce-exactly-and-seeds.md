# 0057. Identical deterministic runs reuse results; any run can be reproduced exactly

- Status: Accepted
- Date: 2026-11-12
- Refines: [0042](0042-action-runs-and-jobs.md),
  [0047](0047-workers-run-only-the-code-a-run-was-launched-with.md)

## Context

A structure prediction can take hours on a GPU. People launch the same one
twice often: a colleague in another project, a batch launched again after a
few runs failed, or an agent that doesn't know it ran yesterday. An action
that declares `deterministic: true` says its results depend only on what it
was given, so a second run makes nothing new.

Regulated labs ask a different question of a run that already happened: does
it reproduce? An auditor wants to see the record's computation done again,
with the same software and inputs, giving the same answer. That's only
possible if a run records everything its results depend on, including its
random numbers.

## Decision

**Every run gets a seed.** At launch, Iki draws 31 random bits (so any
tool's seed fits) and records them on the run. The command gets the seed as
`IKI_SEED`, and as `run.seed` in `input.json`. A command that uses random
numbers should seed them from it. A packed job's runs all record the seed
the command was given, which is the first run's.

**A deterministic run has a reuse key.** It's a SHA-256 of everything its
results depend on:

- the action and its plugin's commit;
- its params, with a file param counted by its content, so the same file
  uploaded twice is the same;
- its plugin's settings;
- each input's entity, at the revision it reads, with its type's schema
  version.

Runs of local code (no commit) get no key, since that code may have changed
in between. Reference data versions will join the key when managed volumes
arrive. The seed isn't in the key, because a deterministic command doesn't
depend on it.

**A launch reuses an identical run's results.** The earlier run must:

- have the same key;
- have succeeded;
- have run, rather than reused another run;
- be one the launcher can read;
- have made at most 10,000 entities, none of them archived.

If one does, the new run doesn't queue. It ends at once as succeeded, with
`reused_from` naming that run, and no attempts. The entities that run made
are linked into the new run's project (each link is audited) and listed as
the new run's effects, as `reused`. It shows that run's metrics and
messages. This is like Nextflow's `-resume` and Bazel's action cache, so
it's on by default, and nothing needs approving or counts against quotas.
`reuse: false` on a launch (the run page's **Run again**) runs it anyway.

Only runs the launcher can read count. Someone in another project gets a
run of their own, so no reuse tells them that an identical run happened
somewhere they can't see. A run whose updates changed its inputs never
matches a later one, because the later one reads a newer revision. Runs that
made more than 10,000 entities (bulk imports) are run again rather than
linked within one request. Their duplicates are reused anyway (ADR 0056).

**Reproduce exactly runs a run again, and writes nothing.**
`POST /runs/{ref}/reproduce` (the run page's **Reproduce exactly**) queues a
run with the original's commit, its action as launched, params, plugin
settings and seed. It reads the inputs at the revisions the original read,
even if they've changed since. Workers already lease only runs of the
commit they have (ADR 0047). Iki refuses the request (409) when the
deployment runs other code for the plugin, or when the original has no
output to compare with. A run that reused or reproduced another one is
reproduced as that one.

The reproduction asks for approval and counts against quotas like a launch.
It's in the original's project and takes the role that runs its tier there.

When it ends, it doesn't write its results. Writing them would update
entities from old revisions, or make a second copy of every structure.
Instead, it compares its output with the original's: creates, updates,
creates file and metrics, but not messages. Files are compared by content,
not by ID. It records `reproduction`, which says:

- `matches`;
- the first 20 differences, each as a JSON pointer with both values, and how
  many more there are;
- both environment builds, which must be the same for a match.

The run page shows **Reproduced**, or **Didn't reproduce** with the
differences.

## Consequences

- Launching an identical deterministic run is instant and free, and says
  which run's results it reused. Batches reuse run by run, so approval
  covers only the runs that will run.
- A GxP reviewer can reproduce any run whose plugin commit is still
  deployed, and gets a recorded yes or no with the differences. Reproductions
  are themselves runs, with their own audit trail, attempts and logs.
- An action that wrongly declares `deterministic` (a fetch from a source
  that changes) would have its results reused when they shouldn't be. The
  manifest linter already warns on `deterministic` with `network`, and
  Reproduce exactly shows the difference.
- Floating-point results that vary across GPUs show up as differences. That's
  the truth about the run, and why its environment build is recorded too.

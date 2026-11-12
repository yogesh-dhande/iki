# 0055. Runs launched together share a batch, and a batchable action packs them into jobs

- Status: Accepted
- Date: 2026-11-11
- Refines: [0042](0042-action-runs-and-jobs.md), [0043](0043-workers-lease-jobs.md), [0044](0044-run-results-are-written-all-or-nothing.md)

## Context

Scientists run one action over many entities: molecular weights for a
200-protein panel, GC content for every sequence of an import. Iki already
launches one run per entity, all or none (`POST /actions/{name}/runs/batch`).
Each run keeps its own inputs, provenance, retries and results, as a GxP
reviewer expects of each result. But the runs had nothing in common
afterwards, so no page or API answered "how is the panel going?", and
nothing cancelled it as a whole.

Each run was also its own job. For an action that takes milliseconds per
entity, a worker spends far longer leasing, staging and reporting than
computing: 200 jobs where one would do. ADR 0009 kept `batchable: N` for
this, without saying how it works.

## Decision

**A batch is a record.** Launching on several entities makes a `RunBatch`
(`BAT-000123`): the action, project, who, how (web, API, MCP), why, the input
each run took one entity for, and how many runs. Each run names its batch.
A batch has a page and an API (`GET /batches/{ref}`, `GET /runs?batch=`, in
launch order) that count its runs by status, and live updates as its runs
start and end. Cancelling a batch cancels each run that hasn't finished, as
cancelling each would, and is audited once. One approval still covers a batch
an agent asks for, and its notification links to the batch.

The API launches up to 1000 runs at once. The web app launches up to 200,
because it names the entities in the new run's address, and proxies commonly
refuse requests past 8 KB.

**Packing happens at lease time, not launch time.** The runs and their jobs
stay one per entity. When a worker leases a job whose action is
`batchable: N`, the server packs up to N−1 more queued jobs of the same
batch into it: the same plugin version and code, environment and tier, all
ready to run. The worker is unchanged. It gets one job whose one input lists
every packed run's entity, and a `pack` list of which entity is which run's.

The attempts packed into the job follow it. Its state and progress are
theirs, its log is theirs (kept once, read from each run's page), and its
egress proxy credentials and secrets are the one job's. A pack counts once
against running limits: it's one command on one worker.

**Results are split back per run.** Each update goes to its entity's run;
each new entity to the run of the entity it's `derived_from` (in a pack it
must say); each message to its `entity`'s run, or to every run if it names
none; metrics to every run. Then each run's part is checked and written as
any run's output is, on its own: one run's results that don't fit, or that
can't be written now, fail or reject only that run. Output that doesn't say
which run a part is for fails every run in the pack, with where and why.

**A message at level `error` about an entity fails its run.** The run writes
nothing and ends failed, with the message as its error. This lets a packed
command fail one entity without failing the rest.

**Each run still ends on its own.** A pack that fails, times out or loses its
worker ends every attempt in it, and each run retries under its own policy
(its retries may be packed again). Cancelling one run of a pack doesn't stop
the command, since others need it: the run ends cancelled, with nothing
written, when the pack ends. The command stops once every run in it is
cancelled.

## Consequences

- Per-run records are as they were: each run's page, provenance, retries,
  result reuse and audit trail are its own, packed or not.
- A batchable action's command must handle several entities in its input,
  and say which entity each new entity was derived from. The validator
  already allows `batchable` only for actions with one input taking one
  entity.
- A command that fails fails the whole pack. One that can tell a bad
  entity from the rest should go on, and report an `error` message about
  that entity: that entity's run fails, with the message, and writes
  nothing, while the others keep their results. The same holds for a run
  that isn't packed.
- Packing only joins runs of one batch. Runs launched one at a time, or by
  triggers (later), run one to a job.

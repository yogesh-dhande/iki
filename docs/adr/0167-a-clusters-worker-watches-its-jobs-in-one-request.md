# 0167. A cluster's worker watches its jobs in one request, and works with an older server

- Status: Accepted
- Date: 2026-11-20
- Refines: [0043](0043-workers-lease-jobs.md),
  [0060](0060-workers-update-themselves-to-signed-releases.md),
  [0133](0133-a-slurm-cluster-runs-container-tier-jobs-its-templates-place.md),
  [0156](0156-a-slurm-jobs-log-is-masked-where-its-written.md)

## Context

A worker holding one job learns of a cancel two ways: a long-poll on the
job (`POST /worker/v1/jobs/{id}/watch`), which answers the moment the run is
cancelled, and each heartbeat's answer. A Slurm cluster's worker holds up
to a hundred jobs, so ADR 0133 left their cancels to heartbeats alone,
rather than hold a long-poll for each. A cancel then took up to 20 seconds
to reach the cluster. Worse, it didn't reach the cluster at all while its
heartbeats failed in a way that left the lease renewed: the job ran on to
its time limit, and kept its nodes.

One such way was a worker newer than its server. ADR 0060 accepts newer
workers, and ADR 0156 added `log_offset` to the heartbeat's answer. A worker
that required it couldn't read an older server's answer at all, so it read
no cancel from it either. And a cluster's worker that restarted asked again
and again for a lease the server had already renewed.

A restarted cluster worker also lost why its job said it failed. It reads
the job's log file on from where the server's copy ends (ADR 0156), so a
`::error` line from before the restart isn't read again, and the run said
only how the command exited.

## Decision

**One long-poll covers every job a worker holds.** `POST /worker/v1/watch`
takes the worker's jobs, each with the lease token it holds it by (up to
1,000), and answers the moment any of their runs is cancelled. It also
names any it doesn't hold any more, as `stale_lease` would say of one, and
both lists are empty after `wait` seconds. It reads the jobs in the same few
queries however many there are, and wakes on any of their cancels, as a
job's own watch does. A cluster's worker holds this one request open for
all its jobs, so a cancel reaches the cluster at once, as on the other
tiers, and still does when heartbeats fail. Heartbeats carry cancels too,
as before. `IKI_SLURM_MAX_JOBS` is at most 1,000, so one watch covers every
job. This is how a GitHub Actions runner hears of a cancelled job: one
long-poll for all its messages, not one per job.

**A newer worker reads an older server's answers.** A field the server
adds to what it answers workers is optional in the worker's protocol, so a
worker built against a newer server reads an older one's answer and does
without it. Without `log_offset`, a restarted cluster worker carries on
from the last chunk it knows the server has, and the server's answer to
the next chunk says to skip any it already had. A server without the watch
of several jobs refuses it; the worker leaves cancels to heartbeats, and
asks again every few minutes.

**A restarted worker keeps why its job said it failed.** Each mark in
`log.json` holds the last `::error` line before it, so a worker that
restarts reports the line the command printed before the restart if the
command then fails.

## Consequences

- A cancelled run's Slurm job is `scancel`led at once, whatever its
  heartbeats are doing.
- One more request held open per cluster worker, not one per job.
- A worker that holds 1,000 jobs at once can't hold more. A cluster that
  needs more runs a second login node's worker.
- The server's answers can grow without breaking the workers already out
  there; a field a worker can't do without needs a new route, or the worker
  to say what it needs.

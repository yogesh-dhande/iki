# 0045. Compute admins set run quotas per organization, team or project

- Status: Accepted
- Date: 2026-11-11
- Refines: [0043](0043-workers-lease-jobs.md)

## Context

Fair share (ADR 0043) stops one project's batch from starving the rest,
but it doesn't cap anything. Labs share GPUs and licensed software, and
pay for cloud machines. Their Compute admins need to cap how much a team or
a project runs at once, pause one while a problem is looked into, and stop
one person's script from queueing a hundred thousand runs. HPC schedulers
(Slurm's QOS limits) and GitHub Actions' concurrency limits are what they
know.

## Decision

**A quota** covers the organization, a team or a project, on one tier or
all of them. There's at most one per scope and tier. It sets either limit,
or both:

- `max_running`: jobs held by workers at once. Jobs in a scope at its limit
  wait, even when a worker is free, and the run says why
  (`waiting: "The project is at its limit of 4 running jobs."`). Setting it
  to 0 pauses the scope.
- `max_queued`: runs waiting to start. Launching more is refused, with a
  message saying so.

Every quota that covers a job applies, so a project's limit and its team's
both hold. Changing a limit affects only what starts next. Running jobs
keep running, and waiting runs keep waiting.

**Limits are exact.** While a tier has quotas, leasing on it takes an
advisory lock, so two workers can't both take the last slot. Tiers with no
quotas take no lock.

Only Compute admins manage quotas, under `/api/v1/compute/quotas`, and
every change is audited with its before and after values.

## Consequences

- With no quotas, nothing changes: fair share alone decides the order.
- Leases on a tier with quotas are serialized. They take milliseconds, so
  this matters only at thousands of workers per tier.
- Spending limits are a separate matter: a quota counts jobs, not money.
  What runs cost is recorded and reported per [ADR 0070](0070-runs-show-what-they-cost.md);
  budgets build on it ([ADR 0071](0071-budgets-warn-and-can-stop-spending.md)).

## Amendment of 2026-11-27 — a lease waits for others only to count and claim

**Limits stay exact, and only counting waits for them.** A lease claims its job in a
transaction of its own. It takes the tier's quota lock (and a Slurm cluster's, ADR
[0133](0133-a-slurm-cluster-runs-container-tier-jobs-its-templates-place.md)), counts what's
running and picks the job. It also picks the queued runs of the job's batch that the same
command can run with it (ADR [0055](0055-batches-and-packing.md)), marks them all leased and
commits. A pack is claimed whole, so no other lease can take part of it or skip over it
while it's being read. Only then does the lease read what the runs read: their pinned
records, references and files, the whole pack's at once. Then it starts them. So leases on
a tier with quotas wait for one another for a few queries each, however much a pack of a
hundred runs reads. Before, each lease held the lock while it read everything, and a pack's
lease made some two thousand queries under it.

If the job can't start after all, the claim is undone, its pack's included, and the attempt
ends or waits as it would have while queued. This happens when its launcher lost their role,
a record it reads left its project, or a file it reads is held or still being scanned. Its
slot is free again at once. A packed run that can't start for the same reasons is taken out
of the pack and ends or waits the same way. If leasing breaks partway, the job and its pack
wait to be leased again. If the server process dies between claiming and starting, the job
is recovered as a lost worker's is, once its lease runs out.

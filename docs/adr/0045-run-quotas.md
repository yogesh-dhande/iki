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
- Spending limits are a separate matter (budgets, later): a quota counts
  jobs, not money.

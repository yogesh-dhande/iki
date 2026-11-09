# 0009. Four compute tiers, one job per worker, sized compute templates

- Status: Accepted
- Date: 2026-11-09

## Context

Scientific actions range from millisecond calculations to multi-hour GPU
jobs that need terabyte-scale reference databases. Some users want to run
on their own machines. Costs differ by orders of magnitude between these
cases, and every run needs the same provenance and permissions.

## Decision

Actions run on one of four tiers:

- **client**: the user's machine. The worker leases only its own user's
  jobs.
- **backend**: isolated worker pods. They hold only a scoped worker token,
  with no database credentials and a default-deny NetworkPolicy.
- **container**: plugin images on Kubernetes, as ephemeral `k8s_job`s or
  persistent `k8s_deployment` pools.
- **vm**: AWS EC2 (GCE later), ephemeral or persistent, managed by Iki,
  with spot instances and on-demand fallback.

Each worker runs **one job at a time**. Plugins declare portable
**Environments** (resources, image, volumes). Admins define
**ComputeTemplates** per deployment. Each environment binds to several
templates in different sizes, with a priority order. The scheduler picks
the smallest template that satisfies the job's resources.

Jobs come from the Postgres queue (ADR 0005). Leases have a time limit and
are renewed by heartbeats. Each lease carries a **fencing token**: a number
that increases with every new lease, so the server rejects a `complete`
call carrying an older token from a worker that already lost its lease.
Scheduling applies priorities, per-team and per-project fair share, quotas
and per-template concurrency caps. Soft budgets warn. Hard budgets block
the container and VM tiers. Every job records resource-seconds times
template price.

## Consequences

- Positive: one action model covers laptops through GPU fleets.
- Positive: one job per worker gives clean isolation and exact cost
  accounting.
- Positive: fencing makes retries after a lost worker safe.
- Positive: admins control spend through templates, quotas, budgets and
  roles (ADR 0003).
- Negative: four provisioning paths to build, secure and test.
- Negative: per-job overhead for very small jobs. `batchable: N` reduces it
  by packing N entities into one job.
- Negative: the VM tier supports AWS only at first.

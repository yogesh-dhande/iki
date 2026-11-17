# 0108. A lease weighs each project's next jobs, not the whole queue

- Status: Accepted
- Date: 2026-11-17
- Refines: [0043](0043-workers-lease-jobs.md)

## Context

ADR 0043 orders what a worker may lease by project first: projects with
fewer jobs running go first, so one project's batch doesn't starve the
rest. After that come priority, then age. The lease ranked every queued
job it could run that way, counting each candidate's project's running
jobs as it went. That count read the project's whole job history.

A load test showed the cost. It used 5,000 queued jobs across 100
projects, behind 20,000 finished ones. The query read 1.26 million
buffers and took 761 ms, nearly all of the 960 ms a lease took. The cost
grows with the queue times its history. A deployment that has run a
million jobs and has 100,000 queued would take minutes per lease, far
past the lease's 30-second wait.

## Decision

**A lease shortlists the best few jobs of each project with queued work,
then ranks only those.**

- **Finding the projects.** The lease skip-scans a new index,
  `iki_job_queued_by_project` (tier, project, priority descending, queued
  at, ID, over queued jobs only). Each step finds the next project up in
  one index lookup.
- **Taking each project's next jobs.** A lateral join takes each project's
  first 8 jobs this worker can run, in priority and age order. It applies
  every rule the lease always had (resources, plugin code, environment,
  checkpoints, a client worker's owner and projects, quotas) before it
  limits.
- **Ranking them.** Running counts come from another new index,
  `iki_job_held`, over jobs that are leased or running. It holds about as
  many rows as there are workers, however long the history. The best 64
  candidates are ranked by running count, then priority, then age.
- **Locking one.** The lease locks the first of the 64 that no other lease
  holds (`FOR UPDATE SKIP LOCKED`), checking the same rules again.

The order is the same as before. The best job a worker can run in the
project with the fewest jobs running is always among a project's first 8,
so it is always shortlisted. Workers leasing at once all see the same
shortlist, and each takes the first that none of the others has locked.

The lease also turns off Postgres's JIT in its own transaction. Postgres
estimates a lateral join with limits as costly enough to compile, and
compiling took 46 ms against a 6.6 ms plan.

At the same size, the query now takes 6.6 ms and the whole lease 60 ms.
Its cost grows with the number of projects that have queued work. It no
longer grows with the queue's depth or its history.

**`make check-scale-queue` measures it** and runs nightly in the Scale
workflow, or alone on demand. It works like ADR 0103's grid test:

- One run is launched through Iki, then copied 1.1 million times, as a
  restore loads a database: 100,000 queued across 100 projects, behind
  1,000,000 finished.
- Workers, each in its own thread, lease and complete jobs over
  `/worker/v1` as fast as each can, against the real server with four
  processes. No job may be leased twice, and no project may get more than
  a round of workers ahead of another.
- First, four workers (one per server process) do 1,000 jobs. Each lease
  and completion must answer within 100 ms at the median and 500 ms at the
  99th percentile. This is the cost of a lease, and it must not grow with
  the queue.
- Then 32 workers do 5,000 jobs. That saturates the server: each request
  waits behind the others in flight (Little's law), so how long it waits
  says how big the machine is, not how the queue scales. This phase holds
  the rate to at least 40 jobs a second on CI's runner (4 vCPUs, with the
  database and the workers on it too), and the 99th percentile to 2 s.

It also found completions locking more than they meant to.
`select_for_update()` with `select_related()` locks every row in the join:
each completion locked its run's project, team and launcher too. So a
person's runs completed one at a time: 460 ms each, with 32 workers.
Completing, and five other places, now name the rows they lock, and a test
refuses any lock with related rows that doesn't.

## Consequences

- The lease costs one index lookup per project with queued work on the
  tier, at least two per lease. That is hundreds of microseconds for a
  hundred projects, and milliseconds for thousands.
- If a project's head holds many jobs this worker can't run (a GPU batch,
  with a CPU worker asking), the lateral join reads past them to that
  project's first jobs the worker can run. That costs as many index
  entries as there are such jobs ahead, in that project only. Indexing
  resources and environments would avoid it, but they vary too much to
  index usefully.
- With more than 64 leases at the same instant, a worker may find every
  shortlisted job locked. It then waits for the next job to be queued,
  or its next long poll, as an idle worker does. Each completed lease
  frees a place on the shortlist.
- Building the two indexes concurrently keeps workers leasing while a
  deployment migrates.

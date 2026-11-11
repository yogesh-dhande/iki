# 0043. Workers pull jobs over HTTPS, with fenced leases

- Status: Accepted
- Date: 2026-11-11
- Refines: [0042](0042-action-runs-and-jobs.md)

## Context

Jobs run on machines Iki doesn't control. Some are pods in the cluster,
some are VMs in a lab's cloud account, and later some are scientists'
laptops. These machines are often behind NAT or a corporate proxy, so the
server can't reach in. Spot machines get taken back mid-job. Networks
drop. Pharma IT expects each machine to be identifiable, revocable on its
own, and to send nothing but outbound HTTPS.

## Decision

**Workers pull.** `iki-worker` calls `/worker/v1` over HTTPS. Nothing
connects to it. `POST /worker/v1/lease` waits up to 30 seconds for a job,
and a Postgres `NOTIFY` wakes it when one is queued on its tier, so an
idle worker makes about two requests a minute. This is the shape of
GitHub's self-hosted runners and GitLab's runners.

**Two kinds of token, as GitHub runners have:**

- A Compute admin makes a **registration token** (`iki_wreg_…`) for a tier,
  optionally with an expiry, and puts it in the tier's secret store.
- Each worker registers once with it and gets **its own token**
  (`iki_wkr_…`).

Only SHA-256 hashes of either are stored, and each value is shown once.
Revoking a registration token stops new registrations. Workers that already
registered keep working, unless the admin also revokes them, for a token
that leaked. Revoking one worker ends its token at once and interrupts its
jobs. Client-tier workers will sign in as their person instead (commit 30).

**A worker leases the best job it can run:**

- on its tier, with resources that fit its reported capacity
- from projects with fewer jobs running first, so one project's batch
  doesn't starve the rest
- then by priority, then oldest first

Rows are claimed with `FOR UPDATE SKIP LOCKED`, so workers never wait on
each other. The lease returns the run input (`run-input/v1`): the command,
params, and each input entity's values at the revision the run recorded.
The worker writes this to `input.json`.

**Leases are fenced.** Each lease lasts 60 seconds, and heartbeats every 20
seconds renew it. Every lease has a new token, and every report names it.
A report with a token that isn't current answers 409 `stale_lease`, so a
worker that was cut off can't overwrite the attempt that replaced it.
Heartbeats move the job through staging, running and uploading, never
backwards. They also say when the run has been cancelled.

**When an attempt ends badly**, the run is retried as a new job if the
action's `retries.on` lists the reason and attempts remain. Retries wait 10
seconds times the attempt number. Otherwise the run ends `failed` or
`timed_out`. A cancelled run always ends `cancelled`.

The task runner's housekeeping ends two kinds of attempt:

- those whose lease ran out (`worker_lost`)
- those running more than five minutes past their timeout, if the worker
  didn't stop them

**Logs** come in ordered chunks with byte offsets. A repeated chunk is
accepted once, and a gap answers 409 `log_gap` with where to resume. Each
job keeps up to 16 MiB; beyond that, bytes are counted but not kept.

## Consequences

- A worker needs only outbound HTTPS to the server, which suits locked-down
  networks.
- Losing a worker loses at most a minute before its job is retried.
- The queue lives in Postgres, as the task queue does. No broker to run.
- Fair share is per project and best effort. Hard quotas come later.

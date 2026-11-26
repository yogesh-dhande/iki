# 0234. A run waits for its files' scans

- Status: Accepted
- Date: 2026-11-25
- Refines: [0216](0216-files-wait-for-a-scan.md), [0043](0043-workers-lease-jobs.md), [0080](0080-triggers-run-actions-when-entities-change.md)

## Context

A run reads only usable files ([ADR 0216](0216-files-wait-for-a-scan.md)).
Uploads and registrations become files only once their scan decides, so new
files are never pending. Files from before scanning are pending until the
backfill reaches them. With petabytes of instrument data that can take days.

Until now a launch that met such a file was refused. A person saw "being
scanned, try again shortly". Nothing tried again for a trigger, a schedule
or a workflow step. The firing or step failed for good, and the record was
never imported. A job queued before the upgrade was put back 30 seconds at
each lease. Its queue time moved and its history said nothing.

GitHub Actions queues a job that has no runner and starts it when one
appears. A GxP record must say why a run started late and what let it
start.

## Decision

A run that reads a file whose scan isn't done waits for it. It is queued,
not refused. This holds however it starts: by a person, in a batch,
reproduced, by a trigger, by a schedule, by a `from_project` input or as a
workflow step. A file param may name such a file. A record's file field
still may not.

- The attempt records when its wait began (`scan_wait_started_at`), the
  files it waits for (`scan_wait_files`) and when the wait ended
  (`scan_wait_ended_at`). Its queue time doesn't change.
- No worker leases a waiting attempt, none packs it into a batch's job, and
  no machine starts for it.
- Each new scan decision checks the attempts waiting for those bytes. This
  only happens when some attempt is waiting. A sweep every 15 seconds checks
  the rest, in case a decision's task was lost or the wait began after it.
- When every file is usable, the wait ends and the run starts as any other.
  When one is quarantined or can't be scanned completely, the wait ends and
  the run fails with `file_quarantined` or `file_not_scanned`, naming the
  file. A held file ends the run at once, even if others are still
  pending. A run awaiting approval ends its wait on a clean decision, and a
  held one fails it when it's approved.
- A scanner outage leaves the file pending, as ADR 0216's retries expect.
  The run keeps waiting, and a person can cancel it.
- A held file still refuses the launch, saying which file and why.
- A run doesn't reuse an identical run's results while a file it reads is
  pending. It runs once the scan is done.
- The audit log records `run.waiting_for_scan` with the files, and
  `run.scan_wait_ended` with the outcome, how long it waited, and each
  file's decision: scanner, signatures, time, finding and release.
- The run page says it's waiting for a malware scan and names the files
  the reader can read. Each attempt says when it waited and for how long.
  The API returns `waiting_for_scan` on the run and `scan_wait` on each
  attempt.

## Consequences

- Positive: triggers, schedules and workflows keep working while a backfill
  runs. A record's import happens once its file is clean, with no one
  re-firing it. The run's history shows the wait and the decisions that
  ended it. Waiting holds no worker or machine.
- Positive: the 30-second re-leasing is gone, so queue times and wait
  metrics stay true.
- Negative: a run can wait a long time through a long outage or a deep
  backfill. It counts against the project's queued-run quota while it
  waits.
- Negative: an attempt has three more columns, and each scan decision makes
  one cheap query while any run is waiting.

## Alternatives considered

- **Keep refusing, and have each caller retry.** Every caller would need
  its own retry: triggers, schedules, workflows and agents. Each would have
  its own limits and history, and a person would still be told to try again.
- **A new run or attempt status.** A waiting run is queued, and everything
  that handles queued runs already covers it, such as cancelling, plugin
  detach, quotas and exports. A new status would have to be added to each
  of them. The reason it hasn't started goes in `waiting`, as for quotas
  and reference data.
- **Keep a run waiting while its file is quarantined, in case it's
  released.** A release is a person's judgement and may never come. The run
  fails with the reason, and someone runs it again once the file is
  released.

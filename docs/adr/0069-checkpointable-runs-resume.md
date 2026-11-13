# 0069. Checkpointable runs resume

- Status: Accepted
- Date: 2026-11-12
- Refines: [0010](0010-single-rust-worker.md),
  [0043](0043-workers-lease-jobs.md),
  [0068](0068-runs-keep-artifacts-for-a-while.md)

## Context

A structure prediction or a molecular dynamics run can take hours on a GPU
machine. Machines go away: a spot VM is reclaimed, a node is drained, a
worker is upgraded. The queue retries an attempt that's interrupted or whose
worker is lost (ADR 0043), but the retry starts again from nothing, paying
for the same hours twice and sometimes never finishing on spot capacity.

Actions could already say `checkpointable: true`, and commands were promised
a `$IKI_CHECKPOINT_DIR`, but nothing gave them one or kept what they saved.

## Decision

**A checkpointable run's worker saves the command's checkpoint directory, and
a later attempt starts from the newest one.**

- The command gets `IKI_CHECKPOINT_DIR`, a directory of the job's own. It
  saves its progress there, and on start resumes from what's there.
- The worker packs the directory into an uncompressed tar file of its regular
  files and directories, in a set order, without owners or times. Links
  aren't saved. It's uncompressed because saving must be quick when a machine
  is going away, and model state rarely compresses well.
- It saves **every `IKI_RUNS_CHECKPOINT_INTERVAL`** (10 minutes unless the
  deployment says, and at least one) while the command runs, if anything in
  the directory changed. It saves **once more** when the attempt ends in a way
  the run is retried for (interrupted, timed out, or failed, as the action's
  `retries.on` says), once the command has had its 10 seconds after SIGTERM
  to save what it can. On a run's last attempt it doesn't save: nothing would
  resume from it.
- Each save is an upload of its own kind, made under the attempt's **lease**
  like a result file (ADR 0044): a worker whose lease is gone can't replace
  the checkpoint of an attempt that replaced it. The server checks its bytes
  against their SHA-256 and keeps them under a key of their own. Only the
  run's worker sends its parts: people's upload API sees only their files,
  though a run's checkpoints and artifacts are made in its launcher's name.
- The run's checkpoint is the **newest one whose bytes were checked**. A later
  attempt is leased only once none is still being checked, so it never
  resumes from an older one than it could. Its run input names it
  (`job.checkpoint.saved`), and the worker downloads it, checks its SHA-256,
  and unpacks it (regular files and directories only, none outside the
  directory) before the command starts.
- The worker then tells the server, and the attempt **records the checkpoint
  it resumed from**: which attempt saved it, when, its size and SHA-256. The
  run's page shows it, beside what each attempt saved. A checkpoint that can't
  be restored is said in the attempt's log, and the command starts afresh: the
  run is no worse off than without it, and the record says so.
- A checkpoint's bytes are **deleted** once a newer one is checked, and all of
  a run's once it ends, however it ends. `iki-server maintain` deletes any a
  task missed. Their records stay.
- Batchable actions can't be checkpointable (`batch-checkpoints`): one job
  runs several runs, so its progress wouldn't be any one run's.
- `iki-worker run --local --checkpoint-dir DIR` gives the command `DIR`, kept
  between runs, so a plugin's author can stop it and see it resume.
- Workers get 300 seconds to stop (the Helm chart's backend workers, and the
  container tier's pods), long enough to save a checkpoint of a few GiB.

## Consequences

- An interrupted hours-long run costs only the minutes since its last save.
  Spot capacity becomes usable for long runs.
- What a run resumed from is part of its record, for anyone reviewing how a
  result was made: a resumed run's result rests on two attempts, and says so.
- Saving a large directory every 10 minutes is real traffic. Commands should
  keep only what they need to resume in it, and deployments can save less
  often.
- A checkpoint is only as consistent as the command makes it: a file that
  changes size while it's packed spoils that save, which is left for the
  next. Commands write each file under a temporary name and rename it into
  place.
- A checkpoint is as confidential as the run's inputs: it's kept in the same
  store, under the run's project, and only the run's later attempts read it.
- Spot interruption notices stop the worker as SIGTERM does, and so save a
  checkpoint the same way: the VM tier watches for them (ADR 0074).

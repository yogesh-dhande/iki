# 0156. A Slurm job's log is masked where it's written, and resumed where the server's copy ends

- Status: Accepted
- Date: 2026-11-19
- Refines: [0053](0053-runs-reach-the-internet-through-an-egress-proxy.md),
  [0054](0054-run-secrets.md),
  [0133](0133-a-slurm-cluster-runs-container-tier-jobs-its-templates-place.md)

## Context

A worker hides a job's secrets in what its command prints (ADR 0054). It
does so as it reads the output, with the secrets the server sent for the
job.

A Slurm job's command runs on a compute node, and Slurm writes what it
prints to the job's log file on the shared file system. The login node's
worker reads that file as it grows (ADR 0133). The secrets reach the node
in `secrets.json`, which the job reads once and deletes, and the worker
leaves them out of `input.json`. So a worker that restarts while its job
runs, as ADR 0133 allows, no longer has them: it can't hide them in what
it reads after the restart. And it read the file again from the start, so
it sent the server text it had already sent.

The attempt's own credentials need hiding too. The egress proxy takes the
attempt's job ID and lease token as its user name and password (ADR 0053).
A library that prints its proxy settings puts them in the log, where any of
the project's readers sees them, and they work from any pod that reaches
the proxy while the attempt runs.

And a worker that restarts asks the server once whether each job's lease
is still its own. It took any failure of that one heartbeat to mean the
lease was gone, and cancelled the job: a server restarting at the same
time, or briefly overloaded, cost a healthy job its work.

## Decision

**What's hidden is the job's secrets and the attempt's credentials.** For
every job, the worker hides the values of the secrets its action declares,
the attempt's lease token, and the password in its proxy's address, as
`***`. That covers the log, `::error` and `::progress` lines, on every tier.
Setup builds already hid their lease token.

**A Slurm job's output is hidden on its node, before it's written.**
`iki-worker slurm-step` no longer becomes Apptainer. It stays its parent,
reads what it prints (out and error, in one stream, in order), and writes
it to the job's log with what's hidden as `***`. `secrets.json` carries the
values to hide with the variables. The step passes on the signals Slurm
sends (`SIGTERM`, `SIGINT`, `SIGHUP`, `SIGQUIT`, `SIGUSR1`, `SIGUSR2`), and
exits as Apptainer did, by the same signal or with the same status, so
Slurm records the command's end as before. The log file on the shared file
system never holds the secrets, so a worker reading it after a restart
needs none of them. The worker still hides what it can as it reads the
file: the attempt's credentials, from the job's input.

**A restarted worker carries on with the log where the server's copy
ends.** Each chunk the worker sends ends where a line does. Before sending
one, the worker notes in the job's directory (`log.json`) what the server
has, and what it will have once the chunk arrives: the bytes of the log,
and how far into the file they reach. A heartbeat's answer says how much
of the job's log the server has (`log_offset`). Starting again, the worker
picks whichever of its two notes matches it, and reads the file from
there. A note that matches neither (a machine that lost its last write)
starts from the nearer one before it, and the server's answer to the first
chunk says to skip what it already has, as for any repeated chunk.

**Only the server lets go of a lease.** A restarted worker cancels a job
only when the server says the lease isn't its own any more (`stale_lease`),
or no longer takes the worker's token. A transport failure, a 429 or a 5xx
says nothing of the lease: the worker asks again, backing off up to 30
seconds, for as long as it takes, and the job runs on meanwhile. Stopped
before the server answers, the worker leaves the job running, to pick up
when it next starts. Any other refusal is noted, and the job is followed;
its heartbeats then ask again.

## Consequences

- A Slurm job's log on the cluster's file system and on the run's page
  holds neither its secrets nor its proxy credentials, through any number
  of worker restarts, and holds each line once.
- A server that restarts while its workers do, as in an upgrade, loses no
  Slurm job: its leases last ten minutes, and its workers keep asking.
- The step is one more process on the job's node, holding the values to
  hide in memory while the command runs, as the command does itself.
- Masking is still a safeguard, not a guarantee (ADR 0054): a command that
  encodes a secret before printing it, or writes it into the log file
  itself, isn't masked.
- `log.json` is written before each chunk, at most about once a second for
  each job that prints.

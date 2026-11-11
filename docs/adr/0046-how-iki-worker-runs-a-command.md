# 0046. How iki-worker runs a command

- Status: Accepted
- Date: 2026-11-10
- Refines: [0010](0010-single-rust-worker.md), [0043](0043-workers-lease-jobs.md)

## Context

`iki-worker` runs plugin code that Iki didn't write, on machines that hold
a worker token and sometimes other jobs' data. A command can hang, ignore
signals, start children of its own, print gigabytes, or never write its
results. People cancel runs and expect them to stop. Spot machines and
Kubernetes stop the worker with SIGTERM. Pharma IT puts TLS-intercepting
proxies between the worker and the server and installs their own CAs.

## Decision

**Configuration comes from the environment**, as for GitHub's and GitLab's
runners: `IKI_URL`, `IKI_REGISTRATION_TOKEN`, `IKI_PLUGINS_DIR` and what
one job may use. `IKI_URL` must be `https://`; plain `http://` is accepted
only for localhost, so a token is never sent in the clear. Certificates
are checked against the operating system's trust store (with
`SSL_CERT_FILE` honoured), never a bundled list, so a corporate CA works
once IT installs it. `HTTPS_PROXY` and `NO_PROXY` are honoured, and
redirects aren't followed.

**Tokens:** a worker registers with the registration token. With
`IKI_WORKER_STATE`, it keeps its own token in that file (mode 0600) and
reuses it across restarts. Without it, the token lives as long as the
process, and the worker deregisters when it stops.

**Each job gets a private directory** (mode 0700), removed when the job
ends: `input.json`, `inputs/` with the input files, `output.json` for the
command to write, `out/` for files, and its own `TMPDIR` and `HOME`. The command runs
from its plugin's directory with an environment that holds only `PATH`,
locale and certificate settings, those directories (`IKI_INPUT`,
`IKI_OUTPUT`, `IKI_OUT_DIR`), and the run's ID and attempt. For an action
that declares hosts, it also holds `HTTPS_PROXY`, the egress proxy with the
attempt's credentials (ADR 0053), and the secrets it declares (ADR 0054).
Nothing of the worker's own, so the command never sees its token.

**The command runs in a process group of its own.** Stopping it sends
SIGTERM to the whole group, then SIGKILL 10 seconds later. When it exits,
anything it left running in its group is killed. It's stopped when:

- the run is cancelled. While a job runs, the worker holds
  `POST /worker/v1/jobs/{id}/watch`, which answers the moment someone
  cancels, through the same `NOTIFY` as leasing. A cancel takes effect in
  about a second, not at the next heartbeat. The attempt ends `cancelled`.
- it runs past its timeout. The attempt ends `timed_out`.
- the worker gets SIGTERM or SIGINT. The attempt ends `interrupted`, so it
  can be retried elsewhere, and the worker exits.
- the lease went to another worker. Nothing is reported.

**Output** from stdout and stderr goes to the job's log in chunks of up to
256 KiB, about once a second. A chunk is sent again unchanged until the
server has it, so a retry is never an overlap. While the server can't be
reached, up to 16 MiB waits, as much as the server keeps, and anything
past that is dropped with a note in the log. A line like
`::progress 0.4 Folding chain A` reports progress, as GitHub Actions'
`::` commands do. A command that fails can say why with a line like
`::error Record 3 has no bases`: the last one is the run's error, with how
it exited (ADR 0052).

**Files** go through object storage, never the API: the server hands out
a short-lived link per file. Before the command starts, the worker
downloads each file the inputs hold into `inputs/`, under a name made safe
for the file system, and checks its size and SHA-256; a mismatch fails the
attempt. Afterwards, each `{"$file": "…"}` in `output.json` is uploaded
the way the web app uploads, in parts, and replaced with the new file's
ID. Its path must resolve, symlinks and all, to a file inside `out/`, so a
command can't upload what it shouldn't, such as the worker's state.

**Ending:** if the command exits 0, the worker reads `output.json` (a JSON
object of at most 2.5 MB), uploads its files and reports it. Otherwise, or if `output.json`
is missing or unreadable, the attempt fails with a message that says why,
such as "The command exited with status 3." Reports are retried with
backoff for five minutes, while heartbeats keep the lease.

**Locally:** `iki-worker run --local input.json` runs a command with no
server, for plugin authors and `iki plugin test`. Only what touches the
server differs: input files are copied from paths input.json gives (and
checked against it), `$file`s are checked as for an upload but described
rather than uploaded, what the command prints goes to stderr, and its
output to stdout. The rest is the same code, so a fixture that passes
locally runs the same way on a worker.

The worker logs one `logfmt` line per event to stderr, readable in a
terminal and parsed as-is by Loki and Datadog.

## Consequences

- A cancelled or timed-out command and everything it started stop, so a
  run can't outlive its run record.
- A plugin can't read the worker's token or another job's files through
  its environment or the job directory. On a shared host, isolation beyond
  this (users, cgroups, containers) is per tier and comes with it.
- One job runs at a time per worker. More capacity means more workers.
- `watch` adds a long-poll per running job. Like leasing, it's cheap while
  nothing happens.

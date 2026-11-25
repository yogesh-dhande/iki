# iki-worker

The agent that runs Iki actions: it registers with the server, leases jobs
over HTTPS, runs each one's command, streams what it prints, and reports
how it ended. One binary for every compute tier
([ADR 0010](../docs/adr/0010-single-rust-worker.md)). It only makes outbound
HTTPS calls, so it works behind NAT and corporate proxies
([ADR 0043](../docs/adr/0043-workers-lease-jobs.md)).

- `crates/iki-worker`: the binary.
- `crates/iki-protocol`: the wire types, generated from the server's
  schemas and API (`make protocol`).

## On a team's machine (the self-hosted tier)

Self-hosted actions run on machines a team looks after: the PC beside an
instrument, a workstation with licensed desktop software, a GPU box in the
lab ([ADR 0112](../docs/adr/0112-self-hosted-workers-belong-to-teams.md)).
A Compute admin registers each one, as for GitHub's self-hosted runners.
Under **Compute → Workers** they make a self-hosted registration token for
the teams whose runs it takes (perhaps only some of their projects), with
its labels, such as `chromeleon` or `lab-3`. On the machine:

```sh
iki-worker register --url https://iki.example.com --token iki_wreg_…
iki-worker start
```

`register` registers once and keeps the worker's own token in
`~/.config/iki/worker.json` (`%APPDATA%\iki\worker.json` on Windows, or
`IKI_WORKER_STATE`), readable only by this user, so `start` needs nothing
else. `--name` sets how admins tell it apart (the host name unless given);
`IKI_URL` and `IKI_REGISTRATION_TOKEN` work in place of `--url` and
`--token`. Registering again replaces the machine's earlier worker.

It runs only self-hosted runs in the projects it covers: every project of
its teams, or only those listed for a team. Admins see each machine's
labels, teams, projects, version and what it's running under Compute →
Workers. They change its labels and teams there, and revoke it. Narrowing
its teams or projects stops what it was running outside them, which queues
again for a machine that covers it. `iki-worker deregister` takes it off
and deletes its token.

The worker builds for Linux and macOS; Windows isn't supported yet.

`iki-worker login` and `logout` are gone: they signed a worker in as its
person, which self-hosted registration replaced.

It fetches each plugin's code from the server, at the commit the deployment
installs, and checks its SHA-256 before unpacking it
([ADR 0059](../docs/adr/0059-workers-fetch-plugin-code-and-build-environments.md)).
It then builds the plugin's environments with
[uv](https://docs.astral.sh/uv/) (or micromamba, for conda ones) from the
pinned, hashed files in the plugin, so install whichever your plugins need.
Everything goes in `~/.cache/iki/worker` (`IKI_WORKER_CACHE`), by commit and
digest, so the next start reuses it. When the deployment upgrades a
plugin, an idle worker notices within ten minutes and fetches the new
code.

It also keeps itself up to date
([ADR 0060](../docs/adr/0060-workers-update-themselves-to-signed-releases.md)).
When the server wants a newer release, the idle worker downloads it, checks
its Sigstore signature against Iki's release workflow (offline), and starts
the new version in its place. Set `IKI_WORKER_AUTO_UPDATE=0` if IT manages
the install. To check a download by hand:

```sh
iki-worker verify iki-worker-0.5.0-aarch64-apple-darwin.tar.gz \
  iki-worker-0.5.0-aarch64-apple-darwin.tar.gz.sigstore.json --version 0.5.0
```

## On a Slurm cluster's login node

A Slurm cluster runs container-tier actions that the deployment's Slurm
templates place on it
([ADR 0133](../docs/adr/0133-a-slurm-cluster-runs-container-tier-jobs-its-templates-place.md)).
The worker runs on the cluster's login node, registered with a
container-tier token for the cluster. With `IKI_SLURM_WORK_DIR` set to a
directory the compute nodes share, `iki-worker start` leases the cluster's
jobs, many at once. It stages each job there and submits it with `sbatch`.
On the job's node, `iki-worker slurm-step` runs the job's command in its
image under Apptainer, once cosign has checked who signed the image. The
worker follows each job in `squeue`, cancels it with `scancel`, and reads
how it ended and what it used from `sacct`. Restarting the worker leaves
its jobs running, and it picks them up again when it starts.
[Running Iki's jobs on a Slurm cluster](../docs/slurm.md) is the
administrators' runbook.

## Running it on a deployment

A Compute admin makes a registration token for a tier under Compute → Workers.
For an install set up from code, an operator can make one inside the
cluster instead; it prints only the token:

```sh
kubectl exec -n iki deploy/iki-web -- iki-server create_registration_token \
  --tier backend --label "Backend pool"
```

Then:

```sh
IKI_URL=https://iki.example.com \
IKI_REGISTRATION_TOKEN=iki_wreg_… \
IKI_PLUGINS_DIR=/opt/iki/plugins \
iki-worker start
```

| Variable                                   | What it's for                                                                                                                         |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `IKI_URL`                                  | The server. `https://` only; `http://` is accepted for localhost. After `register`, the one it registered with.                       |
| `IKI_REGISTRATION_TOKEN`                   | Registers the worker. Not needed again once `IKI_WORKER_STATE` holds a token.                                                         |
| `IKI_WORKER_STATE`                         | A file to keep the worker's own token in (mode 0600), so restarts reuse it. After `register`, `~/.config/iki/worker.json` unless set. |
| `IKI_PLUGINS_DIR`                          | Each plugin's code, a directory each (its `plugin.yaml` names it). All the worker runs. Unless set, it fetches the server's.          |
| `IKI_WORKER_CACHE`                         | Where it keeps the plugins it fetched and the environments it built. `~/.cache/iki/worker` unless set.                                |
| `IKI_WORK_DIR`                             | Where jobs run, a private directory each. A temporary directory unless set.                                                           |
| `IKI_WORKER_NAME`                          | How people tell it apart under Compute → Workers. The host name unless set.                                                           |
| `IKI_WORKER_CPU`, `IKI_WORKER_MEMORY`      | What one job may use, such as `4` and `16Gi`. Detected on Linux (cgroup-aware).                                                       |
| `IKI_WORKER_GPUS`, `IKI_WORKER_GPU_MEMORY` | GPUs one job may use.                                                                                                                 |
| `IKI_WORKER_SCRATCH_DISK`                  | Local disk one job may use.                                                                                                           |
| `HTTPS_PROXY`, `NO_PROXY`                  | A proxy to reach the server through.                                                                                                  |
| `SSL_CERT_FILE`                            | CA certificates, if the operating system's don't cover the server or the proxy.                                                       |
| `IKI_KEEP_JOB_DIRS`                        | `1` keeps each job's directory after it ends, to debug a command.                                                                     |
| `IKI_ALLOW_HTTP`                           | `1` allows a plain `http://` `IKI_URL` to any host. Development clusters only.                                                        |

The worker leases only runs launched with the plugin code it has
([ADR 0047](../docs/adr/0047-workers-run-only-the-code-a-run-was-launched-with.md)).
Its plugins come from `iki deploy build --workers`, which writes each
plugin's code at its commit, a `plugins.json` saying what it has, and a
script that builds its environments into the image. Without
`plugins.json`, each directory holding a `plugin.yaml` counts as a local
plugin: it runs what a development server launches, with the host's own
software.

### In a container

`ghcr.io/<owner>/iki-worker` ([`Dockerfile`](Dockerfile)) is the worker
with micromamba and no plugins, running as uid 10001. A deployment builds
its own image on top of it from the `--workers` output
([`deploy/worker.Dockerfile`](../deploy/worker.Dockerfile)), so its
plugins' code and environments are baked in, read-only, and a worker
installs nothing when it starts:

```sh
iki deploy build deployments/prod --output build/plugins --workers build/worker
docker build -f deploy/worker.Dockerfile \
  --build-arg IKI_WORKER_IMAGE=ghcr.io/<owner>/iki-worker@sha256:… build/worker
```

`scripts/ci/worker-image.sh` builds both from the test plugins and checks
the result.

On Kubernetes, the Iki chart runs a pool of them (`backendWorkers` in
[its values](../deploy/helm/iki/values.yaml)): non-root, with a read-only
root filesystem, no Kubernetes or cloud credentials, a scratch volume for
jobs, and egress only to the server and object storage.

### In a plugin's image (the container tier)

For container-tier runs, the chart's provisioner starts a pod with the
environment's own image ([ADR 0061](../docs/adr/0061-container-tier-runs-get-pods-of-their-own.md)).
An init container copies the worker in with `iki-worker install /iki/bin`.
The binary is static, so it runs in any Linux image. The pod then runs
`iki-worker start --once` with:

- `IKI_WORKER_ENVIRONMENT`: the environment it runs (`plugin.environment`, such as
  `iki/protein.structure-tools`).
  It fetches only that plugin's code and builds nothing, since the image is
  the environment.
- `IKI_WORKER_IMAGE`: the image, pinned by its digest. Each attempt records
  that digest.
- `IKI_REGISTRATION_TOKEN`: a token made for that pod alone (`iki_wlch_…`).
  It works once, for that environment's runs only.
- `IKI_WORKER_IDLE_EXIT`: how many seconds it waits for a job before it
  stops. That covers runs another pod took first, and runs that were
  cancelled.

A persistent template's pods (`k8s_deployment`) run `iki-worker start`
instead, one job after another, until the provisioner stops them
([ADR 0062](../docs/adr/0062-warm-pods-run-one-job-after-another-and-start-each-clean.md)).

### Between jobs

Runs from different projects can follow one another on a worker that runs
many: backend workers, and persistent container-tier pods. So that nothing
one leaves reaches the next:

- On Linux, the worker makes its memory unreadable to other processes,
  which run as the same user. Its token is in there.
- When the worker is its container's first process, every other process is
  a job's. After each job it stops any still running and reaps them, and
  empties `/dev/shm`.
- `IKI_WORKER_CLEAN_DIR`, a directory that holds only what jobs leave (the
  pod's scratch volume), is emptied after each job, whatever permissions a
  job gave what it left. A worker whose plugin code is in there fetches it
  again, checked against its SHA-256. The chart sets it for backend workers
  and the provisioner for persistent pods.

If it can't make the machine clean, the worker stops rather than take
another job. If that's because its plugin's code changed since it started
(an upgrade, or a local directory edited), it says so, deregisters and
exits cleanly, so a machine with the new code takes its place.

A local directory's code, on a development server, has no commit: a worker
that fetched it tells the server the SHA-256 of the bundle it got, and the
server leases it none of that plugin's jobs once another bundle is
installed. So a warm pod started before the directory was edited never runs
the old code; idle, it notices the change and stops the same way.

### Stopping

`iki-worker start --once` runs one job and exits. SIGTERM or SIGINT stops
the current job's command (SIGTERM, then SIGKILL after 10 seconds), reports
it `interrupted` so it can be retried elsewhere, and exits. Give pods a
termination grace period of at least a minute.

How a command is run, and what it can see, is in
[ADR 0046](../docs/adr/0046-how-iki-worker-runs-a-command.md); what it reads
and writes is in [`spec/`](../spec/README.md#what-a-command-reads).

## Running an action locally

`iki-worker run --local input.json` runs one action's command on this
machine with no server, through the same code as a leased job: the same
private directory, environment, process group, timeout and checks.
Plugin authors use it to try an action, and `iki plugin test` uses it for
fixtures.

```sh
cd my-plugin
iki-worker run --local tests/relax/input.json > output.json
```

The input is a run input ([`run-input/v1`](../spec/README.md#what-a-command-reads))
that may leave out `job`. Each of its `files` gives a `path` relative to
input.json. The worker fills in missing `name`, `media_type`, `size` and
`sha256` values and checks that the size and SHA-256 match the fixture.
It infers a missing media type from the filename using core's format
declarations. Standalone offline runs can specify a plugin-specific media
type in the fixture or result. On a deployment, Iki recognizes uploaded
results and artifacts using the installed format declarations.

What the command prints goes to stderr as it prints it. If it succeeds,
its output goes to stdout, and each `{"$file": …}` is checked as an
upload would be and described with its size, SHA-256 and path. The worker
removes the job directory when the run ends, including on failure or Ctrl-C.
Use `--keep` to inspect the result files afterward.

| Option              | What it's for                                                   |
| ------------------- | --------------------------------------------------------------- |
| `--plugin DIR`      | The plugin's directory. The current directory unless set.       |
| `--timeout SECONDS` | Stop it after this long. The input's `job.timeout`, or an hour. |
| `--keep`            | Keep the job directory and print its path to stderr.            |

It exits 0 if the command succeeded, 1 if it failed, 2 if the input can't
be run at all, and 130 if it was interrupted.

## Developing

`make check-rust` runs rustfmt, Clippy and the unit tests. The end-to-end
tests in `server/tests/test_worker_e2e.py` build the binary and run it
against the real server; `server/tests/test_worker_local.py` runs it with
`run --local`.

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

## On your own computer

To run the actions you launch on the client tier (your GPU, your desktop
software, your files), sign the worker in as yourself
([ADR 0058](../docs/adr/0058-workers-on-peoples-computers-sign-in-as-them.md)):

```sh
iki-worker login https://iki.example.com
iki-worker start
```

`login` opens your browser to confirm a code, through your company's
sign-in, and asks which projects the worker may run your actions in. Add
`--no-browser` over SSH and open the page it prints yourself. Its token
goes in `~/.config/iki/worker.json`, readable only by you, so `start`
needs no `IKI_URL` or registration token. It runs only your runs. You can
see and revoke it under _Your workers_ in your account, and
`iki-worker logout` signs it out.

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

## Running it on a deployment

A Compute admin makes a registration token for a tier in Compute settings.
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

| Variable                                   | What it's for                                                                                                                      |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `IKI_URL`                                  | The server. `https://` only; `http://` is accepted for localhost. After `login`, the one you signed in to.                         |
| `IKI_REGISTRATION_TOKEN`                   | Registers the worker. Not needed again once `IKI_WORKER_STATE` holds a token.                                                      |
| `IKI_WORKER_STATE`                         | A file to keep the worker's own token in (mode 0600), so restarts reuse it. After `login`, `~/.config/iki/worker.json` unless set. |
| `IKI_PLUGINS_DIR`                          | Each plugin's code, in a directory named for the plugin. All the worker runs. Unless set, it fetches the server's.                 |
| `IKI_WORKER_CACHE`                         | Where it keeps the plugins it fetched and the environments it built. `~/.cache/iki/worker` unless set.                             |
| `IKI_WORK_DIR`                             | Where jobs run, a private directory each. A temporary directory unless set.                                                        |
| `IKI_WORKER_NAME`                          | How people tell it apart in Compute settings. The host name unless set.                                                            |
| `IKI_WORKER_CPU`, `IKI_WORKER_MEMORY`      | What one job may use, such as `4` and `16Gi`. Detected on Linux (cgroup-aware).                                                    |
| `IKI_WORKER_GPUS`, `IKI_WORKER_GPU_MEMORY` | GPUs one job may use.                                                                                                              |
| `IKI_WORKER_SCRATCH_DISK`                  | Local disk one job may use.                                                                                                        |
| `HTTPS_PROXY`, `NO_PROXY`                  | A proxy to reach the server through.                                                                                               |
| `SSL_CERT_FILE`                            | CA certificates, if the operating system's don't cover the server or the proxy.                                                    |
| `IKI_KEEP_JOB_DIRS`                        | `1` keeps each job's directory after it ends, to debug a command.                                                                  |
| `IKI_ALLOW_HTTP`                           | `1` allows a plain `http://` `IKI_URL` to any host. Development clusters only.                                                     |

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
input.json; leave out `name`, `media_type`, `size` and `sha256` to have
them read from the file, or give them to check the fixture still matches.

What the command prints goes to stderr as it prints it. If it succeeds,
its output goes to stdout, and each `{"$file": …}` is checked as an
upload would be and described with its size, SHA-256 and path. The job's
directory is kept, and its path is printed.

| Option              | What it's for                                                   |
| ------------------- | --------------------------------------------------------------- |
| `--plugin DIR`      | The plugin's directory. The current directory unless set.       |
| `--timeout SECONDS` | Stop it after this long. The input's `job.timeout`, or an hour. |

It exits 0 if the command succeeded, 1 if it failed, 2 if the input can't
be run at all, and 130 if it was interrupted.

## Developing

`make check-rust` runs rustfmt, Clippy and the unit tests. The end-to-end
tests in `server/tests/test_worker_e2e.py` build the binary and run it
against the real server; `server/tests/test_worker_local.py` runs it with
`run --local`.

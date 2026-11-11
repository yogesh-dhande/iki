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

## Running it

A Compute admin makes a registration token for a tier in Compute settings.
Then:

```sh
IKI_URL=https://iki.example.com \
IKI_REGISTRATION_TOKEN=iki_wreg_… \
IKI_PLUGINS_DIR=/opt/iki/plugins \
iki-worker start
```

| Variable                                   | What it's for                                                                   |
| ------------------------------------------ | ------------------------------------------------------------------------------- |
| `IKI_URL`                                  | The server. `https://` only; `http://` is accepted for localhost.               |
| `IKI_REGISTRATION_TOKEN`                   | Registers the worker. Not needed again once `IKI_WORKER_STATE` holds a token.   |
| `IKI_WORKER_STATE`                         | A file to keep the worker's own token in (mode 0600), so restarts reuse it.     |
| `IKI_PLUGINS_DIR`                          | Each plugin's code, in a directory named for the plugin.                        |
| `IKI_WORK_DIR`                             | Where jobs run, a private directory each. A temporary directory unless set.     |
| `IKI_WORKER_NAME`                          | How people tell it apart in Compute settings. The host name unless set.         |
| `IKI_WORKER_CPU`, `IKI_WORKER_MEMORY`      | What one job may use, such as `4` and `16Gi`. Detected on Linux (cgroup-aware). |
| `IKI_WORKER_GPUS`, `IKI_WORKER_GPU_MEMORY` | GPUs one job may use.                                                           |
| `IKI_WORKER_SCRATCH_DISK`                  | Local disk one job may use.                                                     |
| `HTTPS_PROXY`, `NO_PROXY`                  | A proxy to reach the server through.                                            |
| `SSL_CERT_FILE`                            | CA certificates, if the operating system's don't cover the server or the proxy. |
| `IKI_KEEP_JOB_DIRS`                        | `1` keeps each job's directory after it ends, to debug a command.               |

`iki-worker start --once` runs one job and exits. SIGTERM or SIGINT stops
the current job's command (SIGTERM, then SIGKILL after 10 seconds), reports
it `interrupted` so it can be retried elsewhere, and exits. Give pods a
termination grace period of at least a minute.

How a command is run, and what it can see, is in
[ADR 0046](../docs/adr/0046-how-iki-worker-runs-a-command.md); what it reads
and writes is in [`spec/`](../spec/README.md#what-a-command-reads).

## Developing

`make check-rust` runs rustfmt, Clippy and the unit tests. The end-to-end
tests in `server/tests/test_worker_e2e.py` build the binary and run it
against the real server.

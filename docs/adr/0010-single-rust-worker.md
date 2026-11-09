# 0010. One Rust worker binary for all tiers

- Status: Accepted
- Date: 2026-11-09

## Context

Actions are language-agnostic commands that run on four tiers (ADR 0009):
user machines, backend pods, plugin container images and cloud VMs. The
component that runs them must work inside arbitrary plugin images, on
users' machines behind firewalls, and on generic VM images. It handles
tokens, secrets and large files, and it must survive spot interruptions.

## Decision

- A single Rust static binary, **`iki-worker`**, runs jobs on every tier.
  On the container tier an init container injects it and it becomes the
  plugin image's entrypoint. On the VM tier it runs on the host and starts
  the plugin image with containerd or Docker.
- Workers **pull** jobs by long-polling `lease` on `/worker/v1`. They never
  accept inbound connections.
- The run protocol is defined in `spec/`: the worker stages `input.json`
  and input files, the command writes `output.json` and files in `out/`,
  and reports progress with `::progress` lines. Protocol types in the
  `iki-protocol` crate are generated from `spec/`.
- **Checkpointing**: the worker syncs `$IKI_CHECKPOINT_DIR` periodically
  and on SIGTERM or a spot interruption notice. The job is requeued and the
  next attempt gets the checkpoint back.
- **Caching**: plugin bundles by SHA, images, built environments, and input
  files by content hash, within a disk budget with LRU eviction.
- **Network and secrets**: per-job secrets are injected for that job only,
  and egress is limited to the action's declared `network`.
- **Run artifacts** are uploaded with multipart, resumable transfers.
- **Signed auto-update**: the server pins the required version. Workers
  verify cosign signatures before updating, and update only when idle. The
  protocol supports version N-1 so updates never break running jobs.
- `iki-worker run --local` runs the same code path offline for plugin
  tests.

## Consequences

- Positive: one code path to harden and test, and identical behaviour in
  local tests and production.
- Positive: no runtime dependencies, so it drops into any image.
- Positive: pull-based leasing works behind NAT and needs no open ports.
- Negative: Rust is a third language in the monorepo (ADR 0019).
- Negative: protocol changes must be coordinated between the Python server
  and the Rust worker. Contract tests check the generated types against
  each other.
- Negative: the client tier needs release builds for several operating
  systems.

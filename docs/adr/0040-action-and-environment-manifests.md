# 0040. Actions and environments are manifests; what they install is pinned

- Status: Accepted
- Date: 2026-11-11
- Refines: [0006](0006-deploy-time-plugins-as-django-apps.md),
  [0039](0039-first-party-plugins-and-the-plugin-template.md)

## Context

Plugins need to run code: compute a sequence's properties on a small pod,
predict a structure on a GPU machine, import records from a public database.
Labs in regulated settings need to know, for every result, what code ran, on
what, with which inputs, and that running it again gives the same answer.
They also need reviewers to see, before a plugin is deployed, everything it
may write, reach and read.

## Decision

**An action is a manifest (`kind: Action`, under `actions/`).** It declares
its inputs (entity types, with the schema versions it works with), its
params, its effects (the types it creates and the fields it updates), where
it runs (a tier and an environment), its command, and what it may reach
(network hosts and secrets). The server will refuse any output that isn't in
its effects. The validator checks every type and field it names, so a
plugin can't be deployed with an action that writes a field that doesn't
exist.

**An environment is a manifest (`kind: Environment`, under
`environments/`).** It says what actions need wherever they run: tiers, the
smallest machine, software and data volumes. It names no cloud: each
deployment binds environments to its own compute templates (commit 20b).

**What an environment installs is pinned, and the validator refuses anything
else:**

- images by `@sha256:` digest, with the registry named
- Python packages with `==` and `--hash=sha256:`, in one file (no includes
  or editable installs)
- conda packages in an explicit lockfile, with their hashes

This is the same rule as the deployment lockfile (ADR 0034): what's reviewed
is what runs, and a run can be reproduced exactly.

**Safe defaults:**

- Runs reach no network unless they list hosts.
- Secrets are named, never valued; admins map them to their secret manager.
  Names the worker sets (`IKI_*`, `PATH`, `HOME`, …) are refused.
- Client-tier actions can't declare secrets, network or resources: Iki can't
  keep secrets from a person's own machine or limit it, so declaring them
  would promise what Iki can't keep.
- Runs whose machine went away are retried twice. Actions that reach the
  network aren't retried unless they say so, since a retry could repeat
  what they did elsewhere.
- Runs take at most 7 days. Longer work should checkpoint and split.

Durations are written `90s`, `30m`, `8h` or `365d`. Memory and disk use the
Kubernetes notation (`512Mi`, `64Gi`, `3Ti`), which the compute admins who
size them already use.

## Consequences

- `iki plugin validate` and the deployment build catch an action that reads
  a type its plugin doesn't depend on, writes a removed field or runs where
  its environment can't, before anything is deployed.
- Plugin authors must lock their requirements with hashes. The error names
  the command that does it: `uv pip compile --generate-hashes`.
- The run protocol (`input.json`, `output.json`) and the worker follow in
  commits 21 to 23, which check outputs against these effects.

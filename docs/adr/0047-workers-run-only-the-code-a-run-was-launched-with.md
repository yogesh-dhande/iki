# 0047. Workers run only the plugin code a run was launched with

- Status: Accepted
- Date: 2026-11-11
- Refines: [0043](0043-workers-lease-jobs.md), [0046](0046-how-iki-worker-runs-a-command.md)

## Context

A run records the plugin version and commit it was launched with
([ADR 0042](0042-action-runs-and-jobs.md)). Until now, any worker on the
run's tier could lease it, and ran whatever code it had for the plugin.
After an upgrade, a run launched just before it could run newer code on
one worker and older code on another. Its results would then say they came
from code that didn't make them. GxP audits ask exactly this: which
software, at which version, produced this record.

Environments raise the same question. A worker that hasn't built an action's
environment can't run it, and whether it did belongs in the run's record.

## Decision

**A worker says what it has.** When it registers, and each time it starts
again with a saved token (`PUT /worker/v1/me`), it lists each plugin:

- its `name` and the `sha` of the commit it was built from;
- each `environment` it has built, with that build's digest.

This works like GitHub's runner labels, which a runner sends each time it
connects, so a new image's capabilities apply at once. A change is
audited as `worker.updated`.

**The server leases a worker only runs it can run.** The run's plugin must
match by name and commit, and its environment must be one the worker
built. Each attempt records the build its worker used (`environment_build`),
for provenance. The worker checks again before it runs anything, and fails
an attempt it can't run as launched.

**Local directories are for development.** A plugin from a local
directory (`sha: null`) runs only runs whose plugin is local too, which
only a development server launches. A worker that lists no environments
for a plugin (`environments: null`) runs all of them with its host's own
software, as a plugin author's machine does. A deployment's workers get
their plugins and built environments from `iki deploy build`, from the
same lockfile as the server, so the commits match.

## Consequences

- A run is either run with the code and environment it was launched with,
  or it waits for a worker that has them. Nothing runs it on the wrong
  code.
- During a rolling upgrade, runs launched at the old commit are run by
  workers still on it. Once those are gone, those runs wait until they're
  cancelled.
- Compute admins see what each worker has (`GET /api/v1/compute/workers`),
  so they can tell why a run is waiting.

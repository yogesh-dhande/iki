# 0062. Warm pods run one job after another, and start each clean

- Status: Accepted
- Date: 2026-11-12
- Refines: [0048](0048-backend-workers-run-in-baked-isolated-pods.md),
  [0061](0061-container-tier-runs-get-pods-of-their-own.md)

## Context

A pod of its own for each run (ADR 0061) costs a pod start, and on a new
node an image pull, before the run begins. For a quick action that people run
often, that wait is most of the run. Compute admins want some pods kept
warm, ready for the next run.

A machine that runs one job after another also runs one project's job after
another's. Pharma and biotech sites keep projects apart: one program's data
must not reach another's runs. Iki's backend workers already reuse pods this
way. Until now, nothing stopped what one run left from reaching the next on
them:

- files outside the job's own directories
- processes still running
- the worker's token, which a run could read from the worker's memory: they
  run as the same user

## Decision

**`k8s_deployment` templates are persistent: their pods run one job after
another.** The spec already lets an action ask for `lifecycle: persistent`.
Container and vm actions get `ephemeral` by default, so a fresh machine is
what a plugin gets unless its author chooses otherwise. Only persistent
actions go to persistent pods, and an ephemeral action never does: each job
records the lifecycle its action asks for, and a lease checks it.

**The provisioner keeps them warm, and stops idle ones.** Persistent pods are
the provisioner's own Jobs, as in ADR 0061, without `--once`. They aren't a
Kubernetes Deployment. That way each pod keeps a token made for it alone,
and scaling down stops exactly the pod with nothing to do. A Deployment
would remove pods it picks itself, perhaps mid-run. GitHub's Actions Runner
Controller manages its runner pods the same way. Each tick, the provisioner:

- starts a pod for each queued job that no free pod can take. A free pod is
  one starting, or a persistent one with no job, that would lease it: same
  environment, plugin commit and lifecycle, and big enough.
- keeps each persistent template's `min_warm` pods for each environment
  bound to it, at the plugin commit runs launch with now
- stops a pod once it has had nothing to do for `idle_shutdown` (ten
  minutes unless the template says), when there are more than `min_warm`
- replaces a pod as soon as it's idle if its template, or how Iki starts
  run pods, has changed since it started: a new worker image, say. Each
  launch records a digest of its pod to tell.

A lease and a stop both lock the launch, so a pod is never stopped as it
takes a job. Every stop is in the audit log, with why.

**Every worker that runs many jobs starts each one clean.** Between jobs,
the worker:

- makes its memory unreadable to other processes (non-dumpable, on Linux),
  so a run can't read its token
- stops and reaps every process a job left running, when the worker is its
  container's first process: then every other process in the container is
  a job's
- empties `/dev/shm`
- empties `IKI_WORKER_CLEAN_DIR`, the pod's scratch volume, whatever
  permissions a job gave what it left
- fetches the plugin's code again in a persistent pod, checked against its
  SHA-256, so a run can't change the code the next one runs

The chart sets it for backend workers, and the provisioner for persistent
pods. A worker that can't make the machine clean stops, rather than take
another job.

## Consequences

- Persistent actions on a warm pod start in about the time it takes to
  lease. The cost is the warm pods, which compute admins size with
  `min_warm` and `max_concurrent`.
- Runs from other projects have run on a persistent pod before. It starts
  each run clean of what they left, but it's the same pod: same kernel, same
  node, same image. Actions whose data must never share a machine leave
  `lifecycle` out. The spec says so.
- Fetching plugin code for each job adds one small download. Bundles are
  typically kilobytes to a few megabytes.
- Pods with an outdated template are replaced all at once when idle, so
  warm capacity dips while new ones start.
- `min_warm` is kept whether or not anyone runs the environment's actions.
  Turning warm pods off and on by schedule comes with autoscaling by demand
  history, later.

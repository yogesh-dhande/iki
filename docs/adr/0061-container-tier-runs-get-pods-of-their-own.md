# 0061. Container-tier runs get pods of their own, started by a provisioner

- Status: Accepted
- Date: 2026-11-12
- Refines: [0009](0009-compute-tiers-and-templates.md),
  [0041](0041-compute-templates-and-bindings.md),
  [0048](0048-backend-workers-run-in-baked-isolated-pods.md)

## Context

Some actions need software that doesn't fit in a Python or conda
environment: a GPU tool with its own CUDA build, say, or a vendor's
container. Plugins give those environments an image, pinned by its digest
(ADR 0040). The container tier runs them in the deployment's Kubernetes
cluster, on the node pools and runtime classes its compute templates name
(ADR 0041).

Something has to start those pods. That something holds credentials to the
Kubernetes API, and the pods it starts run code Iki didn't write. In a GxP
setting, an auditor also asks what ran, from which image, and on what. And
the people waiting for a run want to know why it hasn't started.

## Decision

**A provisioner starts a Kubernetes Job for each queued run.** It runs as
`iki-server provision`, in a Deployment of its own. It's the only part of
Iki that talks to the Kubernetes API. One provisioner acts at a time (it
holds a Postgres advisory lock), so a rollout never starts pods twice. It
wakes when a container-tier job is queued, and otherwise every ten seconds.

- **Which template:** each job goes to the first template bound to its
  environment that fits it and has the lifecycle its action asks for. That's
  the lowest priority, then the smallest.
- **How many pods:** it starts one pod for each queued job that no pod is
  already starting for, up to the template's `max_concurrent`, and a few at
  a time.

This is how GitHub's Actions Runner Controller scales ephemeral runners.

**The pod runs the environment's image with Iki's worker.** An init
container from Iki's own signed `iki-worker` image copies the binary into a
volume (`iki-worker install`). The binary is static (musl), so it runs in
any image. The image's container mounts that volume read-only and runs
`iki-worker start --once`. In that mode, the worker:

- fetches only the plugin's code, as workers on people's computers do (ADR 0059)
- builds nothing: the image is the environment
- runs one job, then exits; the Job completes, and Kubernetes cleans it up

A plugin needs nothing of Iki's in its image.

**Each pod holds one credential, made for it alone.** The provisioner
records a launch: the template, environment, image, plugin commit and pod.
It also makes a token (`iki_wlch_…`) that only that pod's worker may
register with, once, within an hour. What the worker may run comes from the
launch, not from what it says:

- only that environment's runs
- in that image, whose digest each attempt records as its environment build
- with the template's capacity
- one job only

Each attempt also records its template. Every launch, and how it ended, is
in the audit log.

**The provisioner can do little else.** Run pods go in a namespace of their
own, which the chart refuses to make the same as Iki's. The provisioner's
Role there allows only:

- managing Jobs
- seeing pods
- creating Secrets

It may never read, change or delete a Secret. Each Job is created suspended,
then its token's Secret is created with the Job as its owner, then the Job
is started, so the two go together. The provisioner has nothing in Iki's
own namespace.

**Run pods meet PodSecurity `restricted`, with a default-deny network.**
Each pod:

- runs as a non-root user, with no capabilities, no privilege escalation and
  the runtime's default seccomp profile
- has a read-only root filesystem, with a size-limited scratch volume for
  writing
- has no Kubernetes API token
- asks for exactly the template's resources (requests equal limits)

The namespace's NetworkPolicy lets pods reach only DNS, the server, object
storage and the egress proxy, plus what `networkPolicy.egress.workers`
adds, and lets nothing reach them.

**What Kubernetes reports is reconciled with what Iki launched.**

- A launch whose Job finished, or was deleted, ends. Its worker is revoked,
  and its job is interrupted and retried under the action's policy.
- A Job with no live launch is deleted.
- A pod that can't pull its image ends after two minutes. If that happens
  three times in half an hour, the environment's queued runs fail. Their
  message names the image and says an administrator can check it, rather
  than leaving them waiting.
- A template whose pods keep failing to start waits longer each time, up to
  ten minutes.
- While a pod waits for a node, its queued runs say why ("Waiting for a
  machine: 0/3 nodes are available: 3 Insufficient nvidia.com/gpu.").

On a development server, local plugins have no commit, so their code is
bundled as `local`. Only the pods started for that plugin can download it.

## Consequences

- A deployment turns the tier on with `containerTier` in the chart: a
  namespace it created, labelled to enforce PodSecurity `restricted`, and
  templates in `compute.yaml`.
- A pod per run costs an image pull and a pod start. Warm pools of pods
  (`k8s_deployment`), with an autoscaler, come next (commit 31b).
- Checking image signatures at admission, so only signed images run in the
  runs namespace, comes after that (commit 31c).
- The provisioner's NetworkPolicy allows the Kubernetes API's usual ports
  anywhere, since its address depends on the cluster. Sites should narrow
  `networkPolicy.egress.kubernetesApi` to their API server.

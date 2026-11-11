# 0048. Backend workers run in baked, isolated pods

- Status: Accepted
- Date: 2026-11-11
- Refines: [0043](0043-workers-lease-jobs.md), [0047](0047-workers-run-only-the-code-a-run-was-launched-with.md)

## Context

Backend-tier workers run plugins' commands inside the deployment's own
cluster. Those commands process regulated data, and whoever audits a GxP
system asks what software ran, where it came from, and what it could
reach. A worker that installs packages when it starts, or can reach the
database, makes each of those questions harder to answer.

## Decision

**Images are built, signed and deployed like the server's.** Iki publishes
`iki-worker`: the worker and micromamba, no plugins, uid 10001. Each
deployment's CI builds its own image on top of it from
`iki deploy build --workers`, with its plugins' code and the environments
their actions run in, built from the packages their lockfiles pin by hash.
The CI checks Iki's signature on the base image first, then signs the
result and attests its provenance. Plugin code and environments belong to
root and are read-only to the worker. A worker installs nothing when it
starts, so the image that was reviewed is the software that runs.

**Pods hold one credential.** The chart's `backendWorkers` Deployment
passes only a backend-tier registration token: a Compute admin makes it,
and the deployment stores it in a Secret. There are no database credentials, no cloud
role (a dedicated service account that mounts no Kubernetes token) and no
other Secrets. Pods meet PodSecurity `restricted`, with a read-only root
filesystem. Jobs write only to a size-limited scratch volume.

**Pods reach only what they need.** Iki pods already deny all traffic by
default. The shared egress policy, which carries the database, now
excludes workers. Their own policy allows DNS and the server's HTTPS
address: the ingress controller (`networkPolicy.ingressFrom`) and, by
default, public addresses, for a cloud load balancer. They can also reach
object storage, through the URLs the server signs. Anything else actions
need, such as a licence server, is listed in
`networkPolicy.egress.workers`. Nothing reaches the workers.

**A pod is a worker for its lifetime.** Without a state file, a worker
registers when it starts and deregisters when it stops. A killed pod's
worker goes offline, and its lease expires and is retried
([ADR 0043](0043-workers-lease-jobs.md)). Each worker reports the CPU and
memory limits and scratch size the chart sets, so the server gives it only
jobs that fit.

**Plain HTTP only by explicit opt-in.** The worker refuses an `http://`
server except on localhost. `IKI_ALLOW_HTTP=1` lifts that for development
clusters without TLS, and the worker warns when it starts with it. The
chart sets it only with `backendWorkers.allowHttp` and `https: false`.

This follows GitHub's Actions Runner Controller and GitLab's runner chart:
runners as pods that hold only a registration secret and pull work over
HTTPS. It adds the default-deny networking and baked, signed images that
regulated sites expect.

## Consequences

- A plugin change reaches workers only through a new image. Rollouts are
  safe because workers lease only runs launched with the code they have
  ([ADR 0047](0047-workers-run-only-the-code-a-run-was-launched-with.md)).
- Actions that need network access beyond the server and object storage
  need a change to the deployment's values, so it gets reviewed. Declaring
  that access per action comes later.
- The public-HTTPS default reaches more than the server. Sites that know
  their load balancer's addresses should narrow `networkPolicy.egress.server`.

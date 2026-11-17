# 0112. Self-hosted workers belong to teams, and an admin registers each one

- Status: Accepted
- Date: 2026-11-17
- Supersedes: [0058](0058-workers-on-peoples-computers-sign-in-as-them.md)
- Refines: [0009](0009-compute-tiers-and-templates.md),
  [0043](0043-workers-lease-jobs.md)

## Context

ADR 0009's client tier ran actions on the launcher's own computer, and ADR
0058 had each person sign a worker in as themselves. A worker ran only its
owner's runs, and its owner managed it under _Your workers_.

The machines that run work outside the cluster are mostly not someone's
laptop. They are the PC beside an HPLC, a workstation with a licensed
desktop package, a GPU box in a lab, a server that reaches an instrument's
share. A team shares them, and IT looks after them. In a GxP lab each one
is a managed asset: someone registered it, its scope is written down, and
every change to it is on record. A worker that belongs to whoever signed in
on it fits none of this. It goes when they leave, its runs depend on who
launched them, and no admin decides what it may run.

GitHub's and GitLab's self-hosted runners work the other way. An admin
registers each machine with a token, for an organization, group or
project, and gives it labels. A job runs only on a runner whose scope
covers its repository and whose labels include every label the job asks
for.

## Decision

**The client tier becomes the self-hosted tier.** It is `self_hosted` in
plugins' actions and environments, the API, the SDK and the worker, and
"Self-hosted" in the web app. There is no alias: a migration renames the
tier on runs, jobs, quotas and costs. Its permission is `run.self_hosted`,
held by Editors as `run.client` was. An agent's self-hosted run queues at
once, as a client-tier one did
([ADR 0050](0050-agents-runs-on-costly-tiers-wait-for-approval.md)).

**A Compute admin registers each machine.** Under Compute → Workers they
make a self-hosted registration token for one or more teams, perhaps
narrowed to some of those teams' projects, with labels and an expiry. On
the machine, `iki-worker register --url <server> --token <iki_wreg_…>`
registers once. It keeps the worker's own `iki_wkr_` token in
`~/.config/iki/worker.json` (`%APPDATA%` on Windows, `IKI_WORKER_STATE` if
set), readable only by its user, so `iki-worker start` needs nothing else.
`iki-worker deregister` takes it off. The worker gets the token's teams,
projects and labels. `worker_registration.created` and `worker.registered`
record them.

**A worker covers its teams' projects.** It covers a project when the
project's team is one of its teams and, if it lists any projects of that
team, the project is one of them. A team none of whose projects it lists is
covered whole, new projects included.

**It runs only runs it covers.** A self-hosted lease looks only in the
projects the worker covers. The scope goes on the skip-scan of projects
with queued work, so the shortlist stays bounded
([ADR 0108](0108-a-lease-weighs-each-projects-next-jobs.md)). Every report
on a job (heartbeat, progress, logs, files, completion) checks the scope
again. A worker taken off a project gets `409 stale_lease` for what it
holds there, as if it had lost the lease.

**Admins manage every self-hosted worker.** Compute → Workers shows each
one's name, labels, teams and projects, whether it's online, its version,
what it's running and when it was last seen. An admin changes its labels
and scope, and `worker.changed` records the scope and labels before and
after. Narrowing the scope interrupts the worker's jobs it no longer
covers, which queue again for a worker that does. Changing labels doesn't
interrupt anything: what's running was matched when it was leased. Revoking
works as for any worker.

**Labels say what a machine has.** They are lowercase words such as
`chromeleon`, `lab-3` or `gpu`, at most 30. Only self-hosted workers have
them.

**A run says why it waits.** A queued self-hosted run says whether no
worker covers its project, or one does but every such worker is offline or
busy. Compute admins get a link to register one. Each job records which
worker ran it, and the run page names that worker.

**People's own workers, the `worker` scope and the `iki-worker` OAuth client
go.** `iki-worker login` and `logout` say that registration replaced them,
and how to register. Nothing carries over: Iki has no installs from before
this, so the migrations rename the tier in place and create no `iki-worker`
client, with nothing to convert.

## Consequences

- Each self-hosted machine is registered by a Compute admin, has a written
  scope, and every change to it is audited. Nothing depends on who launched
  a run, or on someone staying at the company.
- A leaked state file gives a token that leases only the runs its teams'
  projects queue on the self-hosted tier. An admin revokes it, or narrows
  it, under Compute → Workers.
- A team that wants a machine of its own asks a Compute admin, as it would
  for a GitHub runner. Someone who ran their own actions on their laptop
  now needs a token for it.
- A self-hosted action may now ask for resources and GPUs, and a trigger may
  run one: the machine is a team's, not whoever happens to be at it
  ([ADR 0040](0040-action-and-environment-manifests.md),
  [ADR 0080](0080-triggers-run-actions-when-entities-change.md)). It still
  can't declare secrets or network: Iki can't keep either from whoever runs
  the machine.
- Offline self-hosted workers raise `IkiWorkersOffline` like any other
  tier's ([ADR 0098](0098-metrics-dashboards-and-alerts.md)): they're
  managed assets now, not laptops that are often off.
- Self-hosted workers update themselves to signed releases
  ([ADR 0060](0060-workers-update-themselves-to-signed-releases.md)), and
  `IKI_WORKER_AUTO_UPDATE=0` leaves that to IT.
- The worker still builds for Linux and macOS only. Windows needs process
  groups replaced with Job Objects, a Windows self-update, and a Windows
  release build; that's a later step.

## Alternatives considered

- **Keep people's own workers beside team ones.** Two models of the same
  tier, one of which can't be audited as an asset. A team can still
  register someone's workstation for its projects.
- **Scope by organization only, with labels for everything else.** A label
  says what a machine has, not whose data it may see. A machine on one
  team's bench mustn't run another team's runs just because it has the
  right software.
- **Matching in the database by scanning the queue for runs a worker
  covers.** That's the full queue scan ADR 0108 removed. The scope filters
  the projects the skip-scan visits instead.

# 0133. A Slurm cluster runs the container-tier jobs its templates place

- Status: Accepted
- Date: 2026-11-18
- Refined by: [0167](0167-a-clusters-worker-watches-its-jobs-in-one-request.md), on how cancels reach a running job
- Amends: [0009](0009-compute-tiers-and-templates.md),
  [0046](0046-how-iki-worker-runs-a-command.md)
- Refines: [0041](0041-compute-templates-and-bindings.md),
  [0043](0043-workers-lease-jobs.md),
  [0061](0061-container-tier-runs-get-pods-of-their-own.md),
  [0063](0063-the-cluster-admits-only-images-iki-signed-or-the-deployment-approved.md),
  [0070](0070-runs-show-what-they-cost.md),
  [0112](0112-self-hosted-workers-belong-to-teams.md)

## Context

Most research organizations already have a Slurm cluster, and its GPUs and
fast storage are where heavy work like structure prediction should run.
Its administrators decide who may use which partition, charge each job to an
account, and keep the cluster's queue fair. Iki must use the cluster the way
its other users do. It should not stand a second scheduler beside it.

Two kinds of system sit in front of Slurm:

- **CI runners**, such as GitHub's self-hosted runners and GitLab's Slurm
  executor. A runner registered for a scope takes one job at a time.
- **Workflow engines**, such as Nextflow's and Cromwell's Slurm executors and
  Snakemake's profiles. One process submits many tasks, each sized from the
  task, and follows them in the queue. The pipeline doesn't change when it
  moves from Kubernetes to Slurm: the executor is configuration.

Open OnDemand submits as each person, under their own Unix account. That
needs the person to have an account on the cluster, and a submit host that
can act as them.

Iki's tiers say what a plugin's action needs: the server, a pod's isolation
and network policy, a cloud machine, or a lab's own computer. A cluster's
compute node, running the action's image under Apptainer, gives what the
container tier gives. It runs isolated, with the image's own software, and
reaches the internet only through the egress proxy.

## Decision

**Slurm is a provisioner of the container tier, not a tier.** A compute
template with `provisioner: slurm` names its cluster, partition, QOS,
default account, the node features it needs, the GPU type, the partition's
time limit, and where its compute nodes reach the egress proxy. Its
`capacity` is the most one job may ask for. Templates live in compute.yaml,
which is reviewed like code (ADR 0041). The web app shows them under
Compute → Slurm, and doesn't edit them: a change to where regulated work
runs goes through the deployment's change control.

```yaml
apiVersion: iki/v1
kind: ComputeTemplate
name: hpc-gpu
provisioner: slurm
capacity: { cpu: 64, memory: 512Gi, gpu: { count: 4, memory: 80Gi, model: A100 } }
slurm:
  cluster: hpc1
  partition: gpu
  qos: normal
  account: rd-shared
  gpu_type: a100
  max_walltime: 2d
  egress_proxy_url: http://iki-proxy.hpc.example.com:3128
price: { per_cpu_hour: 0.02, per_gpu_hour: 1.10, per_memory_gib_hour: 0.001, currency: USD }
limits: { max_concurrent: 50 }
```

Plugins stay portable. An environment's binding decides whether its jobs go
to Kubernetes or to the cluster, as Nextflow's profile does. Container-tier
quotas, budgets, cost estimates and agents' approvals (ADRs 0045, 0050, 0071) apply unchanged. `iki deploy build` refuses a Slurm template with a
machine's `per_hour` price, a pod's `kubernetes` settings, warm machines,
or volumes. It refuses a binding whose templates can't run one of its
actions because their partitions don't allow its timeout. It warns when
they can't because the action reaches the internet, or calls outside APIs
(ADR 0130), and the clusters have no
`egress_proxy_url`. The server says why such an action
can't run there, wherever it would be launched.

**A job goes where the first template that takes it is.** That is the
first template, in its binding's order, that it fits, that takes its
lifecycle, and whose time limit and network it fits. If that template is
a Slurm one, Iki starts no machine for it, and only that cluster's workers
lease it. Each job runs in an allocation of its own, so a Slurm template
takes both ephemeral and persistent actions.

**A worker on the cluster's login node submits the jobs.** A Compute admin
makes a container-tier registration token for the cluster (Compute →
Workers). On a login node, `iki-worker register` registers once and keeps
its token (ADR 0112). With `IKI_SLURM_WORK_DIR` set to a directory the
compute nodes mount at the same path, `iki-worker start` serves the
cluster. Two login nodes can each run one.

**It holds many jobs at once.** This departs from one job per worker
(ADRs 0009 and 0046). That rule keeps one job's files, secrets and cost
apart from another's on the machine that runs it. Here, Slurm's allocation
is that machine: each job still runs alone in its own allocation, and only
the submitting is shared. A worker that runs no jobs itself would otherwise
need one process per job. It leases while it holds fewer than `IKI_SLURM_MAX_JOBS`, and the
server leases it a job only while the job's template is under its
`max_concurrent`. Leases on a cluster take an advisory lock, so the limit
holds exactly across login nodes. A job waiting for room says so on its
run's page, as does one whose cluster has no worker online.

**The server places each job as it's leased.** It records the job's Slurm
placement:

- the template;
- the partition and QOS;
- the account the project is charged to there: the one a Compute admin set
  for the project under Compute → Slurm (audited, with the account before
  and after), or the template's default;
- what to ask Slurm for, sized from the action's environment: CPUs, memory,
  GPUs, scratch disk, and the action's timeout as its walltime.

A project with no account there, on a template with no default, fails at
lease with a message saying who can fix it. Jobs already placed keep their
account.

**The worker stages each job on the login node, and submits it.** Into the
job's own directory (mode 0700) it writes:

- `input.json`;
- the input files, from object storage;
- a copy of its plugin's code;
- `step.json`: what to run;
- `secrets.json` (0600): the job's secrets and egress proxy credentials;
- a batch script.

The worker's token is never in the directory, the script, or the job's
environment. It submits with:

```sh
sbatch --parsable --export=NONE --no-requeue --nodes=1 --ntasks=1 \
  --job-name=iki-<run>-<attempt> --comment=iki:<job> \
  --partition --account --qos --cpus-per-task --mem --gpus[=type:n] \
  --tmp --time --constraint --output/--error=<dir>/log
```

Compute nodes need no network to Iki or object storage. The results go up
from the login node once the job ends. `--no-requeue` keeps an attempt one
attempt: when Slurm preempts it or its node fails, the server retries it if
its action allows (ADR 0043).

**On its node, the job runs in its image under Apptainer.** The batch script
runs `iki-worker slurm-step <dir>`, from a copy of the worker in the shared
work directory. That step reads `secrets.json` once and deletes it. It then
becomes:

```sh
apptainer exec --containall --cleanenv --home <dir>/home --bind <dir> [--nv] <image.sif> <command>
```

The job's variables are passed as `APPTAINERENV_*`, and its home as `--home`.
Apptainer takes the step's place, so Slurm signals the command and records
its exit status.

**Images are checked before they're pulled.** No admission controller stands
in front of a cluster's nodes. The worker checks the image's signature with
`cosign verify` against the signers the deployment trusts (ADR 0063), which
the server sends with each job. Those are Iki's release workflow, and the
deployment's build into its approvals repository. The chart's
`containerTier.imagePolicy` sets them. Only then does the worker
`apptainer pull` the image by its digest into a shared SIF cache. Each job
checks the signature again, so a signer the deployment stops trusting runs
nothing more. The run's Slurm record keeps who signed the image. A
deployment that names no signers runs images unchecked, with a warning in
the worker's log, as its container tier does.

**The worker follows its jobs in Slurm's queue.** One `squeue` call covers
every job it holds, every 15 seconds. A job waiting in the queue is in the
new job state `pending` ("Waiting in the cluster's queue"), which only a
cluster's jobs take. Its heartbeats renew its lease, and cancels arrive
with them. The worker streams the job's log from its output file, and says
what Slurm reports (`POST /worker/v1/jobs/{id}/slurm`): the Slurm job ID,
state, reason, nodes and times. Once the job leaves the queue, `sacct`
says how it ended:

| Slurm's state                               | The attempt                                            |
| ------------------------------------------- | ------------------------------------------------------ |
| `PENDING`, `CONFIGURING`, a requeue or hold | `pending`                                              |
| `RUNNING`, `COMPLETING`, `SUSPENDED`        | `running`                                              |
| `COMPLETED`                                 | its output is read and its results go up               |
| `FAILED`                                    | `failed`, with what the command said                   |
| `OUT_OF_MEMORY`                             | `failed`, saying it used more memory than it asked for |
| `TIMEOUT`, `DEADLINE`                       | `timed_out`                                            |
| `CANCELLED` by Iki, for a cancelled run     | `cancelled`                                            |
| `CANCELLED` by someone else                 | `interrupted`, saying so, retried if it may be         |
| `PREEMPTED`, `NODE_FAIL`, `BOOT_FAIL`       | `interrupted`, retried if it may be                    |

A cancelled run's job is `scancel`led. A job whose lease goes elsewhere is
`scancel`led too. A job's timeout counts from when it started on its
nodes, not while it waited in the queue. On a cluster without accounting,
the batch script's note of the exit status stands in for `sacct`.

**A login node can restart without losing a job.** A Slurm job's lease
lasts ten minutes without a heartbeat, not one. Each job's directory keeps
`slurm.json`: the job, its lease and its Slurm job's ID, or the job's name
to find it by if the worker stopped between submitting and noting the ID.
A stopping worker leaves its jobs running. Starting again, it picks each
one up whose lease is still its own, and `scancel`s and removes the rest.

**A Slurm job costs what it was given while it ran.** Its cost is its
elapsed time on its nodes times what `sacct` says Slurm allocated, at the
template's rates per CPU-, GPU- and GiB-of-memory-hour. Time in the queue
costs nothing. Its cost record names the Slurm job, not a machine. Budgets
count running jobs from their start on the cluster, and estimates price
the action's environment at the template's rates.

**Admins and launchers see the Slurm job.** A run's page shows its attempt's
Slurm job:

- its ID and state, and why it waits;
- its partition, account and QOS, and its nodes;
- what it asked for, and what `sacct` says it used: elapsed, CPU time, peak
  memory, and what was allocated;
- its exit code, and who signed its image.

Compute → Slurm shows each cluster's templates, workers and projects'
accounts. The API has the same.

**Jobs run as the worker's service account.** Every job runs under the Unix
account the login node's worker runs as, and is charged to the project's
Slurm account. `--comment=iki:<job>` and the job's name tie each Slurm job
to its Iki run and person, so the cluster's accounting can be joined to
Iki's audit trail. Per-person submission, as Open OnDemand does it, can come
later. It needs each person mapped to a cluster user, and the worker to
submit as them: through slurmrestd with per-user tokens, or `sudo -u` from
the login node. The placement and the `slurm` report would carry the user,
and nothing else would change.

## Consequences

- Positive: an organization's cluster runs Iki's container-tier actions
  without changing them, under its own scheduler, partitions, accounts and
  fair share. Compute nodes need no network beyond the shared file system,
  and the egress proxy for actions that need the internet.
- Positive: the template, account, Slurm job ID, image signer and usage of
  each attempt are on record, and account changes are audited: what ran
  where, charged to whom, can be shown to an auditor.
- Positive: a login node restarting, or Slurm preempting a job, loses no
  work Iki can't retry.
- Negative: one worker holds many leases, a second shape of worker beside
  ADR 0046's. Its jobs take heartbeats rather than a long-poll for cancels,
  so a cancel takes up to 20 seconds to reach the cluster.
- Negative: environments with volumes (ADR 0075) don't run on a cluster yet.
  Their setup builds would need to run there too. Until then, binding one to
  a Slurm template is refused.
- Negative: jobs run under one service account. Per-person accounting on
  the cluster is a later step.
- Negative: the shared work directory holds jobs' inputs and results while
  they run, protected by file modes alone. The cluster's file system must
  not let other users read the worker's directory.

## Alternatives considered

- **A tier of its own.** Plugins would have to declare `slurm` to run there,
  and a deployment without a cluster couldn't run them. The provisioner is
  where the cluster belongs, as it is for Nextflow.
- **One worker per job, as runners do.** A process per queued job on the
  login node, or an allocation held by the worker for the job's life, would
  waste the login node or the cluster.
- **slurmrestd.** Not every site runs it, and its JWT keys are the
  cluster's admins' to give. The CLI is on every login node. A later
  per-person mode can use it.
- **Pulling images on compute nodes.** That needs registry access from
  every node, and the signature check to run there. Checking and pulling on
  the login node keeps nodes offline and checks once per job.

# Running Iki's jobs on a Slurm cluster

How a cluster's administrators and an Iki Compute admin set a Slurm cluster
up to run Iki's container-tier actions, and look after it.
[ADR 0133](adr/0133-a-slurm-cluster-runs-container-tier-jobs-its-templates-place.md)
has the design. In short, a worker on the cluster's login node leases Iki's
jobs, stages each one on the cluster's shared file system, and submits it
with `sbatch`. On the job's node, the job runs in its plugin's image under
Apptainer.

## What the cluster needs

- **A login node** (or any submit host) that reaches the Iki server and the
  deployment's object storage over HTTPS. Slurm's `sbatch`, `squeue`,
  `sacct` and `scancel` must be on it, with Apptainer 1.2 or later and
  [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
  2.x. The login node also needs the registries the deployment's images
  come from.
- **A shared file system** that the compute nodes mount at the same path as
  the login node, such as `/scratch/iki`. It holds each job's inputs,
  outputs and log while the job runs, and the images as SIF files. Plan for
  the largest images, plus the inputs and outputs of the most jobs you let
  run at once.
- **Apptainer on every compute node**, at the same path as on the login
  node. GPU nodes need the NVIDIA driver: jobs that ask for GPUs run with
  `--nv`.
- **A service account**: a Unix user the worker runs as, which may submit to
  the partitions and accounts Iki uses. Its home directory keeps the worker's
  token. Every Iki job runs as this user.
- **Accounting** (slurmdbd) is strongly recommended. Without it, Iki still
  learns each job's exit status, but not what the job used, so it can't
  price the job, and preempted or failed nodes look like lost jobs. With
  `AccountingStoreFlags=job_comment` in `slurm.conf`, `sacct` also keeps
  each job's comment, `iki:<job>`, which finds the Iki job a Slurm job ran.
- **The egress proxy, if actions reach the internet or call outside APIs**
  (ADR 0130). Compute nodes need to reach the deployment's egress proxy
  (ADR 0053) at the template's `egress_proxy_url`. Usually that's a
  forwarded port on a host the nodes can reach. Without it, Iki refuses those actions on the cluster, and
  doesn't run them unproxied.

Compute nodes need no network to Iki or object storage.

## Templates (compute.yaml)

A Slurm template places jobs on one partition of one cluster. Each template
is a `ComputeTemplate` with `provisioner: slurm`. It goes in the
deployment's `compute.yaml`, which is reviewed and deployed like code
([ADR 0041](adr/0041-compute-templates-and-bindings.md)):

```yaml
apiVersion: iki/v1
kind: ComputeTemplate
name: hpc-gpu
provisioner: slurm
capacity:
  cpu: 64
  memory: 512Gi
  gpu: { count: 4, memory: 80Gi, model: A100 }
slurm:
  cluster: hpc1 # Iki's name for the cluster; workers register for it
  partition: gpu
  qos: normal # optional
  account: rd-shared # optional: the account for projects without their own
  constraint: a100 # optional: --constraint, the node features it needs
  gpu_type: a100 # optional: --gpus=a100:<n>
  max_walltime: 2d # the partition's time limit
  egress_proxy_url: http://iki-proxy.hpc.example.com:3128 # optional
price: { per_cpu_hour: 0.02, per_gpu_hour: 1.10, per_memory_gib_hour: 0.001, currency: USD }
limits: { max_concurrent: 50 }
```

`capacity` is the most one job may ask for there. `limits.max_concurrent`
caps how many of the template's jobs are in the cluster at once, waiting or
running. The price is what Iki charges a project's budget for each hour of
what Slurm allocated. Leave it out to show no cost. Bind environments to
the template like any other, in order of preference:

```yaml
apiVersion: iki/v1
kind: ComputeBinding
environment: colabfold/gpu
templates: [hpc-gpu, gke-gpu]
```

`iki deploy build` refuses a template or binding the cluster can't run:

- a machine's `per_hour` price;
- `kubernetes` settings;
- warm machines;
- an environment with volumes;
- a binding none of whose templates allows an action's timeout within its
  `max_walltime`.

It warns of an action that reaches the internet, or calls outside APIs,
when its binding has only
templates without an `egress_proxy_url`. Wherever the action would be
launched, the server says why it can't run there.

Templates are shown, read-only, under **Compute → Slurm**.

## Accounts per project

Each project's jobs are charged to a Slurm account on each cluster. Under
**Compute → Slurm**, a Compute admin sets the account for each project. The
API is `PUT /api/v1/compute/slurm/{cluster}/accounts/{project_id}`. A
project without one uses its template's `account`. If the template has none,
the project's runs fail with a message saying who can set it. The audit log
records each change, with the account before and after
(`slurm_account.changed`).

The service account must be a member of every account it charges. Use
`sacctmgr add user iki-svc account=<account>`.

## Setting up the login node

1. **Register the worker.** Under **Compute → Workers**, a Compute admin makes
   a container-tier registration token, with **Slurm cluster** set to the
   cluster's name (`hpc1`). On the login node, as the service account:

   ```sh
   iki-worker register --url https://iki.example.com --token iki_wreg_… --name hpc1-login1
   ```

   This keeps the worker's token in `~/.config/iki/worker.json`, readable
   only by the service account. Keep that home directory off the shared file
   system if you can.

2. **Make the work directory**, readable by the service account only:

   ```sh
   install -d -m 0700 -o iki-svc /scratch/iki
   ```

   The worker keeps every job's files in it, and any other user who could
   read it would see inputs and results. Check that the file system honors
   modes. Some parallel file systems need an ACL or a project directory
   instead.

3. **Run it as a service.** A systemd unit, for example:

   ```ini
   [Unit]
   Description=Iki worker for the hpc1 Slurm cluster
   After=network-online.target

   [Service]
   User=iki-svc
   Environment=IKI_SLURM_WORK_DIR=/scratch/iki
   Environment=IKI_SLURM_MAX_JOBS=200
   ExecStart=/opt/iki/bin/iki-worker start
   Restart=always
   KillSignal=SIGTERM
   TimeoutStopSec=60

   [Install]
   WantedBy=multi-user.target
   ```

| Variable                 | Default                      | What                                                                    |
| ------------------------ | ---------------------------- | ----------------------------------------------------------------------- |
| `IKI_SLURM_WORK_DIR`     | (required)                   | The shared directory jobs are staged in. Setting it turns on Slurm mode |
| `IKI_SLURM_IMAGE_CACHE`  | `$IKI_SLURM_WORK_DIR/images` | Where images are kept as SIF files                                      |
| `IKI_SLURM_MAX_JOBS`     | 100                          | The most jobs this worker holds at once                                 |
| `IKI_SLURM_POLL_SECONDS` | 15                           | How often it asks `squeue` after its jobs                               |
| `IKI_SLURM_BIN`          | on `PATH`                    | The directory of `sbatch`, `squeue`, `sacct` and `scancel`              |
| `IKI_APPTAINER`          | `apptainer`                  | Apptainer, at the same path on the compute nodes                        |
| `IKI_COSIGN`             | `cosign`                     | cosign                                                                  |

The worker won't start if its registration and its settings disagree:
`IKI_SLURM_WORK_DIR` set on a worker that isn't registered for a cluster,
or unset on one that is. It also needs its token kept across restarts, so
it doesn't take a one-off `IKI_WORKER_TOKEN`.

Like any self-hosted worker, it fetches the code of the plugins the
deployment installs from a commit, and runs their jobs. A development
server's plugins from local directories aren't sent to it: set
`IKI_PLUGINS_DIR` to their code instead.

A second login node can run a second worker with the same work directory
and image cache. The server keeps the template's `max_concurrent` across
both.

## What a job does

For each job, the worker:

1. makes the job's directory, `$IKI_SLURM_WORK_DIR/jobs/<job>` (mode 0700);
2. downloads the job's inputs, and copies in its plugin's code;
3. checks the image's signature with `cosign verify`, against the signers
   the deployment trusts (the chart's `containerTier.imagePolicy`). It then
   pulls the image by its digest into the image cache, unless it's already
   there;
4. writes `step.json`, `secrets.json` (mode 0600) and `job.sh`, and submits
   it:

   ```sh
   sbatch --parsable --export=NONE --no-requeue --nodes=1 --ntasks=1 \
     --job-name=iki-<run>-<attempt> --comment=iki:<job> \
     --partition=gpu --account=<project's> --cpus-per-task=… --mem=… \
     --gpus=a100:1 --time=… --output=<dir>/log job.sh
   ```

5. follows the job in `squeue`, renews its lease, streams its log to the
   run's page, and `scancel`s it if the run is cancelled;
6. once the job leaves the queue, reads how it ended and what it used from
   `sacct`, uploads its results, and removes its directory.

On the node, `job.sh` runs `$IKI_SLURM_WORK_DIR/bin/iki-worker-<version>
slurm-step <dir>`, which reads and deletes `secrets.json` and becomes:

```sh
apptainer exec --containall --cleanenv --home <dir>/home --bind <dir> [--nv] <image>.sif <command>
```

The worker's own token is never in the job's directory or its environment.

To find Iki's jobs on the cluster:

```sh
squeue -u iki-svc -o '%i %j %k %T %R'      # %k is the comment: iki:<job>
sacct -u iki-svc --format=JobID,JobName%30,Comment%45,Account,State,Elapsed,AllocTRES%60
```

The run's page shows the same Slurm job ID, its state, and why it waits.

## Restarts, upgrades and draining

- **Restarting the worker** leaves its jobs running. On SIGTERM it stops
  leasing and lets go of its jobs. When it starts again, it picks up each
  job from its directory's `slurm.json`. A job's lease lasts ten minutes
  without the worker. If the worker is away longer, the server gives up on
  the job, and the worker `scancel`s it and removes its directory when it
  comes back.
- **Upgrading** works as for any self-hosted worker: it updates itself to
  the release the server wants when it holds no jobs, or IT installs the new
  binary and restarts the service. Running jobs keep the version they started
  with, under `bin/`.
- **Draining a partition** for maintenance: lower the template's
  `max_concurrent`, or bind its environments elsewhere, and deploy. Jobs
  already submitted run to the end. Jobs Slurm preempts, or whose nodes
  fail, end as interrupted and are retried if their action allows.
- **Taking a cluster off Iki**: revoke its workers under **Compute →
  Workers**. Each worker `scancel`s its jobs and stops.

## When something's wrong

| What the run page says                                                           | What to check                                                                                                                  |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Waiting for the hpc1 cluster's worker: none is online                            | The worker service on the login node, and its log (`journalctl -u iki-worker`)                                                 |
| Waiting for room on the hpc1 cluster                                             | The template's `max_concurrent`; jobs already in the cluster                                                                   |
| Waiting in the cluster's queue (reason: `Priority`, `QOSMaxJobsPerUserLimit`, …) | Slurm's own queue: the reason is `squeue`'s                                                                                    |
| The cluster refused the job: …                                                   | The partition, account and QOS: `sacctmgr show assoc user=iki-svc`                                                             |
| … has no Slurm account on hpc1                                                   | Set the project's account under Compute → Slurm                                                                                |
| Its image … isn't signed by anyone the deployment trusts                         | The chart's `containerTier.imagePolicy`, and that cosign on the login node reaches the registry and Sigstore's public instance |
| Slurm stopped it: it used more than the … of memory it asked for                 | Raise the environment's memory in the plugin                                                                                   |
| Slurm stopped it at its time limit                                               | The action's timeout; the partition's `max_walltime`                                                                           |
| Its Slurm job … was cancelled on the cluster by user …                           | Someone `scancel`led it outside Iki; it's retried if its action allows                                                         |

The worker logs one JSON line per event to stderr, with the job and its
Slurm job's ID.

## Per-person accounting

Every job runs as the service account, and is charged to its project's
account. The job's comment, `iki:<job>`, ties it to its Iki run, and the
run's audit trail names the person who launched it. Join `sacct` to Iki's
audit log to report usage by person. Submitting as each person, as Open
OnDemand does, is planned (ADR 0133).

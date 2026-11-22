# 0075. Environment setup builds fill managed volumes, and runs read snapshots of them

- Status: Accepted
- Date: 2026-11-13
- Refines: [0040](0040-action-and-environment-manifests.md),
  [0041](0041-compute-templates-and-bindings.md),
  [0053](0053-runs-reach-the-internet-through-an-egress-proxy.md),
  [0061](0061-container-tier-runs-get-pods-of-their-own.md)

## Context

Some tools read large reference data on every run: AlphaFold's databases
are about 3 TB; a BLAST database or a genome index can be hundreds of
gigabytes. An environment declares such data as a volume that its setup
script fills (ADR 0040), but until now nothing ran the script, and nothing
gave runs the data.

Downloading it for each run would take hours and cost more than the run.
Keeping one shared copy that every machine mounts is the obvious
alternative, but a shared copy changes under running jobs when it's
refreshed, and then nobody can say which data a result came from. In a
GxP setting that is the question an auditor asks first: which release of
the database did this prediction use, who decided to change it, and when.

Filling a volume is also expensive and reaches the internet. It should be
a deliberate act by someone answerable for the deployment's compute, with
a second person's approval when the environment asks for one, and with
the same limits that runs have.

## Decision

**A setup build fills an environment's volumes once, and each build makes
a new version.** A Compute admin asks for one under Compute → Volumes (or
`POST /api/v1/compute/setups`), giving a reason. Builds belong to the
deployment, not a project: their data serves every project's runs, and
their cost is the deployment's. There's one build for each place the
environment's runs go (a Kubernetes namespace), and only one unfinished
build per place at a time. Builds get HRIDs (`SETUP-000012`), and the
version is per environment and place: 1, 2, 3.

**One whose setup says `requires_approval` waits for another Compute
admin.** Whoever asked for it can't approve it, as with pull requests whose
author can't approve their own. Asking, approving, cancelling, finishing
and retiring are each in the audit log.

**A build runs on a machine of its own, as a run does.** The provisioner
makes a disk (a PersistentVolumeClaim, of the template's StorageClass) for
each volume the setup fills, then starts a pod with the environment's image
and Iki's worker, with the disks mounted writable where the environment
says. The pod is locked down like any run pod (ADR 0061). Its worker leases
the build rather than a run: the lease answer is a `SetupInput`
(`spec/…/setup-input/v1`), and the worker runs the script with `sh` from
the plugin's code, with heartbeats, a timeout, cancelling and a streamed
log, as it does a run's command. The script can reach only the hosts its
setup lists under `network`, through the egress proxy, with the build's own
credentials; what it reached, and what was refused, is recorded with the
build. It says what the data is by writing `{"data_version": "…"}` to
`IKI_OUTPUT`. A machine for a build runs nothing else, and isn't counted
as capacity for runs.

**Its disks are snapshotted once its machine has gone.** The provisioner
waits until the pod has ended, so nothing is still writing, then takes a
VolumeSnapshot of each disk with the template's VolumeSnapshotClass. When
every snapshot is ready to use, the build has succeeded and its disks are
deleted. If a snapshot fails, or isn't ready within six hours of the
build's timeout, the build fails.

**A run pins its reference data at launch.** Iki chooses the newest ready,
unretired build in the first eligible place that has one. If none is ready,
the run waits, then pins a build before its first attempt. Every attempt
leases a machine with that exact version. Each run pod mounts a read-only
disk made from the pinned snapshot (a generic ephemeral volume with the
snapshot as its data source). Kubernetes deletes the disk with the pod.

**Each attempt records the version it read**: the build, its HRID, the
version and the data version, in the job and on the run's page. A result
can be traced to the exact data, and to who asked for it and who approved
it.

**Retiring a version stops new runs selecting it.** Runs already pinned to
the build can still start or retry with its snapshots. Iki deletes them
once every pinned run has ended and every machine with a copy has gone.
A failed or cancelled build's disks and snapshots are deleted at once,
as is anything labelled `iki.ikisci.org/setup` whose build is gone.

**Templates say how their machines get each volume.** In `compute.yaml`,
a template's `volumes` names a strategy for each volume:
`clone_from_snapshot` on Kubernetes now, with optional `storage_class` and
`snapshot_class`. The provisioner's Role gains create, get, list and delete
on PersistentVolumeClaims and VolumeSnapshots in the namespaces where runs
go, and nothing else. The cluster needs a CSI driver that supports
snapshots; the development cluster uses the CSI host-path driver.

## Consequences

- A deployment can run AlphaFold-sized tools without downloading their data
  for each run, and every result says which release of the data it used.
- Changing reference data is a recorded, approvable act, separate from
  installing or upgrading a plugin.
- A new version uses as much storage again until a Compute admin retires
  the old one and its last pinned run and machine have ended.
- Each run pod's disk is a clone of the snapshot. Most CSI drivers make
  these quickly and lazily, but the first reads may be slower than from a
  disk made in advance.
- VM-tier machines get these volumes from EBS snapshots, with fast snapshot
  restore when a template asks ([0076](0076-vm-tier-volumes-come-from-ebs-snapshots.md)).
  Shared file systems give each version a directory of its own
  ([0077](0077-shared-filesystem-volumes-are-a-directory-per-version.md)).
  Object storage mounts and disks one run at a time changes come later.

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

**Machines for the environment's runs get a read-only copy of the newest
version that isn't retired.** Each run pod gets a new disk made from the
snapshot (a generic ephemeral volume with the snapshot as its data source),
mounted read-only, and deleted with the pod. Runs therefore never see data
change under them, and a new version reaches only machines started after
it's ready; warm pods with the old version are replaced, since their spec
includes it. Until a version is ready, the environment's runs wait, saying
so: "Waiting for its reference data (af-db): setup build SETUP-000012 is
running."

**Each attempt records the version it read**: the build, its HRID, the
version and the data version, in the job and on the run's page. A result
can be traced to the exact data, and to who asked for it and who approved
it.

**Retiring a version stops new machines getting it.** They get the newest
version that isn't retired, or none, and their runs wait. Machines that
have it keep it, and its snapshots are deleted once none does. A failed or
cancelled build's disks and snapshots are deleted at once, as is anything
labelled `iki.ikisci.org/setup` whose build is gone.

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
- A new version uses as much storage again until the old one is retired and
  no machine has it. Compute admins retire old versions.
- Each run pod's disk is a clone of the snapshot. Most CSI drivers make
  these quickly and lazily, but the first reads may be slower than from a
  disk made in advance.
- VM-tier machines get these volumes from EBS snapshots, with fast snapshot
  restore when a template asks ([0076](0076-vm-tier-volumes-come-from-ebs-snapshots.md)).
  Shared file systems, object storage mounts and disks one run at a time
  changes come later (step 36d).

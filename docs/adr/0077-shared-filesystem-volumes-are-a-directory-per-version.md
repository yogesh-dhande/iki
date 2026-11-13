# 0077. Shared file system volumes are a directory per version

- Status: Accepted
- Date: 2026-11-13
- Refines: [0075](0075-environment-setup-builds-and-managed-volumes.md)

## Context

ADR 0075 fills an environment's volumes with a setup build and gives each
of its runs' pods a read-only copy of the newest version's snapshot. Many
deployments already have a file system that every node can mount at once:
FSx for Lustre, EFS, or NFS. For reference data of terabytes read by many
runs, it serves them all from one copy, with no disk to make for each pod
and nothing to load before a run starts. Templates could already say
`strategy: shared_filesystem`, but nothing filled or mounted it.

ADR 0075 turned down one shared copy because refreshing it changes data
under running jobs, and then nobody can say which data a result came from.
That holds for one copy, not for a file system: each version can have its
own place on it, never changed once its build is done.

## Decision

**A template names the file system and a directory in it for Iki.** On
Kubernetes, a volume with `strategy: shared_filesystem` names the file
system's PersistentVolumeClaim (`claim`), in the namespace its runs go in,
which pods must be able to mount at once (ReadWriteMany), and the directory
Iki keeps versions under (`path`, `iki` unless set). The deployment's
administrator makes that directory and lets runs' user (UID 10001) write it:
Iki never writes anywhere else on the file system.

**Each version is a directory of its own, which only its build writes.** A
setup build's version of a volume is
`<path>/<plugin.environment>/<volume>/<build HRID>`. Before the script
starts, a container of the worker's image, as runs' user, makes that
directory (Kubernetes would make a missing one as root, which runs' user
couldn't write) with `iki-worker prepare`, never through a symbolic link.
The script's container then mounts only that directory, writable, where the
environment says: it can't see or change any other version. Nothing is
copied: once the script has succeeded and the pod has gone, the version is
ready.

**Runs mount their version's directory read-only.** Each run pod mounts the
claim read-only, with the newest version's directory as its `subPath`, where
the environment says. Writing to it fails, and the run sees only that
version. Each attempt records it, as for snapshots.

**Runs go where a version is ready.** An environment bound to templates in
several places (namespaces, or regions) has a build in each. Its runs go to
the first template, in the binding's order, whose place has a ready version,
rather than waiting for the first template's; with none ready, they wait
there, saying why.

**A retired or failed version's directory is removed by a Job.** Once a
version isn't kept (it's retired and no live pod has it, or its build
failed, timed out or was cancelled, and its pod has gone), the provisioner
starts a Job in the place's namespace: the worker's image, as runs' user,
with the claim's `path` mounted writable, running
`iki-worker remove /iki/shared <directory>`. The worker removes only plain
relative paths under the directory it's given, never following a symbolic
link out of it. Once the Job succeeds, the version is deleted. One that
fails says so on the version, and is kept, for its log, until Kubernetes
deletes it ten minutes later; then it's started again. The Job carries the build's label
but not a launch's, so it's never mistaken for a run's machine, and Jobs
whose build is gone are deleted. The provisioner's Role already lets it
manage Jobs where runs go.

**EC2 instances come next.** Templates for the `aws_ec2` provisioner can't
fill a `shared_filesystem` volume yet; their builds aren't offered there.

## Consequences

- Runs start without waiting for a disk to be made or loaded, and every run
  in a place reads the same copy, so a version's storage is paid for once.
- The file system's performance is shared by every run reading it; a
  template whose runs need a disk's IOPS to themselves uses
  `clone_from_snapshot` instead.
- Kubernetes keeps versions apart: a build's script and its runs each mount
  only their version's directory, and only the worker's own commands mount
  the directory for Iki. Kubernetes refuses a `subPath` that leads out of
  its volume through a symbolic link.
- A Kubernetes PersistentVolume's own access controls apply: a claim the
  provisioner names must be bound, and readable and writable by UID 10001.
- The dev cluster has a second namespace for runs, `iki-runs-shared`, with a
  host-path volume standing in for the file system; its end-to-end checks
  fill, mount, and remove a version there.

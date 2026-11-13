# 0078. Bucket volumes are copied from a disk into a prefix per version

- Status: Accepted
- Date: 2026-11-13
- Refines: [0077](0077-shared-filesystem-volumes-are-a-directory-per-version.md)

## Context

ADR 0077 gives each version of an environment's volume a directory of its
own on a shared file system. Many deployments keep reference data in S3
instead: it's cheaper than a file system for terabytes read now and then,
and the Mountpoint for Amazon S3 CSI driver mounts a bucket in pods.
Templates could already say `strategy: object_mount`, but nothing filled or
mounted it.

A bucket mounted with Mountpoint isn't a file system a script can write as
it likes. Files can only be written once, from start to end. They can't be
renamed or changed afterwards, and there are no symbolic links. Setup scripts
download with tools that write out of order (aria2c), unpack archives, and
move files into place, so most of them would fail if they wrote to the bucket
directly, and fail in ways their authors can't foresee.

## Decision

**A template names the bucket's claim and a directory in it for Iki, as for
a shared file system.** On Kubernetes, a volume with `strategy: object_mount`
names the PersistentVolumeClaim (`claim`) of a PersistentVolume of the
Mountpoint for Amazon S3 CSI driver, in the namespace its runs go in. It also
names the directory (a prefix) Iki keeps versions under (`path`, `iki` unless
set). The deployment's administrator mounts the bucket so that runs' user
(UID 10001) can write and delete there (`uid=10001`, `gid=10001`,
`allow-delete`). `storage_class` names the class of the disk a build fills
(the cluster's default unless set).

**A setup build fills a disk, which a Job then copies into the version's own
prefix.** The build's script writes to a disk of its own, as for a snapshot
(ADR 0075), so it can write however it likes. Once the script has succeeded
and its pod has gone, the provisioner starts a Job in the place's namespace.
It runs the worker's image as runs' user, with the disk mounted read-only and
the bucket's directory for Iki writable, and runs
`iki-worker publish /iki/staging /iki/shared <directory>`. The version's
directory is `<path>/<plugin.environment>/<volume>/<build HRID>`, as in ADR 0077.

The worker checks everything on the disk first. A symbolic link, device or
anything else a bucket can't hold fails the build before anything is copied,
saying which. Then it copies each file once, in order, in large writes, and
waits for each upload to finish (`fsync`). It checks each copy's size. A file
already there at the same size is one an earlier try copied, so a retried Job
carries on where the last left off. Mountpoint shows a file only once all of
it has been uploaded, and S3 checks each part's checksum.

Once the Job succeeds, the version is ready and the disk is deleted. If it
fails (it's tried four times, for up to six hours), the build fails. The
failure says which Job's log tells why, which Kubernetes keeps for ten
minutes. While the copy is running, the build isn't timed out for its
snapshots: the Job's own deadline bounds it.

**Runs and removal work as on a shared file system.** Run pods mount the
claim read-only, with the version's directory as the `subPath`. A version
that isn't kept is removed by ADR 0077's removal Job, which waits for a copy
still running. The version is recorded before anything is copied, so a failed
build's partial copy is removed too. With nothing left under a prefix, the
bucket shows no directory, so the worker treats a directory that disappears
while it's being removed as removed.

**The Volumes page says what's happening.** A version in a bucket is shown as
`bucket` (API `kind`), "being written, then copied" until it's ready. A build
whose script has succeeded says its output is being copied into the bucket.

## Consequences

- Setup scripts are the same whichever strategy a template picks: they write
  to a disk.
- A build briefly needs a disk as large as its volume, and the copy adds time
  that grows with the data (Mountpoint writes several GB/s from a large
  instance; a few hundred MB/s is common).
- Every run in a place reads the same copy, as on a shared file system.
  Mountpoint caches nothing by default, so a run reading the same data many
  times can set a cache on the PersistentVolume.
- Data written as many small files suits a bucket badly. The template's
  author picks `shared_filesystem` or `clone_from_snapshot` for that.
- The dev cluster has a third namespace for runs, `iki-runs-bucket`, with a
  host-path volume standing in for the bucket. Its end-to-end checks fill a
  disk, copy it, mount the copy in a run, and remove it. Mountpoint itself
  isn't exercised there: the worker's copy keeps to what Mountpoint allows
  (each file written once, in order, `fsync` before close).
- On `aws_ec2`, `object_mount` isn't yet filled by setup builds; a template
  can't yet name a bucket there.

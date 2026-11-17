# 0109. Google Cloud natively: Cloud Storage's own APIs, and the VM tier on Compute Engine

- Status: Accepted
- Date: 2026-11-17
- Refines: [0036](0036-files-content-addressed-uploads.md), [0068](0068-runs-keep-artifacts-for-a-while.md), [0074](0074-vm-tier-runs-get-cloud-machines-of-their-own.md), [0076](0076-vm-tier-volumes-come-from-ebs-snapshots.md), [0107](0107-reference-gcp-infrastructure-in-opentofu.md)

## Context

ADR 0107 built Google Cloud's infrastructure, but the server spoke only S3
to object storage and only EC2 to start the VM tier's machines. Files went
through Cloud Storage's S3-compatible (XML) API with an HMAC key of the
server's service account. That API names versions by `x-goog-generation`,
not `VersionId`, and has no `ListObjectVersions`, so a registered file
couldn't be pinned to its version (ADR 0036) and an expired artifact or
bundle couldn't be deleted for good (ADR 0068, 0096). An HMAC key is a
long-lived secret besides, which Workload Identity exists to do without.
The VM tier couldn't run on Google Cloud at all.

Precedent: GitLab speaks Cloud Storage natively beside S3, with
Application Default Credentials (Workload Identity) and the service
account's `iam.serviceAccounts.signBlob`, rather than a key; Google's own
guidance is to use Workload Identity rather than service account keys.

## Decision

**Files: a second implementation of the storage module, on Google's
client.** `IKI_FILES_BACKEND=gcs` (the chart's `files.backend: gcs`)
selects `iki_server.files.gcs`, on `google-cloud-storage` (Apache-2.0).
Each function of `iki_server.files.storage` that a store does its own way
calls its namesake there, so nothing else in Iki changes, and a contract
suite checks both stores do the same: SeaweedFS for S3, and an in-process
fake of Cloud Storage's JSON and XML APIs and IAM's signBlob that checks
every signed URL's signature. S3 stays the default.

| What Iki uses on S3                                                          | On Cloud Storage                                                                     | The same?                                                                                                                                                                                             |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A version ID, pinned, read, signed for                                       | The object's generation                                                              | Yes: a number that names that version for good                                                                                                                                                        |
| `ListObjectVersions`, deleting each version                                  | A versioned listing, deleting each generation                                        | Yes, but there are no delete markers; a deleted generation stays restorable for the bucket's soft-delete period (30 days in deploy/gcp; 7 to 90), then is gone. S3 has no soft delete                 |
| Multipart uploads: create, presigned part URLs, `ListParts`, complete, abort | The XML API's multipart uploads, with V4-signed part URLs                            | Yes: parts 1 to 10,000, 5 MiB but the last, listed with their ETags, a part sent again replacing the earlier one. No MD5 for the object (Iki checks SHA-256 itself)                                   |
| Presigned URLs (SigV4), signed locally                                       | V4 signed URLs (`GOOG4-RSA-SHA256`), signed by IAM's `signBlob` as the pod's account | Same URLs, at most 7 days; each signature is a call to IAM (an upload's parts 16 at a time; IAM allows 60,000 a minute a project)                                                                     |
| Object Lock (unused by Iki; the bucket owner's)                              | Object Retention Lock, temporary and event-based holds (the bucket owner's)          | Equivalent: either refuses to delete or replace a held version, and the delete fails. Unlocked/Locked are S3's governance/compliance modes; retention up to 100 years. Iki sets none, on either store |
| Iki's retention and legal holds (ADR 0068, 0092)                             | The same, in Iki's database                                                          | Yes                                                                                                                                                                                                   |
| Default encryption (SSE-KMS)                                                 | The bucket's default customer-managed key                                            | Yes: Iki names no key, the bucket's applies                                                                                                                                                           |
| `setup_storage`: CORS, lifecycle, versioning                                 | The same through the JSON API, and the bucket's default key checked                  | Yes                                                                                                                                                                                                   |

Files are recorded as `gs://bucket/key`; `s3://` records written through
the S3-compatible API read from the same bucket. Registered locations are
`gs://`. The pods' Google service account signs as itself:
`iam.serviceAccounts.signBlob` on itself, in a custom role, rather than
Service Account Token Creator, which could also mint its tokens. Storage
and IAM errors become one `StorageError`, which the API answers 503 for, as
it does boto3's. On deploy/gcp the server's account can't change the
bucket, so OpenTofu sets its CORS and lifecycle rules and
`setup_storage --check` checks them. The HMAC key, its secret and the
S3-compatible path go from deploy/gcp.

**The VM tier: a `gcp_gce` provisioner on Compute Engine.** With
`IKI_RUNS_VM_CLOUD=gcp` (the chart's `vmTier.cloud: gcp`), the provisioner
starts each run's machine with `instances.insert` from the `vm-tier`
module's instance template, as its own service account by Workload
Identity. A compute template's `gcp:` block names the machine type, image,
region and zones, Spot and its fallback, GPUs and disks.

| On EC2 (ADR 0074, 0076)                                 | On Compute Engine                                                                                                                  |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Tagged with the deployment; IAM conditions on the tag   | Named `<owner>-...`, which IAM conditions match (labels can't be used in them); labelled for billing too                           |
| A client token per launch                               | A request ID per try, so a retry never starts two machines                                                                         |
| Launch template, IMDSv2, no public IP                   | Instance template, Shielded VM, OS Login, no serial console or project SSH keys, no access config (no public IP)                   |
| User data                                               | `startup-script` metadata, the same script (on Container-Optimized OS, iki-worker lives under `/var/lib/iki`)                      |
| The spot interruption notice                            | `instance/preempted` in the metadata server, watched by `iki-worker host`; a zone's operations say a stopped machine was preempted |
| Capacity and quota errors move to the next zone or type | The same, for `ZONE_RESOURCE_POOL_EXHAUSTED`, `QUOTA_EXCEEDED` and their kin; permission and invalid errors refuse                 |
| A setup build's EBS volumes, snapshotted                | A setup build's persistent disks, kept when the machine goes, each made an image                                                   |
| A run's volume from the snapshot, read-only             | A run's disk from the image, mounted read-only, deleted with the machine                                                           |

Volumes come from images, not snapshots: Compute Engine makes a disk from
one snapshot in a zone at most six times an hour, and Google's guidance for
many disks is an image. Fast snapshot restore has no equivalent, and the
spec refuses it, provisioned IOPS, shared file systems and buckets for
`gcp_gce`. Images are named for the deployment, encrypted with its
`vm-disks` key and labelled with the build and volume; their request ID is
fixed per build and volume, so asking twice makes one.

The provisioner's role changes only instances, disks and images named
`<owner>-`; what it reads (lists, operations, public images, the template)
isn't limited by name, because Compute Engine can't limit it.

## Consequences

- deploy/gcp holds no long-lived credential for files: nothing to rotate or
  revoke, and fencing a lost region is disabling its server's service
  account. Each signed URL is a call to IAM, so an upload's part URLs take
  longer to make than S3's.
- On Cloud Storage, deleting a file for good leaves it restorable for the
  bucket's soft-delete period. Under GxP that's a safeguard (an accidental
  purge can be undone); a purge that must be immediate needs soft delete
  off on that bucket.
- Object Retention Lock and holds are the bucket owner's, as Object Lock is
  on S3: Iki sets none, and a delete they refuse fails as S3's would. Iki's
  own retention and legal holds depend on neither.
- On Compute Engine the VM tier's owner starts resource names, so a
  deploy/gcp deployment's name is 3 to 18 lowercase letters, digits and
  hyphens.
- Two stores and two clouds behind the same functions: a change to one
  needs the contract suite, or the fakes, to say what the other does.

## Alternatives considered

- **Keep the S3-compatible API with an HMAC key.** It has no version IDs
  or version listing, so pinning and deleting for good can't work, and it
  needs a secret.
- **Resumable uploads instead of XML multipart.** One session, in order,
  from one client: the web app, workers and the SDK upload parts in
  parallel and resume by part, so all three would change.
- **Service Account Token Creator for signing.** It also lets the account
  mint its own tokens; a role with `signBlob` alone is enough.
- **Snapshots for volumes**, as on EC2: the six-an-hour limit would stall a
  batch of runs on one environment.

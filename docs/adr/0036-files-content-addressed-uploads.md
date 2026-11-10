# 0036. Files: checked uploads straight to object storage, stored once per content

- Status: Accepted
- Date: 2026-11-10
- Refines: [0005](0005-postgres-only-data-infrastructure.md),
  [0007](0007-typed-table-per-type-entity-storage.md),
  [0022](0022-seaweedfs-for-development-object-storage.md)

## Context

Entities carry files: structures, plate reads, spectra, sequencing runs.
Some are a few kilobytes, others hundreds of gigabytes. Pharma and lab
customers need to know a file is exactly the one that was added (ALCOA+:
original and accurate), that it can't change afterwards, and that only
people with access to its project can read it. Their security teams also
expect that data can't be probed from outside a project, and that it leaves
storage only by way of a short-lived, audited path.

## Decision

**Bytes go straight to object storage, never through Iki's web pods.** An
upload opens an S3 multipart upload to a staging object, and the caller
`PUT`s each part to a URL signed for that part alone. The URLs last an hour,
and the caller can ask for fresh ones, so a slow upload can resume. Parts
are 64 MiB, or larger so that no file needs more than 10,000. This is how
S3-backed products, such as GitHub's release assets, take large files: the
web tier stays small, and a lost connection costs one part.

**Iki checks every upload before it becomes a file.** The caller declares
the file's size and SHA-256 up front. When it says the upload is complete,
Iki checks that every part arrived at its size, assembles them, and a task
reads the bytes back and hashes them. A mismatch fails the upload and keeps
nothing. A match moves the bytes to `blobs/sha256/<hash>`. Only the server
writes under that prefix, and only after checking, so no signed URL can
place bytes there.

**Content is stored once, but every upload sends its bytes.** If the same
content is already stored, the checked copy is dropped and the existing
object is used. The caller can't skip sending the bytes by naming a hash
Iki already holds. That shortcut would let anyone learn whether a file
exists in a project they can't see, by asking about its hash.

**A blob is content; a file is what someone added.** `Blob` holds the hash,
size and location. `File` holds the name, the media type, the project it was
added to, who added it and when. Neither ever changes: the app's database
role may only add and read them. Two files can share a blob, so the same
bytes can carry different names. Adding a file is audited.

**Files are read through their project.** Whoever can read the project a
file was added to can read the file, and nobody else learns it exists (404).
An upload is visible only to the person making it.

**Downloads are short-lived redirects.** `GET /files/{id}/content` checks
access and redirects to a URL signed for five minutes. That URL makes the
browser save the file under its name with its media type, rather than
display it, since the bytes are whatever was uploaded. They are served from
the store's own origin, not Iki's.

**Unfinished uploads are cleaned up.** An upload left unfinished for 24
hours is cancelled by the hourly maintenance job, which aborts the multipart
upload and deletes what arrived.

**Configuration.** An S3 bucket, or an S3-compatible store through an
endpoint URL, with an optional key prefix so deployments can share a bucket.
On AWS, the pods' IAM role grants access, so no keys are stored. Signed URLs
can name a public endpoint that differs from the one the pods use. Pages may
connect to the store's origin for uploads (`connect-src`), and nowhere else.
Only web pods, task runners and maintenance may reach the store (network
policy). Without a bucket, the files API answers 503.

## Consequences

- Positive: files of any size, without load on the web pods. Uploads resume
  part by part.
- Positive: every stored byte is known to match its SHA-256, and stored
  content can't change.
- Positive: duplicates cost no storage, and reveal nothing.
- Negative: a file is ready only after Iki has read it back, which for a
  100 GiB file takes minutes. The upload says `checking` meanwhile, and
  `upload.finished` arrives on the event stream when it's done.
- Negative: a client must hash the file before uploading it. Iki's web app
  and SDK do this.
- Negative: browsers send parts to the store directly, so the bucket needs a
  CORS rule allowing `PUT` from Iki's address. `iki-server setup_storage`
  adds it, with lifecycle rules for abandoned uploads, or reports what's
  missing (`--check`) where the bucket is managed elsewhere.

## Alternatives considered

- **Uploading through Iki.** Rejected: large files would tie up web pods and
  their memory, and hit request time limits.
- **Trusting S3's per-part checksums.** Rejected for now: S3 has no
  full-object SHA-256 for multipart uploads, and compatible stores differ in
  checksum support (ADR 0022). Reading the bytes back works everywhere.
- **Skipping the upload when the hash is known.** Rejected: it reveals
  whether content exists to anyone who can compute a hash.
- **Keeping the name and media type on the blob.** Rejected: the same bytes
  can be added under different names and types.

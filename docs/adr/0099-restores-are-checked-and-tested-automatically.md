# 0099. Restores are checked, and tested automatically

- Status: Accepted
- Date: 2026-11-16
- Refines: [0097](0097-reference-aws-infrastructure-in-opentofu.md)

## Context

A backup is only as good as the last restore that worked. ADR 0097 copies
the database's backups and every version of the files to a second region,
and replicates the Secrets. A restore can still come back broken, without
anyone noticing until people use it. Three ways:

- **The schema** is behind the code that runs it.
- **The encryption keys** restored aren't the ones the values were
  encrypted with. Identity providers' and webhooks' secrets then can't be
  read.
- **Files** that the records name aren't in the bucket Iki reads: a
  replication still catching up, a lifecycle rule, the wrong bucket.

GxP expects a restore to be verified, and the procedure tested on a
schedule (EU GMP Annex 11 §7.2; 21 CFR 11.10(c)).

## Decision

**`iki-server check_restore`** says whether a restored deployment is whole,
and exits 1, naming what, if it isn't:

- every migration the code has is applied;
- every encrypted value (`core.crypto.ENCRYPTED`) decrypts with the keys it
  has;
- every file's contents, and every run artifact whose retention hasn't
  ended, is in storage at its recorded size. `--hashes` reads each one and
  compares its SHA-256, `--sample N` checks N of each kind chosen at
  random, and `--external` checks files registered from other buckets too.

It also reports what the database holds (people, projects, entities,
revisions, files, runs, audit events) and when its newest change is. Every
change is audited, so the newest audit event says how far the data
reaches, which measures the recovery point. `--json` prints the report for
a script to compare with the deployment the backup came from.

**A restore elsewhere reads the replicas through bucket aliases.** Each
file's record names its bucket (`s3://iki-files-use1/…`). S3 bucket names
are global, so the replica in the DR region has another name, and a
deployment restored there would read the lost region's bucket.
`files.bucketAliases` (`IKI_FILES_BUCKET_ALIASES`) maps a bucket's old name
to the one its objects are in now. Iki resolves every stored location
through it, for downloads, runs' inputs, imports and this check. Version
IDs survive replication, so a file pinned to a version still resolves. No
record is rewritten: the tables stay append-only, the audit trail names
the bucket as it was, and failing back means removing the alias. The same
works for a lab's own bucket, replicated elsewhere.

**The restore test runs on every change**, in CI's dev cluster job, after
the end-to-end checks have made data (`deploy/dev/restore-test.sh`). It:

1. checks the live deployment;
2. backs the database up with `pg_dump`;
3. restores it into a new database;
4. checks the restore with every file's size and a sample's SHA-256;
5. fails if the restore holds less than the live deployment did when the
   backup began;
6. drops the restored database.

Production runs `check_restore` after every restore, and the DR runbook
has it on its checklist. A quarterly restore drill, timed against the RTO,
is the operator's.

## Consequences

- A restore that loses data, or can't read its secrets, fails the check
  before anyone signs in.
- Checking every file's size is a HEAD request per file. A large deployment
  samples, or runs the full check overnight.
- The dev test proves that the backup format, the restore and the check fit
  together, at dev scale. The production restore is RDS's own, from the
  replicated automated backups; the runbook covers it.

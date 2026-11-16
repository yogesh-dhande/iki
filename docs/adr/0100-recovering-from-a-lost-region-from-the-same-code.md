# 0100. Recovering from a lost region, from the same code

- Status: Accepted
- Date: 2026-11-16
- Refines: [0097](0097-reference-aws-infrastructure-in-opentofu.md), [0099](0099-restores-are-checked-and-tested-automatically.md)

## Context

ADR 0097 copies the database's backups, every version of the files, and
the secrets to a DR region, and promises recovery from losing a region
within 4 hours, from the same code. The production root could only make a
new, empty deployment: run in the DR region, it would make an empty
database, a new bucket and new secrets, beside the copies that matter.
Three facts shape a recovery:

- Bucket and IAM role names are global, and the lost deployment's still
  exist, so a recovered deployment can't reuse them.
- Runs' artifacts are found in the current files bucket by key, with no
  stored bucket. A new, empty bucket would lose every artifact.
- The lost deployment's state is in the lost region's bucket.

## Decision

**The production root takes a `recovery` variable**: the lost
deployment's name, its database's automated backups copied here, and
optionally a moment to restore to. Applied under a new name (such as
`iki-production-r1`), in the DR region, with a new state:

- **The database** is restored from the backups
  (`restore_to_point_in_time`), to the latest they reach unless told a
  moment. It's as protected as a new one: Multi-AZ, encrypted with its own
  key, deletion protected, its own backups copied to its own DR region. A
  later apply ignores how it began, so dropping `recovery` can't replace
  it.
- **The files bucket is the lost deployment's copy**, adopted rather than
  made: it already holds every file and artifact, at the same version IDs.
  The recovered deployment's state owns its settings from then on:
  versions, encryption, TLS only, no public access, access logging, and
  replication to the recovered deployment's own DR region. New files are
  encrypted with the key the copy's files already are.
- **The chart is told the lost bucket's objects are in the copy**
  (`files.bucketAliases`, ADR 0099), so records naming it resolve and none
  is rewritten.
- **Secrets are copied from their replicas** by
  `scripts/recover-secrets.sh`, Secrets Manager to Secrets Manager. The
  database's URIs are given the new host, and the roles' passwords still
  hold because the roles came with the restore. The same keys decrypt the
  same values, which `check_restore` confirms.

[`docs/disaster-recovery.md`](../disaster-recovery.md) is the runbook. It
covers fencing the lost deployment, the steps, `check_restore`, switching
DNS, a restore in the same region, and the drills.

## Consequences

- A recovery is the production runbook with three more inputs and one more
  script, all of it tested against a mocked AWS (`tofu test`).
- The recovered deployment has another name, so its IAM roles, buckets and
  secrets differ from the lost one's. Anything outside Iki that names them
  (a lab's bucket policy granting Iki's role) needs the new names.
- `recovery` stays set for as long as the recovered deployment runs.
- What a mocked AWS can't prove (RDS's restore, S3's settings on an adopted
  bucket) the yearly drill does.

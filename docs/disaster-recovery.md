# Disaster recovery

How a production deployment on AWS
([`deploy/aws/production`](../deploy/aws/production)) comes back after
losing data or its region, and how you prove it would.
[ADR 0097](adr/0097-reference-aws-infrastructure-in-opentofu.md) sets the
targets: at most **5 minutes of data lost** (RPO) and Iki serving again
**within 4 hours** (RTO).
[ADR 0099](adr/0099-restores-are-checked-and-tested-automatically.md) and
[ADR 0100](adr/0100-recovering-from-a-lost-region-from-the-same-code.md)
cover the checks and the recovery. On Google Cloud
([`deploy/gcp/production`](../deploy/gcp/production), ADR 0107), the
database's replica is promoted and the dual-region files bucket adopted
instead: its runbook's
[Recovering from a lost region](../deploy/gcp/production/README.md#recovering-from-a-lost-region)
has the steps; `check_restore` and the drills below apply as they are.

Commands run from the repository's root, with the production runbook's
helper: `out() { tofu -chdir=deploy/aws/production output -raw "$1"; }`.

| What                      | Kept                                                      | Copied to the DR region                                |
| ------------------------- | --------------------------------------------------------- | ------------------------------------------------------ |
| The database              | Multi-AZ; point-in-time recovery for 35 days              | Automated backups and transaction logs, minutes behind |
| Files and runs' artifacts | Every version, in a versioned bucket                      | Every version, by S3 replication, seconds behind       |
| Secrets                   | Secrets Manager, with a key of their own                  | Replicas, with a key there                             |
| The infrastructure        | This repository's OpenTofu, and its state in the region's | Rebuilt from the same code (below)                     |

`iki-server check_restore` decides whether a restore is whole, whichever
way it was made. It checks that every migration is applied, every encrypted
value decrypts, and every file and artifact is in storage at its size. It
also says how far the data reaches. Run it before anyone signs in.

## Be ready, before you need it

- **A certificate in the DR region.** ACM certificates are regional: issue
  one there for Iki's host name, and note its ARN.
- **A short DNS TTL** on Iki's host name, such as 60 seconds, so switching
  takes effect quickly.
- **This repository, and your `terraform.tfvars`, somewhere the region's
  loss doesn't take**, such as your Git host.
- **Watch replication.** S3 reports the files' replication lag
  (`ReplicationLatency`, with `replication_time_control`), and RDS the
  backups' (`describe-db-instance-automated-backups`, `RestoreWindow`).
- **A drill every quarter** (below), timed against the RTO.

## Restoring in the same region

For data lost or damaged while the region still stands, such as a mistaken
bulk change or a bad migration:

1. Stop writes: `kubectl -n iki scale deploy --all --replicas=0`.
2. Restore the database to just before the damage, as a new instance
   beside it:

   ```console
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier iki-production \
     --target-db-instance-identifier iki-production-restored \
     --restore-time 2026-11-16T09:41:00Z \
     --db-subnet-group-name iki-production \
     --vpc-security-group-ids "$(out database_security_group_id)" \
     --db-parameter-group-name iki-production-postgres17 \
     --multi-az --no-publicly-accessible --deletion-protection
   ```

3. Swap their names, so the restored one answers at the host name the
   secrets already hold. Rename the damaged one first, and wait for each:

   ```console
   aws rds modify-db-instance --db-instance-identifier iki-production \
     --new-db-instance-identifier iki-production-damaged --apply-immediately
   aws rds modify-db-instance --db-instance-identifier iki-production-restored \
     --new-db-instance-identifier iki-production --apply-immediately
   ```

4. Bring Iki back with the `helm upgrade` you install it with, and run
   `kubectl -n iki exec deploy/iki-web -- iki-server check_restore`. Its
   "Data until" is the last change kept.
5. Hand the restored instance to OpenTofu, which puts back what a restore
   doesn't carry over (monitoring, logs' export, the master password in
   Secrets Manager):

   ```console
   tofu state rm module.database.aws_db_instance.this
   tofu import module.database.aws_db_instance.this iki-production
   tofu apply
   ```

6. Keep the damaged instance while you investigate, then lift its deletion
   protection and delete it.

Files need no restoring: Iki never overwrites a stored object, and every
version is kept.

## Recovering from a lost region

The deployment is stood up again in the DR region from the same code, with
another name, `iki-production-r1` in the examples. Bucket and IAM role
names are global, and the lost deployment's still exist. Its database is
restored from the replicated backups. Its secrets are copied from their
replicas, so the same keys decrypt the same values. Its files bucket is
the lost deployment's replica, which already holds every file and
artifact. The records still name the lost bucket, so the chart is told
the replica is now where its objects are (`files.bucketAliases`).

Keep a record as you go: who decided, when each step started and ended,
and what `check_restore` said. It's your evidence of the recovery.

### 1. Fence the lost deployment

If any of it still answers, stop it, so two deployments never write at
once: `kubectl -n iki scale deploy --all --replicas=0` against its
cluster. When the region is down, do it first thing when it's back, before
it restarts on its own.

### 2. State for the new deployment

The lost deployment's state is in the lost region. The new one's goes in
this region, in a workspace of its own:

```console
export AWS_REGION=eu-central-1    # the DR region
cd deploy/aws/bootstrap
tofu init
tofu workspace new r1
tofu apply -var region=$AWS_REGION -var name=iki-production-r1
tofu output -raw backend_hcl > ../production/backend-r1.hcl
```

### 3. The infrastructure

Find the database's replicated backups, and the latest moment they reach:

```console
aws rds describe-db-instance-automated-backups --db-instance-identifier iki-production \
  --query 'DBInstanceAutomatedBackups[0].[DBInstanceAutomatedBackupsArn,RestoreWindow.LatestTime]'
```

Copy `terraform.tfvars` to `recovery.tfvars`, and change:

```hcl
name            = "iki-production-r1"
region          = "eu-central-1"       # the DR region
dr_region       = "eu-north-1"         # where this deployment's own copies go
zones           = ["eu-central-1a", "eu-central-1b", "eu-central-1c"]
certificate_arn = "arn:aws:acm:eu-central-1:…"

recovery = {
  name                 = "iki-production"   # the lost deployment
  database_backups_arn = "arn:aws:rds:eu-central-1:…:auto-backup:ab-…"
  # restore_time       = "2026-11-16T09:41:00Z"   # unset: the latest it can
}
```

```console
cd deploy/aws/production
tofu init -reconfigure -backend-config=backend-r1.hcl
tofu apply -var-file=recovery.tfvars
```

About 40 minutes, as a first apply is. Keep `recovery` set for as long as
this deployment runs: it's how its state knows the database was restored
and the bucket taken on, not made.

### 4. Secrets and the database's roles

```console
deploy/aws/scripts/recover-secrets.sh iki-production iki-production-r1 "$(out database_address)"
deploy/aws/scripts/create-database-roles.sh iki-production-r1 "$(out database_master_secret_arn)"
out external_secrets | kubectl apply -f -
```

The first copies each secret from its replica into the new deployment's,
pointing the database's at the new host. The second finds the roles
already there, with their passwords, and stores this region's RDS CA.

### 5. The cluster's controllers and Iki

As steps 3 and 5 of the [production runbook](../deploy/aws/production/README.md).
`helm_values` names the replica as the files bucket, with the lost bucket
aliased to it. Don't point DNS at it yet.

### 6. Check it

```console
kubectl -n iki exec deploy/iki-web -- iki-server check_restore --external
```

It must pass. Its "Data until" against the time the region was lost is the
data actually lost: record it. On a large deployment, add `--sample 1000`
to decide quickly, then run it in full afterwards.

### 7. Switch over

Point Iki's host name at the new ALB (`kubectl -n iki get ingress iki-web`).
People sign in as before. Self-hosted workers and those on the VM tier
reconnect by the host name. Tell people what time their work was kept to.

### Failing back

The recovered deployment is production now, with its own copies in its own
DR region. To return to the original region, recover again in the other
direction, in a planned window, with step 1 fencing the current one. The
lost region's old resources stay until you delete them; they're protected
from deletion until you lift that.

## Drills

Every change runs a restore in CI
([`deploy/dev/restore-test.sh`](../deploy/dev/restore-test.sh)): it backs
up the dev cluster's database, restores it into a new one, and fails unless
`check_restore` passes and nothing was lost. Production's procedure is
proven by drills:

- **Quarterly**: restore the database in the same region to a new instance,
  point a staging install of Iki at it, run `check_restore`, and time it.
- **Yearly**: recover into the DR region as above, in a separate account
  or with another name, short of switching DNS, and time it against the
  RTO.

Record each drill's date, timings, `check_restore`'s report and what you
changed after it, for your validation file.

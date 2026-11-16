# Iki on AWS

Reference OpenTofu modules for running Iki in your own AWS account
([ADR 0097](../../docs/adr/0097-reference-aws-infrastructure-in-opentofu.md)).
They use the AWS provider alone, no third-party modules, so your platform
team can read and qualify every resource they make.

| Module                         | What it makes                                                                                                              |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| [`network`](modules/network)   | A VPC over two to four zones: public, private and database subnets, NAT gateways, flow logs, endpoints                     |
| [`cluster`](modules/cluster)   | EKS with private Bottlerocket nodes: a system pool, and dedicated pools for runs and GPUs                                  |
| [`database`](modules/database) | RDS PostgreSQL, Multi-AZ, encrypted, TLS only, pgaudit, 35 days of point-in-time recovery, backups copied to the DR region |
| [`storage`](modules/storage)   | The files bucket: versioned, encrypted, private, TLS only, access logged, every version replicated to the DR region        |

Roles for Iki's pods, the VM tier, secrets, and a production deployment
that puts them together come next (roadmap step 48).

The database and storage modules take two AWS providers: the default one,
and `aws.dr` for the disaster-recovery region their copies go to.

## What they promise

- Nothing has a public address but load balancers and NAT gateways. The
  Kubernetes API is private unless you list networks that may reach it.
- Each purpose has its own customer-managed KMS key, rotated yearly.
- Logs are kept: VPC flow logs and every EKS control-plane log, encrypted.
- Pods can't take a node's role (IMDSv2, one hop), and a node's role can
  only join the cluster and pull images.
- NetworkPolicies are enforced (the VPC CNI's network policy agent).
- The database reaches back 35 days to any minute, and its backups are
  copied to the DR region as they're made. Its master password lives in
  Secrets Manager, never in OpenTofu's state.
- Every version of every file is replicated to the DR region.
- Plugins' code runs on its own nodes. Pools other than `system` are
  tainted `iki.ikisci.org/pool=<pool>:NoSchedule`. Give backend workers
  and compute templates the pool's node selector and toleration, which the
  cluster module's `node_pools` output lists.

## Checking them

```console
make check-infra
```

It runs `tofu fmt`, `tofu validate` and `tofu test` for each module, then
Checkov's AWS checks. The tests run against a mocked AWS provider, so they
need no account or credentials: they check what would be applied (a
database subnet has no route out, the API isn't open to the internet), not
that AWS accepts it. Each check Checkov skips says why, in the code.

The AWS provider is pinned in each module's `.terraform.lock.hcl`, with
checksums for Linux and macOS.

## What to know before you rely on them

- **Expired files stay in the DR copy.** Iki deletes an expired artifact
  or bundle by deleting every version of it (ADR 0068, 0096). S3 doesn't
  replicate deleting a version, by design, so the copy in the DR region
  keeps it. Where an expiry must reach every copy, delete it there too.
- **S3 access logs use S3-managed keys.** S3 delivers access logs only to
  buckets encrypted that way; everything else has a key of its own.

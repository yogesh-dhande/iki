# 0097. Reference AWS infrastructure in OpenTofu

- Status: Accepted
- Date: 2026-11-16
- Refines: [0014](0014-kubernetes-only-deployment.md)

## Context

ADR 0014 promises reference OpenTofu modules for AWS: the VPC, EKS with CPU
and GPU nodes, RDS Postgres Multi-AZ with point-in-time recovery, versioned
and encrypted S3, KMS, roles for Iki's pods, the VM tier's instance profile,
optional FSx, cross-region backups and External Secrets. Production must
recover from losing a region with at most 5 minutes of data lost (RPO) and
within 4 hours (RTO).

The readers are a pharma company's platform team. They run their own
deployment (ADR 0002), audit what they apply, and validate it (GAMP 5
treats infrastructure as configuration to be qualified). They need code
they can read end to end and change, not a black box.

Precedent:

- GitLab's reference architectures and its GitLab Environment Toolkit,
  Sourcegraph's and Mattermost's Terraform for AWS: each vendor ships
  modules for its own product, with production values for its chart.
- AWS's EKS Best Practices guide and Security Hub's foundational controls
  (CIS) set the bar: private nodes, envelope encryption of Kubernetes
  Secrets, IMDSv2, encrypted everything, logs on, nothing public by
  default.
- terraform-aws-modules (vpc, eks, rds) are the community's building
  blocks: broad, and so thousands of lines and many inputs each.

## Decision

**Modules of our own, on the AWS provider alone.** `deploy/aws/modules/`
holds one module per concern, each a few hundred lines Iki's tests cover:

| Module     | What it makes                                                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `network`  | A VPC over two to four zones: public, private and database subnets, a NAT gateway per zone, flow logs, VPC endpoints                             |
| `cluster`  | EKS with private nodes: a system pool, and dedicated pools for runs and GPUs; Kubernetes Secrets encrypted with KMS, control-plane logs, add-ons |
| `database` | RDS PostgreSQL, Multi-AZ, encrypted, 35 days of point-in-time recovery, copied to the DR region                                                  |
| `storage`  | The files bucket: versioned, encrypted with KMS, private, TLS only, replicated to the DR region                                                  |
| `access`   | An IAM role per pod that calls AWS, bound by EKS Pod Identity, each allowed only what it uses                                                    |
| `vm-tier`  | The VM tier's instance profile, disk key and security group (ADR 0074)                                                                           |
| `secrets`  | Secrets Manager entries for Iki's secrets, read into the cluster by External Secrets                                                             |
| `fsx`      | Optional FSx for Lustre, root squashed, linked to a bucket, for large reference data such as AlphaFold's                                         |

`deploy/aws/production/` composes them into one deployment: a starting
point the platform team copies, with the chart's production values beside
it. We use no third-party modules. The community's are good, but each
brings far more than Iki uses into what a team must review and qualify,
and a new release of one changes what is applied. The AWS provider is
pinned, and its checksums are in the committed lock file.

**Secure by default, as AWS's foundational controls ask.**

- Nodes and the database have no public address. The EKS API is private
  unless the team lists networks that may reach it.
- One customer-managed KMS key per purpose, rotated yearly: Kubernetes
  Secrets, the database, files, logs and backups. Nothing uses AWS's
  default keys, except S3's access logs, which S3 delivers only to buckets
  with S3-managed keys.
- Nodes require IMDSv2 with a hop limit of 1, so pods can't take the
  node's role, and their disks are encrypted.
- The VPC CNI enforces NetworkPolicies, which the chart relies on.
- Plugins' code runs on nodes of its own. Besides the `system` pool, which
  runs Iki and the cluster's controllers, each pool (`runs`, `gpu`) is
  tainted, and only backend workers and run pods tolerate it. A container
  escape in a plugin's image lands on a node holding none of Iki's Secrets.
- Nodes run Bottlerocket, a minimal read-only OS with SELinux enforcing,
  and their role can only join the cluster and pull images.
- Logs are on: VPC flow logs, every EKS control-plane log, Postgres
  connections and DDL through pgaudit, S3 access logs.
- Deletion protection on the database and the cluster's keys; a final
  snapshot on destroy.

**Pods get AWS credentials through EKS Pod Identity**, not IAM roles for
service accounts. It needs no OIDC provider and no annotations in the
chart, and a role's trust policy names only the Pod Identity service.
Each pod that calls AWS gets its own role: web pods, task runners and the
maintenance job reach the files bucket under Iki's prefix and its key,
each through a role of its own so CloudTrail says which did what; the
provisioner may start and stop only machines, and make and delete only
disks and snapshots, tagged with its deployment (ADR 0074, 0076);
External Secrets reads only this deployment's secrets. Backend workers and
run pods get no role at all (ADR 0043, 0061).

**Secrets stay out of OpenTofu's state.** The `secrets` module creates the
Secrets Manager entries, encrypted with their own key, but not their
values. A script (`deploy/aws/scripts/put-secrets.sh`) generates them
(Django's secret key, the encryption keys, the bundle signing key, the
database roles' passwords) and writes them straight to Secrets Manager;
`make check-infra` fails if any module writes a secret's value. Secrets
are replicated to the DR region with a key there. RDS
keeps its master password in Secrets Manager itself and rotates it. The
state is still encrypted, with OpenTofu's state encryption and a KMS key,
and stored in a versioned S3 bucket with S3 locking.

**Recovering from a lost region.** RDS replicates its automated backups to
the DR region, so the database can be restored there to any point in the
last 7 days, minutes behind. The files bucket replicates every version
there. Deleting a version doesn't replicate, by S3's design, so an
artifact Iki expires stays in the copy unless it's deleted there too.
Restoring takes a new cluster and a Helm install from the same code. The
disaster-recovery runbook and its automated restore test come in step 49.

**Tested without an AWS account.** `tofu test` runs each module against a
mocked provider and asserts the properties above: a public subnet never
holds nodes, the database is Multi-AZ and encrypted, the bucket blocks
public access, a pod's role can't reach another deployment's bucket. CI
also runs `tofu fmt`, `tofu validate`, and Checkov's AWS checks, with each
skipped check given a reason in the code.

## Consequences

- Positive:
  - A platform team can read every resource Iki asks them to apply, and
    changes to it come only with Iki's own releases.
  - The security properties a validation needs are asserted by tests, not
    just written down.
  - Production recovers from losing a region, from the same code.
- Negative / trade-offs:
  - We maintain modules the community already maintains, and follow AWS's
    changes ourselves.
  - Teams with their own landing zone (VPCs, accounts, keys made centrally)
    use some modules and skip others: each takes the IDs it needs, rather
    than making them.
  - Mocked tests check what would be applied, not that AWS accepts it. Only
    applying it in a real account proves that.

## Alternatives considered

- **terraform-aws-modules for the VPC, EKS and RDS.** Less to write, but
  more to review and qualify, and an upgrade of one could change
  production. Teams that use them can still feed their outputs to our
  `access`, `secrets` and `vm-tier` modules.
- **IAM roles for service accounts (IRSA).** It works, and the chart still
  accepts its annotations. Pod Identity is simpler, and is AWS's current
  recommendation for EKS.
- **EKS Auto Mode.** Fewer parts to run, but less control over node images
  and GPU drivers, and harder to reason about when validating.
- **Generating secrets in OpenTofu.** Convenient, but they would sit in
  the state, readable by anyone who can read it.

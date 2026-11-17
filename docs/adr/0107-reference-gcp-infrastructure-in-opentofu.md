# 0107. Reference Google Cloud infrastructure in OpenTofu

- Status: Accepted
- Date: 2026-11-16
- Refines: [0014](0014-kubernetes-only-deployment.md), [0097](0097-reference-aws-infrastructure-in-opentofu.md)

## Context

ADR 0097 gave AWS reference OpenTofu modules. Google Cloud is where Iki is
first tested in a real cloud, so it needs the same: a production
deployment a pharma company's platform team can read end to end, apply in
its own project, and validate (GAMP 5), with the same targets of at most 5
minutes of data lost and Iki serving again within 4 hours after losing a
region.

Google Cloud offers different building blocks from AWS's for the same
ends. Cloud Storage and Secret Manager can keep data in two regions at
once instead of copying it there; Cloud SQL keeps a cross-region replica;
GKE has Workload Identity rather than EKS Pod Identity, and Dataplane V2
enforces NetworkPolicies. Iki's server, though, speaks only S3 to object
storage and only EC2 to start the VM tier's machines.

Precedent: GitLab's and Sourcegraph's reference architectures ship
Terraform for GCP beside AWS's; Google's GKE hardening guide and the CIS
Google Cloud Foundations Benchmark set the bar (private nodes and API,
Workload Identity, shielded nodes, customer-managed keys rotated within 90
days, audit logs on); terraform-google-modules is the community's
equivalent of terraform-aws-modules.

## Decision

**The same shape as on AWS: modules of our own, on the Google provider
alone.** `deploy/gcp/modules/` holds one module per concern,
`deploy/gcp/production/` composes them, and `deploy/gcp/bootstrap/` turns
on the APIs and makes the state's bucket and key. The provider is pinned
(8.x) with its checksums in each committed lock file.

| Module              | What it makes                                                                                                                    |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `kms`               | A key per purpose, in the region, the DR region and the files' multi-region, rotated every 90 days, granted to Google's agents   |
| `network`           | A VPC with private subnets, Cloud NAT, Google's APIs reached privately, Private Service Access, firewalls that deny by default   |
| `logs`              | An encrypted log bucket kept a year, with audit, control-plane, flow, firewall and database logs, and Data Access logs turned on |
| `cluster`           | Regional GKE: private nodes, Dataplane V2, Workload Identity, shielded COS nodes, a system pool and tainted runs and GPU pools   |
| `database`          | Cloud SQL for PostgreSQL: regional, private IP only, TLS only, pgaudit, IAM sign-in, 35 days of PITR, a replica in the DR region |
| `storage`           | The files bucket: a dual-region, versioned, customer-managed keys only, public access prevented, soft delete, object retention   |
| `secrets`           | Secret Manager entries, a replica in each region with its own key, no values                                                     |
| `access`            | Google service accounts for the server and the provisioner, bound by Workload Identity, and External Secrets' read access        |
| `artifact-registry` | Docker repositories for the worker's and plugins' images: immutable tags, vulnerability scanning, CMEK                           |
| `vm-tier`           | The VM tier's service account, a shielded instance template with no public address, and egress firewalls                         |
| `filestore`         | Optional Filestore for shared reference data (ADR 0077): regional, CMEK, root squashed                                           |

**Secure by default, as CIS and GKE's hardening guide ask.**

- Nothing has a public address but the load balancer and Cloud NAT, at
  fixed addresses. The Kubernetes API is private unless the team lists
  networks or turns on its DNS endpoint, where IAM decides.
- Firewalls deny all ingress and egress, logged, below rules that allow
  what's needed. Google's APIs resolve to `private.googleapis.com`.
- A customer-managed key per purpose, rotated every 90 days, protected
  from deletion. Cloud Storage refuses to encrypt the files with Google's
  default keys or a caller's own.
- Pods reach GKE's metadata server, not the node's identity; the node's
  service account can only log, report metrics and pull images.
- Plugins' code runs on tainted pools of its own, as on AWS.
- Cloud SQL logs connections, locks and errors, and pgaudit logs DDL and
  role changes. Cloud Audit Logs' Data Access logs record every read of
  files, secrets, keys and the database's settings.

**Pods get Google credentials through Workload Identity.** The web, task
and maintenance pods share the server's Google service account, the one
annotation the chart takes for them (`serviceAccount.annotations`). Its
access is to the files bucket alone, under Iki's prefix, through an IAM
condition. The provisioner's service account may change only machines,
disks and snapshots whose names start with its deployment's, also through
a condition: Compute Engine's labels can't be used in IAM conditions.
External Secrets reads this deployment's secrets as its own Kubernetes
service account, through Workload Identity Federation's `principal://`
identifier, with no Google service account to impersonate.

**Secrets stay out of OpenTofu's state**, as ADR 0097 decided. On Google
Cloud, three more resources would put a secret there: a Secret Manager
version, an HMAC key and a service account key. A database user's
password would too. `make check-infra` fails if any module uses them. The
scripts set Cloud SQL's `postgres` password through its API and make the
HMAC key with gcloud, both straight into Secret Manager.

**Recovering from a lost region adopts rather than restores.** The files
bucket is a dual-region, so it outlives losing either region and keeps
its name: the records need no bucket alias. Each secret has a replica in
each region. Cloud SQL's cross-region replica is promoted, then imported
into the new deployment (`recovery`), which makes it regional and backed
up. The new deployment runs in the lost one's DR region, with a third
region as its own DR region, since nothing can be made in a region that's
down. The state's bucket and key are multi-regional.

**Iki is served through the GKE Gateway API.** The chart's web Service
takes no annotations, so GKE's Ingress can't be given Cloud Armor or a
TLS policy. OpenTofu reserves the address, makes the Google-managed
certificate, the TLS policy (1.2 or later, modern ciphers) and an optional
Cloud Armor allowlist. It outputs a Gateway, an HTTPRoute and GKE's
policies as manifests to apply. Web pods admit traffic only from Google's
front ends (or the proxy-only subnet, for an internal load balancer).

**Files go through Cloud Storage's S3-compatible API for now**, with an
HMAC key of the server's service account. It works for uploads, downloads
and signed URLs. It doesn't return S3 version IDs, so version pinning and
deleting an expired file for good are lost until the server speaks Cloud
Storage's own API (the README's next steps). OpenTofu sets the CORS and
lifecycle rules `setup_storage` would.

**Built now: what the VM tier will need.** The `vm-tier` module, the
provisioner's identity and Filestore are built and tested. The chart's
`vmTier` stays off until the server has a Compute Engine provisioner and
persistent-disk snapshots (the README's next steps).

**Tested without a Google account,** as on AWS: `tofu test` against a
mocked provider asserts the properties above, the production root's
chart values are checked against a file the chart's tests render, and
Checkov's Google checks run with every skip given a reason. OpenTofu's
tests can't import, so a recovery's imports are checked only for the
names they look for.

## Consequences

- Positive:
  - Iki can be tested in a real cloud on Google Cloud, from code a platform
    team can qualify, with the same promises as on AWS.
  - Recovery after losing a region loses less (a replica, not backups) and
    has fewer steps (nothing restored, no bucket alias).
- Negative / trade-offs:
  - Until the server has a native Cloud Storage backend, files have no
    version pinning or deletion for good, and an HMAC key exists.
  - The VM tier can't run on Google Cloud yet.
  - Each deployment needs a project of its own: GKE's and Compute Engine's
    roles are granted on the project.
  - Some service agents must be made with `gcloud beta` before the first
    apply, since the provider makes them only in its beta.
  - Two sets of reference modules to maintain, and providers to follow.

## Alternatives considered

- **terraform-google-modules** (network, kubernetes-engine, sql-db). As
  with ADR 0097: more to review and qualify than Iki uses.
- **Replicating files to a second bucket in the DR region** (Storage
  Transfer Service), as S3 replication does on AWS. A dual-region keeps
  both copies under one name, with no job to watch, and turbo replication
  bounds how far behind it is.
- **GKE Ingress with a BackendConfig.** It needs annotations on the web
  Service, which the chart doesn't take; the Gateway API is GKE's current
  recommendation besides.
- **Autopilot.** Fewer parts to run, but no dedicated tainted pools for
  plugins' code as Iki has them, and less control over node images.
- **Skipping Filestore.** ADR 0077's shared volumes need an NFS server
  that every node and machine mounts; Filestore is Google's, regional and
  with customer-managed keys, so it's built, and off unless asked for.

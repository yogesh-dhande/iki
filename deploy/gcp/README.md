# Iki on Google Cloud

Reference OpenTofu modules for running Iki in your own Google Cloud project
([ADR 0107](../../docs/adr/0107-reference-gcp-infrastructure-in-opentofu.md)),
the peer of [`deploy/aws`](../aws). They use Google's providers alone
(google, and google-beta in the bootstrap), no third-party modules, so
your platform team can read and qualify every resource they make.

| Module                                                 | What it makes                                                                                                                             |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| [`kms`](modules/kms)                                   | A key for each purpose, in the region, the DR region and the files bucket's multi-region, rotated every 90 days, in software or HSM       |
| [`network`](modules/network)                           | A VPC with private subnets, Cloud NAT at fixed addresses, Google's APIs reached privately, firewalls denying all but what's listed        |
| [`logs`](modules/logs)                                 | A log bucket of the deployment's own, encrypted, kept a year: audit, control-plane, flow, firewall and database logs                      |
| [`cluster`](modules/cluster)                           | Regional GKE with private nodes, Dataplane V2, Workload Identity, shielded COS nodes: a system pool, and tainted pools for runs and GPUs  |
| [`database`](modules/database)                         | Cloud SQL for PostgreSQL, regional, private IP only, TLS only, pgaudit, 35 days of point-in-time recovery, a replica in the DR region     |
| [`storage`](modules/storage)                           | The files bucket: in two regions at once, versioned, customer-managed keys only, private, deletions recoverable for 30 days               |
| [`secrets`](modules/secrets)                           | Secret Manager entries for Iki's secrets, each replica encrypted with its region's key, with no values in OpenTofu's state                |
| [`access`](modules/access)                             | Google service accounts for the pods that call Google, bound by Workload Identity, each allowed only what it uses                         |
| [`artifact-registry`](modules/artifact-registry)       | Docker repositories for the server, worker and plugins' images: tags immutable, scanned for vulnerabilities, encrypted                    |
| [`vm-tier`](modules/vm-tier)                           | The VM tier's service account, a shielded instance template with an encrypted disk and no public address, and its egress firewalls        |
| [`filestore`](modules/filestore)                       | Optional: Filestore for shared reference data, regional, encrypted, root squashed                                                         |
| [`binary-authorization`](modules/binary-authorization) | The project's Binary Authorization policy: only the deployment's repositories and the images named run, the rest refused and audit-logged |

[`production`](production) puts them together into one deployment, with
the chart's production values, and its README is the runbook: from an
empty project to Iki serving. [`bootstrap`](bootstrap) turns on the APIs,
makes the service agents the keys are granted to, and makes the bucket and
key its state is kept in, first.

Where AWS copies to a DR region, Google Cloud mostly keeps both regions at
once: the files bucket is a dual-region, each secret has a replica in
each, and Cloud SQL keeps a cross-region replica. After losing a region,
`production` stands Iki up again in the DR region by adopting the
replica and the same bucket, with `recovery` set
([production's README](production/README.md#recovering-from-a-lost-region)).

## Secrets

OpenTofu makes the Secret Manager entries, never their values.
`scripts/put-secrets.sh` generates the values straight into Secret
Manager, sets Cloud SQL's `postgres` password and makes the server's HMAC
key; `scripts/create-database-roles.sh` creates the database's two roles
with them. Recovering from a lost region, `scripts/recover-secrets.sh`
copies them from the lost deployment's replicas first. None of them
overwrites what exists, so each is safe to run again. External Secrets
then copies the secrets into the cluster, as its own Kubernetes service
account, which the `access` module lets read only these. The production
runbook runs them all.

## What they promise

- Nothing has a public address but the load balancer and Cloud NAT. Nodes
  are private, Cloud SQL has only a private address, and the Kubernetes
  API is private unless you list networks or turn on its DNS endpoint.
- Every firewall rule is listed: ingress and egress are denied, and
  logged, unless a rule allows them. Nodes reach the VPC, Google's APIs
  through `private.googleapis.com`, and HTTPS outside (narrow it in
  `node_egress`).
- Each purpose has its own customer-managed key, rotated every 90 days:
  Kubernetes Secrets, node and machine disks, the database, files,
  secrets, images, Filestore and logs. Keys can't be destroyed by
  OpenTofu, and a destroyed version waits 30 days.
- Logs are kept a year, encrypted: Cloud Audit Logs with Data Access for
  Cloud Storage, Secret Manager, Cloud KMS and Cloud SQL; GKE's
  control-plane logs; VPC flow, firewall and NAT logs; Postgres
  connections and DDL through pgaudit.
- Pods can't take a node's identity (GKE's metadata server), and a node's
  service account can only write logs and metrics and pull images. Nodes
  boot shielded, with secure boot and integrity monitoring.
- NetworkPolicies are enforced (Dataplane V2).
- The database reaches back 35 days to any minute, its backups are kept in
  the DR region, and a replica there follows it. Its `postgres` password
  lives in Secret Manager, never in OpenTofu's state; people sign in with
  IAM.
- Every file is in two regions, every version is kept, and a deleted one
  can be restored for 30 days.
- Each pod that calls Google has its own service account: web, task
  runners and the maintenance job reach only the files bucket under Iki's
  prefix (and read registered locations); the provisioner may change only
  machines, disks and snapshots named for its deployment; External Secrets
  reads only this deployment's secrets. Backend workers and runs get no
  identity.
- Images' tags can't be moved, and every image is scanned as it's pushed.
- The cluster runs only images Binary Authorization allows: the
  deployment's own repositories, Iki's and External Secrets' images, the
  first-party plugins', and any you list (`plugin_images`,
  `extra_images`). GKE's system images are allowed by Google's policy.
  Anything else is refused, and each refusal is in the audit logs.
- Plugins' code runs on its own nodes. Pools other than `system` are
  tainted `iki.ikisci.org/pool=<pool>:NoSchedule`. Give backend workers
  and compute templates the pool's node selector and toleration, which the
  cluster module's `node_pools` output lists.

## Checking them

```console
make check-infra
```

It runs `tofu fmt`, `tofu validate` and `tofu test` for each module and
root, a check that nothing writes a secret's value, then Checkov's Google
checks. The tests run against a mocked Google provider, so they need no
project or credentials: they check what would be applied (the database has
no public address, a pod's service account can't reach another prefix),
not that Google accepts it. Each check Checkov skips says why, in the code.

The Google provider is pinned in each module's `.terraform.lock.hcl`, with
checksums for Linux and macOS.

## What to know before you rely on them

- **Files go through Cloud Storage's S3-compatible API**, with an HMAC key
  of the server's service account, until Iki speaks Cloud Storage's own
  (next steps). Don't run `iki-server setup_storage`: OpenTofu sets its
  CORS and lifecycle rules on the bucket, through Cloud Storage's own API,
  instead. Until then, version pinning and deleting an expired file for
  good (ADR 0068, 0096) don't work, because the S3-compatible API has no
  version IDs.
- **The VM tier has no provisioner yet.** The `vm-tier` module and the
  provisioner's service account are ready for one; the chart's `vmTier`
  stays off (next steps).
- **Give each deployment a project of its own.** GKE's and Compute
  Engine's IAM roles are granted on the project, so two deployments in one
  would administer each other's clusters.
- **Binary Authorization allows by where an image is, not by who signed
  it.** Anyone who can push to an allowed path can run an image there:
  keep `image_writers` to your CI, and pin images by digest. Installing
  something else in the cluster (kube-prometheus-stack, a collector)
  means listing its images in `extra_images` first, and
  `tofu output allowed_images` says what's allowed. In an emergency, a
  pod annotated `alpha.image-policy.k8s.io/break-glass: "true"` starts
  anyway, and the audit logs say so.
- **The bootstrap uses the google-beta provider** for one resource, the
  service agents (`google_project_service_identity`), which the Google
  provider doesn't have. It's pinned, and locked, as the Google provider is.
- **NodeLocal DNSCache is off.** The chart's NetworkPolicies allow DNS to
  kube-dns alone.
- **The web Service takes no annotations**, so GKE's BackendConfig can't
  be used. The production root serves Iki through the Gateway API
  instead: a Gateway, an HTTPRoute, and policies for TLS, Cloud Armor and
  health checks, as manifests you apply.
- **The VM tier's machines reach only the VPC, Google's APIs and NFS.**
  Give them an address for Iki's server inside the VPC, or list more
  networks in `extra_https_cidrs`.
- **Filestore squashes root.** Make `/iki` for runs' user (UID 10001) from
  a machine listed in `root_squash_except_cidrs`, then take it off the
  list. It's built rather than skipped because ADR 0077's shared volumes
  need an NFS server that every node and machine mounts, and Filestore is
  Google's: regional, with customer-managed keys.

## Next steps

These aren't built: each needs the server to change, not only
infrastructure.

- **A native Cloud Storage backend.** `server/src/iki_server/files/storage.py`
  speaks S3 through boto3. Cloud Storage's S3-compatible (XML) API takes
  it, but names versions by `x-goog-generation`, not `VersionId`, and has
  no `ListObjectVersions` or S3-format CORS and lifecycle calls. So
  `head`, `digest_object` and `open_object` lose their version pin,
  `delete_for_good` and `versioned` fail, and `setup_storage` can't
  configure the bucket. The server needs a second implementation of that
  module's functions on `google-cloud-storage`: multipart uploads as
  [XML API multipart uploads](https://cloud.google.com/storage/docs/multipart-uploads)
  with V4-signed part URLs, generations as versions, `list_blobs(versions=True)`
  to delete every generation, and its credentials from Workload Identity
  (the server's service account, already annotated), so the HMAC key and
  its secret go. `gs://` storage URIs, and `files.backend: gcs` in the
  chart, would choose it.
- **A Compute Engine provisioner for the VM tier.** `runs/ec2.py` and
  `runs/ec2_fleet.py` start EC2 instances (ADR 0074). Their GCE twin
  would insert instances from the `vm-tier` module's instance template
  (`regionInstances.bulkInsert`, with `SPOT` provisioning for spot
  templates), named `<deployment>-...` because the provisioner's IAM
  condition matches names (Compute Engine's labels can't be used in IAM
  conditions); pass the worker's start-up in `startup-script` metadata
  instead of user data; and watch `instance/preempted` in the metadata
  server instead of EC2's spot notice, in `iki-worker host`. The chart's
  `vmTier` would take `provider: gce`, and its `cloudApi` egress the
  Compute API through `private.googleapis.com`.
- **Persistent-disk snapshots for volumes.** `runs/ec2_volumes.py` snapshots
  a setup build's EBS disks and restores them on each run's machine (ADR
  0076). On Compute Engine: disks made empty in the build's zone, kept
  when it ends (`autoDelete: false`), then `snapshots.insert` of each,
  encrypted with the `vm-disks` key, labelled with the build and volume;
  a run's machine gets a new disk from the snapshot, attached read-only.
  Fast snapshot restore has no equivalent; instant snapshots or images
  made from the snapshot are the nearest.
- **Cloud Armor and BackendConfig through the chart's Service.** Given a
  new `web.service.annotations` value, for GKE's
  `cloud.google.com/backend-config`, the chart's Ingress could replace the
  Gateway manifests, for teams that prefer one.
- **Binary Authorization by signature.** The policy allows images by
  path. Iki's release workflow already signs its images with cosign
  (keyless, through GitHub's OIDC: `docs/supply-chain.md`), and the chart
  can require those signatures in runs' namespaces through Sigstore's
  policy-controller (`containerTier.imagePolicy`, ADR 0063). The next
  step is requiring them cluster-wide: the policy-controller covering
  Iki's own namespace too, which refuses an unsigned pod; or Binary
  Authorization's check-based platform policies, whose Sigstore signature
  check reports, through continuous validation, any running pod whose
  image Iki's release or the deployment's build didn't sign.

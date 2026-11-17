# Iki's production deployment on Google Cloud

Every module in [`../modules`](../modules) put together, with the chart's
production values beside it ([ADR 0107](../../../docs/adr/0107-reference-gcp-infrastructure-in-opentofu.md)).
Copy this directory and `../modules` into your own repository and change
what you need: it's a starting point, not a module to call.

You need OpenTofu 1.11 or later, the gcloud CLI, kubectl, Helm, jq, curl
and openssl; a project of its own for the deployment, with billing; and
the project's Owner role, or roles covering what it makes, to apply it.

## 1. The APIs, and the state's bucket and key, once

```console
cd deploy/gcp/bootstrap
tofu init
tofu apply -var project_id=acme-iki-production -var location=europe
tofu output -raw backend_hcl > ../production/backend.hcl
tofu output state_kms_key
```

The state's bucket and key are multi-regional (`europe` here: pick the one
holding your region and DR region), so they outlive losing either. Its own
state holds no secrets. Keep it beside the deployment's:
`gcloud storage cp terraform.tfstate gs://<bucket>/bootstrap/terraform.tfstate`.

It also makes the service agents of Cloud SQL, Secret Manager, Artifact
Registry and Filestore, which a project makes only when first asked, and
which the deployment's keys are granted to.

## 2. The infrastructure

```console
cd deploy/gcp/production
cp terraform.tfvars.example terraform.tfvars   # and fill it in
tofu init -backend-config=backend.hcl
tofu apply
```

The state is encrypted by OpenTofu with the bootstrap's key before it's
written, and locked while anyone applies. A first apply takes about 40
minutes, most of it the cluster and the database.

## 3. The cluster's controllers

From here on, run commands from the repository's root, with:

```console
out() { tofu -chdir=deploy/gcp/production output -raw "$1"; }
```

```console
eval "$(out get_credentials)"
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace
```

Pin it with `--version`, to the release you qualified. It needs no
annotation: the `access` module lets its Kubernetes service account
(`external-secrets/external-secrets`) read this deployment's secrets, as
itself, through Workload Identity. GKE brings the rest: the Gateway
controller, the cluster autoscaler, and the CSI drivers.

The cluster starts only images Binary Authorization allows
(`out allowed_images`): this deployment's repositories (`out images`),
Iki's, External Secrets', the first-party plugins', and GKE's own. Before
installing anything else, add its images to `extra_images` and apply;
otherwise GKE refuses its pods, and the audit logs say which image.
A server or worker built with your plugins goes in the `server` or
`worker` repository, pushed by your CI (`image_writers`).

## 4. Secrets and the database's roles, once

```console
eval "$(out put_secrets)"
eval "$(out create_database_roles)"
out external_secrets | kubectl apply -f -
```

The first generates Iki's secrets straight into Secret Manager and sets
Cloud SQL's `postgres` password. The second creates the two database roles with their passwords
(ADR 0025), and stores Cloud SQL's CAs as the `iki-database-ca` Secret.
The third has External Secrets copy each secret into namespace `iki`,
refreshing hourly.

## 5. Iki

```console
out helm_values > deploy/gcp/production/values-gcp.yaml
helm upgrade --install iki deploy/helm/iki --namespace iki \
  -f deploy/gcp/production/values-production.yaml -f deploy/gcp/production/values-gcp.yaml
out gateway | kubectl apply -f -
```

`values-gcp.yaml` is what the infrastructure decided: the bucket, the
Secrets' names, the networks pods may reach. Change those in OpenTofu and
write it again. `values-production.yaml` holds the choices that are yours
(replicas, the egress proxy); pin the image by digest there.

The gateway output is Iki's load balancer: a Gateway at the address
OpenTofu reserved, an HTTPRoute to web pods, and GKE's policies for its
TLS policy (1.2 or later, modern ciphers), its health check and Cloud
Armor. Point your DNS name at `out load_balancer_address`; Google issues
the certificate once it resolves there, which takes up to an hour. With
`internal`, the load balancer serves the certificate you uploaded, and a
private zone already names it inside the VPC (`internal_dns`), so backend
workers and the VM tier's machines reach it; point your own network's DNS
at the address too. Then open the one-time setup link
`kubectl -n iki exec deploy/iki-web -- iki-server setup_link` prints.

OpenTofu set the bucket's CORS and lifecycle rules, which the server's
account can't change. `kubectl -n iki exec deploy/iki-web -- iki-server setup_storage --check`
checks them, with the bucket's versioning and default key.

## 6. Metrics, dashboards and alerts

Iki sends traces and metrics over OTLP to your collector: set
`otel.endpoint` in `values-production.yaml` (an OpenTelemetry Collector,
which can write to Google Cloud Managed Service for Prometheus and Cloud
Trace). With kube-prometheus-stack in the cluster, also set:

```yaml
monitoring:
  dashboards: { enabled: true } # for Grafana's dashboard sidecar
  alerts: { enabled: true, labels: { release: kube-prometheus-stack } }
```

Otherwise import `deploy/helm/iki/files/dashboards/iki.json` into Grafana
and load `deploy/helm/iki/files/alerts.yaml` as a rules file. ADR 0098 lists
the metrics and what each alert means.

## Recovering

Within the region, [the disaster-recovery runbook](../../../docs/disaster-recovery.md)'s
restore and `iki-server check_restore` apply as they are; on Google Cloud
the database restores to any minute of the last 35 days with
`gcloud sql instances clone --point-in-time`.

### Recovering from a lost region

The deployment is stood up again in its DR region from the same code,
under another name (`iki-production-r1` here), with a third region as its
DR region, since nothing can be made in the lost one. The lost
deployment's service accounts, keys and secrets still exist, so names
can't be reused. Nothing is restored: the database is its replica,
promoted, minutes behind at most; the files bucket is the same bucket,
which is in both regions; the secrets are copied from their replicas, so
the same keys decrypt the same values. Keep a record as you go: who
decided, when each step started and ended, and what `check_restore` said.

1. **Fence the lost deployment**, so two deployments never write at once:
   if its cluster answers, `kubectl -n iki scale deploy --all --replicas=0`;
   and disable its server's service account, which is how its pods reach
   the bucket, wherever they run:
   `gcloud iam service-accounts disable <lost deployment>-server@<project>.iam.gserviceaccount.com`.
2. **Promote the replica** into a database of its own:
   `gcloud sql instances promote-replica iki-production-dr`.
3. **State for the new deployment**, in the same multi-regional bucket,
   in a workspace of its own: `tofu init -backend-config=backend.hcl`,
   then `tofu workspace new r1`.
4. **The infrastructure**, with `name = "iki-production-r1"`, `region`
   the lost deployment's DR region, `dr_region` a third, and
   `recovery = { name = "iki-production", lost_region = "<the lost one>" }`
   in `terraform.tfvars` (the example shows it). `tofu apply` imports the
   promoted database and the bucket, rather than making new ones, then
   makes the database regional and backed up as a new one would be.
5. **Secrets**:
   `deploy/gcp/scripts/recover-secrets.sh <project> iki-production iki-production-r1 "$(out database_address)"`,
   then `eval "$(out put_secrets)"`, which sets a new `postgres` password. The database's roles came with it.
6. **The cluster's controllers and Iki**: steps 3 and 5 above, with the
   CA Secret from `create_database_roles`. The records still name the same
   bucket, so the chart needs no `files.bucketAliases`.
7. **Check it**: `kubectl -n iki exec deploy/iki-web -- iki-server check_restore`,
   before anyone signs in. Then point your DNS name at the new address.

## Tearing it down

For a test deployment, or one decommissioned once its records are kept elsewhere:

```console
deploy/gcp/scripts/teardown.sh acme-iki-production iki-production --bootstrap
```

It uninstalls Iki and External Secrets first, so the load balancer GKE made for the
Gateway goes, and deletes any machines, disks and images the VM tier left. Then it
turns `deletion_protection` off (the cluster, the database and the secrets), empties
the files bucket, with soft delete off so nothing waits there, destroys this root, and
deletes the database's retained backups. Cloud KMS keys and key rings can't be deleted:
OpenTofu forgets them, and the script stops each key's rotation and schedules every
version's destruction, 30 days out. `--bootstrap` then does the same for the
bootstrap's state bucket and key, keeping the APIs on. Cloud Logging deletes the log
bucket after 7 days, and IAM the custom roles; their names can't be reused until then.

## Checking it

`make check-infra` plans this root against a mocked Google provider and
checks the chart's values it writes. The chart's own tests render the
chart with those values (`tests/helm-values.yaml`) and
`values-production.yaml`, so a change to either that the chart can't take
fails there. OpenTofu's tests can't import, so a recovery's imports are
checked only for the names they expect; the database and storage modules'
tests check adopting what they import.

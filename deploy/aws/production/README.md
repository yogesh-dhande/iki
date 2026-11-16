# Iki's production deployment on AWS

Every module in [`../modules`](../modules) put together, with the chart's
production values beside it ([ADR 0097](../../../docs/adr/0097-reference-aws-infrastructure-in-opentofu.md)).
Copy this directory and `../modules` into your own repository and change
what you need: it's a starting point, not a module to call.

You need OpenTofu 1.11 or later, the AWS CLI, kubectl, Helm, jq and curl,
credentials for the account, and an ACM certificate for Iki's host name.

## 1. The state's bucket and key, once

```console
cd deploy/aws/bootstrap
tofu init
tofu apply -var region=eu-west-1
tofu output -raw backend_hcl > ../production/backend.hcl
tofu output state_kms_key_arn
```

Its own state holds no secrets. Keep it beside the deployment's:
`aws s3 cp terraform.tfstate s3://<bucket>/bootstrap/terraform.tfstate`.

## 2. The infrastructure

```console
cd deploy/aws/production
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
out() { tofu -chdir=deploy/aws/production output -raw "$1"; }
```

```console
aws eks update-kubeconfig --name "$(out cluster_name)"
helm repo add eks https://aws.github.io/eks-charts
helm repo add external-secrets https://charts.external-secrets.io
helm repo add autoscaler https://kubernetes.github.io/autoscaler

helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system --set clusterName="$(out cluster_name)" \
  --set region="$(out region)" --set vpcId="$(out vpc_id)" \
  --set serviceAccount.name=aws-load-balancer-controller
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace
helm install cluster-autoscaler autoscaler/cluster-autoscaler \
  --namespace kube-system --set autoDiscovery.clusterName="$(out cluster_name)" \
  --set awsRegion="$(out region)" --set rbac.serviceAccount.name=cluster-autoscaler
```

Pin each with `--version`, to the release you qualified. They need no
annotations: the `access` module bound each one's service account to its
role through EKS Pod Identity (`tofu output controller_roles`).

## 4. Secrets and the database's roles, once

```console
eval "$(out put_secrets)"
deploy/aws/scripts/create-database-roles.sh iki-production "$(out database_master_secret_arn)"
out external_secrets | kubectl apply -f -
```

The first generates Iki's secrets straight into Secrets Manager. The second
creates the two database roles with their passwords (ADR 0025), and stores
RDS's CA as the `iki-database-ca` Secret. The third has External Secrets
copy each secret into namespace `iki`, refreshing hourly.

## 5. Iki

```console
out helm_values > deploy/aws/production/values-aws.yaml
helm upgrade --install iki deploy/helm/iki --namespace iki \
  -f deploy/aws/production/values-production.yaml -f deploy/aws/production/values-aws.yaml
```

`values-aws.yaml` is what the infrastructure decided: the bucket, the
Secrets' names, the ALB, the networks pods may reach. Change those in
OpenTofu and write it again. `values-production.yaml` holds the choices
that are yours (replicas, the egress proxy); pin the image by digest there.

Point your DNS name at the ALB (`kubectl -n iki get ingress iki-web`), then
open the one-time setup link
`kubectl -n iki exec deploy/iki-web -- iki-server setup_link` prints.

## 6. Metrics, dashboards and alerts

Iki sends traces and metrics over OTLP to your collector: set
`otel.endpoint` in `values-production.yaml` (an OpenTelemetry Collector,
or Amazon Managed Service for Prometheus through one). With
kube-prometheus-stack in the cluster, also set:

```yaml
monitoring:
  dashboards: { enabled: true } # for Grafana's dashboard sidecar
  alerts: { enabled: true, labels: { release: kube-prometheus-stack } }
```

Otherwise import `deploy/helm/iki/files/dashboards/iki.json` into Grafana
and load `deploy/helm/iki/files/alerts.yaml` as a rules file. ADR 0098 lists
the metrics and what each alert means.

## Recovering

[The disaster-recovery runbook](../../../docs/disaster-recovery.md):
restoring the database in this region, or standing Iki up again in the DR
region after losing this one, and the drills that prove both.

## Checking it

`make check-infra` plans this root against a mocked AWS and checks the
chart's values it writes. The chart's own tests render the chart with
those values (`tests/helm-values.yaml`) and `values-production.yaml`, so a
change to either that the chart can't take fails there.

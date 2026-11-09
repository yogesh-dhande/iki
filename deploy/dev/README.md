# Local development cluster

Iki runs on Kubernetes everywhere, including your laptop. Locally that means a
[kind](https://kind.sigs.k8s.io/) cluster driven by [Tilt](https://tilt.dev/),
deployed with the same Helm chart as production (`deploy/helm/iki`).

## Requirements

- Docker, with about 6 GB of memory available to it
- `kind`, `kubectl`, `helm` (v4) and `tilt`. On macOS:
  `brew install kind kubernetes-cli helm tilt-dev/tap/tilt`
- `jq` and `curl` for the end-to-end checks

## Usage

```console
make dev-up      # create the cluster (once) and start Tilt; UI at http://localhost:10350
make dev-creds   # print the generated passwords and keys
make dev-e2e     # run the end-to-end checks against the running cluster
make dev-ci      # headless: bring everything up, run the checks, exit (what CI runs)
make dev-down    # delete the cluster
```

Tilt rebuilds the server image and redeploys whenever code under `server/`
changes.

## What runs

| Component                        | Local URL               | Notes                                                      |
| -------------------------------- | ----------------------- | ---------------------------------------------------------- |
| Iki web (`iki-web`)              | <http://localhost:8000> | Helm chart with `values-dev.yaml`                          |
| Migrations (`iki-migrate`)       | n/a                     | Job; web pods turn ready only once it has finished         |
| Postgres 18 (`iki-db`)           | n/a                     | CloudNativePG, TLS verified with its own CA                |
| SeaweedFS (S3)                   | <http://localhost:8333> | Bucket `iki`; signed requests only (ADR 0022)              |
| Keycloak                         | <http://localhost:8080> | Realm `iki` with users `alice` and `bob`, client `iki-web` |
| Grafana, Tempo, Loki, Prometheus | <http://localhost:3000> | `grafana/otel-lgtm`; Iki sends OTLP traces and metrics     |

## Security posture

The dev cluster is deliberately close to production:

- The `iki` namespace enforces Pod Security **restricted**. Every pod is
  non-root, drops all capabilities, and uses a RuntimeDefault seccomp profile.
- The chart's NetworkPolicies are enforced (kindnet), so Iki pods can reach
  only DNS, Postgres and the OTLP collector.
- Every credential is random, generated per cluster by `up.sh`, and never
  written to the repo.
- Every image and the CloudNativePG chart are pinned by digest.

Only Keycloak runs with a writable root filesystem, because its dev mode
builds itself on start.

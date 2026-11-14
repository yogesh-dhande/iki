# Local development cluster

Iki runs on Kubernetes everywhere, including your laptop. Locally that means a
[kind](https://kind.sigs.k8s.io/) cluster driven by [Tilt](https://tilt.dev/),
deployed with the same Helm chart as production (`deploy/helm/iki`).

## Requirements

- Docker, with about 6 GB of memory available to it
- `kind`, `kubectl`, `helm` (v4) and `tilt`. On macOS:
  `brew install kind kubernetes-cli helm tilt-dev/tap/tilt`
- `jq`, `curl` and [`uv`](https://docs.astral.sh/uv/) for the end-to-end checks

## Usage

```console
make dev-up      # create the cluster (once) and start Tilt; UI at http://localhost:10350
make dev-creds   # print the generated passwords and keys
make dev-sso     # set up sign-in through Keycloak (bob becomes the admin)
make dev-e2e     # run the end-to-end checks against the running cluster
make dev-ci      # headless: bring everything up, run the checks, exit (what CI runs)
make dev-down    # delete the cluster
```

Tilt rebuilds the server image and redeploys whenever code under `server/`
changes. It builds the image as a deployment does: the server image, then
the plugins in [`plugins.lock.yaml`](plugins.lock.yaml), built with
`iki deploy build --allow-local` and added by
[`deploy/plugins.Dockerfile`](../plugins.Dockerfile). To try a plugin you
are writing, add it there with `local:`.

It builds the worker image the same way: Iki's worker image, then the same
plugins with their environments built in (`iki deploy build --workers`,
[`deploy/worker.Dockerfile`](../worker.Dockerfile)). Once the web pod is up,
[`worker-registration.sh`](worker-registration.sh) makes the workers'
registration token with `iki-server create_registration_token` and stores
it in the `iki-worker-registration` Secret. Delete that Secret to make a new
one. [`compute.yaml`](compute.yaml) binds the plugins' environments to the
worker pod, so their backend actions run there.

## What runs

| Component                        | Local URL                            | Notes                                                                                  |
| -------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------- |
| Iki web (`iki-web`)              | <http://localhost:8000>              | Helm chart with `values-dev.yaml`                                                      |
| Task runner (`iki-tasks`)        | n/a                                  | `iki-server tasks`: sends email and other background work                              |
| Migrations (`iki-migrate`)       | n/a                                  | Job; web pods turn ready only once it has finished                                     |
| Backend worker (`iki-worker`)    | n/a                                  | Runs backend actions; reaches the web Service over plain HTTP (dev only)               |
| Postgres 18 (`iki-db`)           | n/a                                  | CloudNativePG, TLS verified with its own CA                                            |
| SeaweedFS (S3)                   | <http://localhost:8333>              | Bucket `iki`; signed requests only (ADR 0022)                                          |
| Keycloak                         | <http://keycloak.iki.localhost:8080> | Realm `iki` with users `alice` and `bob`; OIDC client `iki-web` and a SAML client      |
| Mailpit (email)                  | <http://localhost:8025>              | Catches every email Iki sends; nothing leaves the cluster                              |
| Webhook receiver                 | <http://localhost:8090/requests>     | Lists what Iki sent to `http://webhook-receiver.iki.svc:8080/…` webhooks               |
| Fake LIMS                        | <http://localhost:8091/requests>     | `https://fake-lims.iki.svc`, for the example LIMS plugin; lists the requests runs made |
| Grafana, Tempo, Loki, Prometheus | <http://localhost:3300>              | `grafana/otel-lgtm`; Iki sends OTLP traces and metrics                                 |

The example LIMS plugin (`plugins/example_lims`) imports from the fake LIMS
as it would from a lab's: over HTTPS, through the egress proxy, to the host
its `lims_host` setting gives (`settings/example_lims.yaml`). The LIMS's
certificate comes from a dev CA that `up.sh` makes; the backend worker
trusts it, with the public CAs, through `backendWorkers.caSecret`, as a
deployment would trust its internal CA. Its token is in `iki-run-secrets`
(`runSecrets`), which the fake LIMS checks.

## Signing in

A fresh cluster isn't set up yet. Either run `make dev-sso`, which goes
through the real setup wizard and Keycloak's login page for you, or do it by
hand:

1. `kubectl --context kind-iki -n iki exec deploy/iki-web -c web -- iki-server setup_link`
2. Open the printed link and follow the wizard. The issuer is
   `http://keycloak.iki.localhost:8080/realms/iki`, the client ID `iki-web`,
   and the client secret is in `make dev-creds`.
3. Sign in as `alice` or `bob` with the dev user password from `make dev-creds`.

Keycloak lives at `keycloak.iki.localhost` so that your browser and Iki's pods
use the same issuer URL. Browsers resolve every `*.localhost` name to your
own machine, where Tilt forwards port 8080; inside the cluster, `up.sh` adds a
CoreDNS rewrite that points the name at the Keycloak Service. Chrome and
Firefox resolve `*.localhost` on their own; if your browser doesn't (some
Safari versions), add `127.0.0.1 keycloak.iki.localhost` to `/etc/hosts`.

## Working on the web app

`make web-dev` serves the React app with instant reloads at
<http://localhost:5173>, passing pages, the API and sign-in to the cluster.
Sign in at <http://localhost:8000> first. See [web/app](../../web/app/README.md).

## Security posture

The dev cluster is deliberately close to production:

- The `iki` namespace enforces Pod Security **restricted**. Every pod is
  non-root, drops all capabilities, and uses a RuntimeDefault seccomp profile.
- The chart's NetworkPolicies are enforced (kindnet), so Iki pods can reach
  only DNS, Postgres, the OTLP collector and (web pods only) Keycloak.
- Every credential is random, generated per cluster by `up.sh`, and never
  written to the repo.
- Every image and the CloudNativePG chart are pinned by digest.

Only Keycloak runs with a writable root filesystem, because its dev mode
builds itself on start.

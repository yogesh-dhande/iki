# iki-server

The Django project behind Iki's REST API, worker API, MCP server and web UI.
The import package is `iki_server`, so it never shadows the client SDK's
`import iki` (ADR 0020).

## Running

The `iki-server` command wraps Django's management commands:

```console
iki-server wait_for_database  # block until Postgres accepts connections
iki-server migrate        # apply database migrations (as the migrator role)
iki-server grant_app_privileges  # give the app role exactly the rights it needs
iki-server serve          # run the ASGI server (uvicorn) on :8000
iki-server check --deploy # Django's production readiness checks
iki-server setup_link     # print a one-time link to the first-run wizard
iki-server setup_link --recover  # reconfigure sign-in on a set-up installation
iki-server rotate_encryption     # re-encrypt stored secrets with the first key
iki-server tasks          # run background tasks (email, …) until SIGTERM; health on :8001
iki-server maintain       # delete expired sessions, old rate-limit counters and finished tasks (the chart runs it hourly)
```

### First run

Iki has no passwords. Everyone signs in through the organization's identity
provider, over OpenID Connect or SAML 2.0 (ADRs 0023 and 0026). To set it up, run `iki-server setup_link`
inside the cluster and open the printed link within 60 minutes:

```console
kubectl exec deploy/iki-web -c web -- iki-server setup_link
```

The wizard:

1. names the organization;
2. checks the identity provider (its OIDC discovery document, or its SAML
   metadata, whose signing certificate it pins);
3. has you sign in through it;
4. makes you the first administrator.

The link works once; creating a new one revokes older ones. Run
`setup_link --recover` to change the provider later, including to renew a
SAML signing certificate.

### SCIM provisioning

Org Admins turn on SCIM on the **Sign-in** settings page (`/settings/sign-in/`).
That shows the base URL (`https://<host>/scim/v2/`) and a bearer token, once,
to enter in Microsoft Entra ID, Okta or another SCIM client.

- **People:** the identity provider creates, updates and deactivates them,
  and only provisioned people can get new accounts.
- **Groups:** they appear on the same page. Linking one to a team makes the
  group decide who is on that team.

### Service accounts

Org Admins create service accounts under **People → Service accounts**, for
scripts, pipelines and instruments whose work shouldn't be done in a
person's name. Each gets an address under `service-accounts.iki.invalid`;
project Admins add it to projects with that address, like a person. Service
accounts can be Auditors or Compute Admins but not Org Admins, and don't
join teams. On a service account's page, Org Admins create credentials
(OAuth client credentials) with fixed scopes; the secret is shown once
(ADR 0027, ADR 0028).

### OAuth apps

Iki is an OAuth 2.1 server (ADR 0028). The CLI and SDK sign in with the
device flow; MCP clients and other tools register themselves and use the
authorization code flow with PKCE. Clients find everything from
`/.well-known/oauth-authorization-server`.

New self-registered apps wait for an Org Admin's approval under **Settings
→ Apps**, where Org Admins also block apps and can turn approval off.
People choose which scopes and projects each app gets, and revoke apps
under **Account → Apps** (the link on their name). Deactivating someone
ends all their tokens.

### AI agents (MCP)

Iki's MCP server is at `/mcp`, over Streamable HTTP (ADR 0013, ADR 0049).
Agents sign in as the person they act for, with the OAuth apps flow above,
and can do only what that person can, within the scopes and projects they
grant. For example, to add it to Claude Code:

```sh
claude mcp add --transport http iki https://iki.example.com/mcp
```

Its tools search and read entities with their provenance, history and
lineage, and actions and runs with their logs. Everything an agent does is
recorded as done through MCP, by its app.

### Database roles

Iki connects to Postgres as two roles (ADR 0025):

- The **migrator** owns the schema. Run `migrate` and then
  `grant_app_privileges` as it, with `IKI_DATABASE_URL` set to the
  migrator's URL and `IKI_APP_DATABASE_URL` (or `IKI_DB_APP_ROLE`) naming the
  app role. The Helm migrate Job does this on every deploy.
- The **app role** is what web pods and maintenance jobs use. It reads and
  writes rows, but can't change the schema, truncate tables, write the
  migration history, or change or delete audit events. A web pod connected
  with more power than that never turns ready.

The container image (`server/Dockerfile`, built from the repo root) runs
`iki-server serve` as UID 10001.

## Configuration

Everything comes from environment variables. Required values have no default,
and the server refuses to start without them.

| Variable                              | Default       | Meaning                                                                                |
| ------------------------------------- | ------------- | -------------------------------------------------------------------------------------- |
| `IKI_SECRET_KEY`                      | required      | Django secret key                                                                      |
| `IKI_ALLOWED_HOSTS`                   | required      | Comma-separated host names the server answers to                                       |
| `IKI_ENVIRONMENT`                     | required      | Deployment name (`dev`, `prod`, …), attached to logs/traces                            |
| `IKI_ENCRYPTION_KEYS`                 | required      | Comma-separated Fernet keys for secrets at rest; first encrypts                        |
| `IKI_PUBLIC_URL`                      | unset         | Address people use, e.g. `https://iki.example.com`; for setup links                    |
| `IKI_DATABASE_URL`                    | required      | `postgres://user:pass@host:port/db`                                                    |
| `IKI_APP_DATABASE_URL`                | unset         | Migrate Job only: the app role's URL, to name the role to grant                        |
| `IKI_DB_APP_ROLE`                     | unset         | Migrate Job only: the app role's name, instead of the URL                              |
| `IKI_DB_REQUIRE_LEAST_PRIVILEGE`      | `true`        | Readiness fails if the database role has more power than the app needs                 |
| `IKI_DB_SSLMODE`                      | `verify-full` | libpq `sslmode`                                                                        |
| `IKI_DB_SSLROOTCERT`                  | unset         | CA bundle used to verify the database certificate                                      |
| `IKI_DB_POOL`                         | `true`        | Use psycopg's connection pool                                                          |
| `IKI_DB_POOL_MIN_SIZE` / `_MAX_SIZE`  | `2` / `10`    | Pool size per process                                                                  |
| `IKI_HTTPS`                           | `true`        | Clients use HTTPS: redirects, secure cookies, HSTS                                     |
| `IKI_FORWARDED_ALLOW_IPS`             | `127.0.0.1`   | Proxies trusted for `X-Forwarded-*` headers                                            |
| `IKI_KEEP_ALIVE_SECONDS`              | `75`          | Idle connections stay open this long; keep above the load balancer's idle timeout      |
| `IKI_CSRF_TRUSTED_ORIGINS`            | unset         | Extra origins allowed to submit forms (comma-separated)                                |
| `IKI_SESSION_IDLE_TIMEOUT_MINUTES`    | `480`         | Sign out after this long without activity                                              |
| `IKI_SESSION_MAX_AGE_HOURS`           | `24`          | Sign out this long after signing in, active or not                                     |
| `IKI_API_RATE_LIMIT_PER_MINUTE`       | `600`         | `/api/v1` requests one caller may make per minute                                      |
| `IKI_OAUTH_ACCESS_TOKEN_MINUTES`      | `60`          | OAuth access token lifetime                                                            |
| `IKI_OAUTH_REFRESH_TOKEN_DAYS`        | `30`          | OAuth refresh token lifetime; each use replaces it                                     |
| `IKI_OAUTH_CLIENT_METADATA_DOCUMENTS` | `false`       | Fetch apps' Client ID Metadata Documents (needs internet egress)                       |
| `IKI_EMAIL_URL`                       | unset         | Mail server, `smtp+tls://user:pass@host:587` or `smtp+ssl://…:465`; unset: no email    |
| `IKI_EMAIL_FROM`                      | required\*    | \*With email: the sender, e.g. `Iki <iki@example.com>` (covered by SPF and DKIM)       |
| `IKI_EMAIL_ALLOW_PLAINTEXT`           | `false`       | Allow `smtp://` (no encryption) with HTTPS on: only for a relay in your network        |
| `IKI_WEBHOOK_ALLOWED_NETWORKS`        | unset         | Comma-separated CIDRs of internal networks webhooks may send to                        |
| `IKI_WEBHOOK_CA_FILE`                 | unset         | CA bundle (PEM) trusted for webhook receivers, as well as the public CAs               |
| `IKI_FILES_BUCKET`                    | unset         | S3 bucket for files; unset, the files API answers 503                                  |
| `IKI_FILES_PREFIX`                    | unset         | Prefix for every key Iki writes, so deployments can share a bucket                     |
| `IKI_FILES_REGION`                    | `us-east-1`   | The bucket's region                                                                    |
| `IKI_FILES_ENDPOINT_URL`              | unset         | An S3-compatible store's URL; unset for AWS S3                                         |
| `IKI_FILES_PUBLIC_ENDPOINT_URL`       | the endpoint  | Where browsers and workers reach the store, which signed URLs name                     |
| `IKI_FILES_MAX_UPLOAD_SIZE`           | `100GiB`      | The largest file one upload may add                                                    |
| `IKI_FILES_REGISTER_LOCATIONS`        | (none)        | Comma-separated `s3://bucket/prefix/` locations files may be registered from, in place |
| `IKI_WEBHOOK_ALLOW_HTTP`              | `false`       | Development only: plain `http://` receivers; refused with HTTPS on                     |
| `IKI_TASKS_CONCURRENCY`               | `4`           | `iki-server tasks`: tasks one runner runs at once                                      |
| `IKI_TASKS_GRACE_SECONDS`             | `25`          | `iki-server tasks`: time running tasks get to finish after SIGTERM                     |
| `IKI_TASKS_HEALTH_PORT`               | `8001`        | `iki-server tasks`: port for `/healthz/live` and `/healthz/ready` (`0`: off)           |
| `IKI_COMPUTE_FILE`                    | unset         | Development and tests: a `compute.json` to use instead of the one the build wrote      |
| `IKI_STATIC_ROOT`                     | `.static`     | Collected static files (`/opt/iki/static` in the image)                                |
| `IKI_DEBUG`                           | `false`       | Django debug mode. Never in production                                                 |
| `IKI_WEB_DEV_SERVER`                  | `false`       | Development only: pages requested through Vite load the web app from it                |
| `IKI_LOG_LEVEL`                       | `INFO`        | Root log level                                                                         |
| `IKI_LOG_FORMAT`                      | `json`        | `json` or `console`                                                                    |
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | unset         | OTLP/HTTP collector. Unset means traces are not exported                               |
| `OTEL_*`                              |               | Any standard OpenTelemetry SDK variable                                                |

Generate an encryption key with:

```console
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

To rotate, put the new key first, keep the old one after it, run
`iki-server rotate_encryption`, then remove the old key.

## REST API

The API lives at `/api/v1` (ADR 0027), described by the OpenAPI document at
`/api/v1/openapi.json`. Today it covers you (`/me`), people and their org
roles, service accounts, teams, projects and project access, entities,
actions and their runs, and your notifications. Live updates come from `/api/v1/events` (see below).

- **Authentication:** an OAuth access token (`Authorization: Bearer ...`),
  or the browser session with the CSRF token (from the page's `csrf-token`
  meta tag) in `X-CSRFToken` for unsafe methods. A token can do what both
  its scopes and its user's roles allow; a missing scope answers 403 with
  code `insufficient_scope`.
- **Errors:** RFC 9457 problem details (`application/problem+json`) with a
  stable `code`, and the invalid fields in `errors`.
- **Lists:** `?limit=` (1–100, default 50) and `?cursor=` from the previous
  page's `next_cursor`.
- **Rate limits:** per caller, reported in the `RateLimit` and
  `RateLimit-Policy` headers; over the limit answers 429 with `Retry-After`.
- **Authorization:** the same rules as the web pages. Changes made with a
  token are audited with `via=api` and the OAuth client's name. The web app
  calls the API with the browser's session, and its changes are audited with
  `via=web`.

### Entities

Every plugin's types share one API (ADR 0035):

- `GET /entity-types` lists the installed types, each with its fields and a
  JSON Schema of its values.
- `/entities` lists, creates and changes entities, addressed by ID or HRID
  (`/entities/DNA-000042`). Lists filter by `type` (with its subtypes),
  `project` and `archived`.
- Reads carry the entity's revision as the `ETag`. `PATCH` must send it back
  as `If-Match`. A change made since answers 412 (`stale`), and a missing
  `If-Match` answers 428, so nobody overwrites someone else's change.
- Quantities are numbers in the field's unit, or a value with any unit that
  converts, such as `{"value": 310.15, "unit": "K"}`.
- File fields take a file's ID (see [Files](#files)). The file must be one
  you can read, of a media type the field allows (`image/*` allows any
  image), and no larger than its `max_size`.
- `/entities/{ref}/revisions`, `…/revisions/{number}`, `…/provenance` and
  `…/projects` give the history: who changed what, how, when and why.
- Reading takes a role in any of the entity's projects. Changing it takes
  Editor in its owner project. Entities you can't read answer 404.

### Actions and runs

Plugins' actions run on entities (ADR 0042):

- `GET /actions` lists the installed actions: inputs, params, effects, tier,
  and whether this deployment can run each one (`available`, with
  `unavailable_reason`). An action can run when its environment is bound to
  compute templates on its tier in the deployment's `compute.yaml`.
- `POST /actions/{name}/runs` queues a run in a project, with its inputs
  (entity IDs) and params. Everything is checked before it's queued: your
  role for the tier (Editor for client and backend, Runner for container and
  vm), every input is an entity you can read, of the right type and not
  archived, entities it updates are ones you can edit, and every param
  fits. Defaults are filled in.
- `GET /runs` and `GET /runs/{ref}` (ID or `RUN-000123`) show runs to
  whoever can read their project: who ran what, on which revisions, with
  which params, and how it ended.
- `POST /runs/{ref}/cancel` cancels a queued run at once, or asks a running
  one's worker to stop. Whoever may run its tier in the project may cancel it.

### Workers

Workers run the jobs and pull them from the server over HTTPS (ADR 0043).
Nothing connects to a worker. A Compute admin manages them under
`/api/v1/compute`:

- `POST /compute/registration-tokens` makes a registration token
  (`iki_wreg_…`) for the backend, container or vm tier, optionally with an
  expiry. Its value is shown once; put it in that tier's secret store.
- `POST /compute/registration-tokens/{id}/revoke` stops new registrations.
  Workers that already registered keep working, unless you pass
  `{"workers": true}` for a token that leaked.
- `GET /compute/workers` lists workers as online, offline or revoked, with
  the jobs each holds. `POST /compute/workers/{id}/revoke` ends a worker's
  token at once. Its jobs are interrupted and retried elsewhere.
- `/compute/quotas` limits runs for the organization, a team or a project,
  on one tier or all of them (ADR 0045). `max_running` caps jobs running at
  once (0 pauses the scope). `max_queued` caps runs waiting to start;
  launching more is refused. A queued run held back by a quota says why in
  `waiting`.

`iki-worker` uses `/worker/v1`, with its OpenAPI document at
`/worker/v1/openapi.json`:

1. `POST /register` with the registration token returns the worker's own
   token (`iki_wkr_…`).
2. `POST /lease?wait=30` waits for a job that fits the worker's capacity and
   returns its run input (`run-input/v1`), or 204 if none arrives.
3. While it holds the job, it sends heartbeats every 20 seconds with the
   job's state. The response says whether the run was cancelled. A lease
   that isn't renewed within 60 seconds is ended, and the job retried.
4. It sends `progress`, and `logs` in ordered chunks with byte offsets; each
   job keeps up to 16 MiB of log.
5. `POST /jobs/{id}/uploads` starts uploading a result file, for the run's
   launcher in the run's project. The worker PUTs its parts, then calls
   `complete` until the upload is `ready`, and cites its file ID in
   `output.json`.
6. `fail` ends an attempt with no results. Attempts lost to `worker_lost` or
   `interrupted` are retried by default; failures in the command itself are
   not.

Every report carries the lease token from the job. A report with a stale
token answers 409 `stale_lease`, and the worker drops that job.

### Results

The worker sends the command's `output.json` with `POST /jobs/{id}/complete`
(ADR 0044). Iki checks it against what the action declares: which inputs it
updates and which fields, which types it creates and how many, and what each
new entity was made from. It then writes all of it in one transaction, as
the person who launched the run, or writes none of it.

- `GET /runs/{ref}` lists the entities a run created or updated (`effects`),
  with its `metrics` and `messages`. A failed run's `error.errors` points at
  each problem in its output.
- Revisions a run made have `via: "run"` and name the run, so
  `GET /entities/{ref}/provenance` shows which run set each value.
- `GET /entities/{ref}/lineage` lists what runs made an entity from, at the
  revisions they read, and what runs made from it.

Results that don't fit what the action declares end the run `failed`
(`invalid_output`), or `rejected` (`schema_changed`) if the plugin was
upgraded while it ran. If the launcher lost access, or an entity or the
project was archived meanwhile, the run ends `rejected`. The output is kept
either way.

## Web app

Home, notifications and the project pages are the React app in `web/app` (ADR 0029). The
server answers their addresses with one page that loads the built app from
`static/app/` and carries the CSRF token; the app reads everything from
`/api/v1`. Build it with `make web` (the image builds it in its own stage).
Without a build, those pages answer 503 and say so. Sign-in, setup, consent,
teams, people, settings and the audit log are server-rendered.

## Background tasks

Work done outside requests, such as sending email, runs as Django tasks
(`django.tasks`) on a queue in Postgres (ADR 0030). Code enqueues a task in
the same transaction as the change it belongs to, so a rolled-back change
never runs its tasks. `iki-server tasks` runs them: the chart deploys it as
the `iki-tasks` Deployment, and more replicas share the queue.

- Runners wake at once on `NOTIFY` and poll every few seconds as well.
- Each attempt holds a lease that the runner renews. When a runner dies, the
  lease runs out and another runner retries the attempt, so tasks must be
  idempotent.
- Failing tasks retry with exponential backoff, up to their `max_attempts`.
- `iki-server maintain` deletes successful tasks after 7 days and failed
  ones after 30.

## Notifications and email

People are told in Iki when something concerns them: being added to a
project or team, a role change, or (for Org Admins) an app awaiting
approval. Nobody is told about their own actions. The bell in the header
counts unread notifications, and `/notifications/` lists them. Each person
chooses which kinds also come by email, at Account → Notifications.

Email is off until `IKI_EMAIL_URL` is set. Encryption is required:
STARTTLS (`smtp+tls://`) or TLS from the start (`smtp+ssl://`), with the
server's certificate verified. Org Admins can send a test email at
Settings → Email, and can choose to leave the details out of emails, so
they only say there's something new in Iki. Emails are marked as automatic
(`Auto-Submitted`), so out-of-office replies stay quiet.

## Files

Files live in object storage (ADR 0036). Iki keeps each content once, under
its SHA-256, and checks every upload against the hash its sender declared.

1. `POST /api/v1/uploads` with the file's project, name, media type, size and
   SHA-256. The answer lists its parts (64 MiB each, or more for very large
   files), each with a URL signed for an hour.
2. `PUT` each part's bytes to its URL, in any order or in parallel.
   `POST /api/v1/uploads/{id}/parts` signs them again if the URLs expire.
3. `POST /api/v1/uploads/{id}/complete`. The upload is `checking` while Iki
   reads the bytes back, then `ready` with its `file`, or `failed` if they
   don't match. `upload.finished` arrives on `/api/v1/events`.

- `GET /api/v1/files/{id}` describes a file. `…/content` redirects to a URL
  that downloads it, signed for five minutes. Whoever can read the project a
  file was added to can read it, and so can whoever can read an entity that
  refers to it, at any revision.
- Uploads left unfinished for a day are cancelled by `iki-server maintain`.

Files already in a lab's own bucket can be **registered in place** instead
of uploaded. Administrators approve the locations
(`IKI_FILES_REGISTER_LOCATIONS`, such as `s3://lab-data/runs/`), and each
bucket must keep versions. `POST /api/v1/registrations` with the project and
the object's `s3://` URI (and, optionally, a `version`) pins that version.
Iki then reads it once to learn its SHA-256, and the registration becomes
`ready` with its `file`. `registration.finished` arrives on
`/api/v1/events`. Objects stored before their bucket kept versions are
refused, since they could still be overwritten. Copying one onto itself
gives it a version. Give the pods' IAM role `s3:GetObject`,
`s3:GetObjectVersion`, `s3:GetBucketVersioning`, `s3:GetBucketLocation` and
`s3:ListBucket` on those locations. `GET /api/v1/registrations/locations`
lists them.

- On AWS, give the pods' service account an IAM role with `s3:GetObject`,
  `s3:PutObject`, `s3:DeleteObject`, `s3:ListMultipartUploadParts` and
  `s3:AbortMultipartUpload` on the keys under the prefix, and `s3:ListBucket`
  on the bucket for that prefix. Without `s3:ListBucket`, S3 answers 403
  rather than 404 for a missing object. Turn on versioning and default
  encryption. Browsers send parts straight to the bucket, so it needs a CORS
  rule allowing `PUT` from Iki's public URL. `iki-server setup_storage` adds
  that rule, and lifecycle rules that clear abandoned uploads; `--check`
  only reports what's missing, for buckets managed elsewhere (OpenTofu, for
  example).

## Webhooks

Webhooks tell other systems, such as an ELN or LIMS, when something changes
(ADR 0031). Project Admins add them on a project's Webhooks tab, and Org
Admins add organization-wide ones at Settings → Webhooks. The API is under
`/api/v1/projects/{id}/webhooks` and `/api/v1/webhooks`; tokens need the
`webhooks` scope.

- Messages follow [Standard Webhooks](https://www.standardwebhooks.com/):
  verify the `webhook-signature` header with the secret shown when the
  webhook is added, using the standard's library for your language.
- Events come from the audit log, or from entities' revisions, in the
  change's transaction: `/api/v1/webhook-events` lists them. Delivery is at
  least once; dedupe on `webhook-id`.
- Entity events carry identifiers and the changed fields' names, not values.
  Receivers read the values through the API with their own token.
- Task runners send them, retrying failures over about a day. Each
  webhook's delivery log keeps 30 days, and a webhook failing for 5 days is
  turned off and its managers told.
- Iki sends only to public addresses over HTTPS, without following
  redirects. For receivers inside your network, list their networks in
  `IKI_WEBHOOK_ALLOWED_NETWORKS`, and add your CA with `IKI_WEBHOOK_CA_FILE`
  if it's private. The chart lets only task runners reach receivers
  (`networkPolicy.egress.webhooks`).
- Org Admins see every project's webhooks (the host, not the address), can
  turn any of them off, and choose whether project Admins may add them.

## Live updates

`GET /api/v1/events` is a Server-Sent Events stream: the browser session or
a token with the `read` scope. Events say _that_ something changed, with
ids and counts (`ready`, `notification`, `notifications_read`, `resync`),
never content. Each event is checked against the caller's access before
it's sent, and the session or token is checked again every 20 seconds:
when it has ended, the stream sends `signed_out` and closes. Streams last
at most 30 minutes, and clients reconnect. Holding a stream open doesn't
count as activity for the session's idle timeout.

Behind a proxy, turn off response buffering for that path (Iki sends
`X-Accel-Buffering: no` for nginx) and allow idle reads of at least a
minute.

## Health checks

- `GET /healthz/live`: the process is serving. Checks nothing else.
- `GET /healthz/ready`: Postgres answers, all migrations are applied, and the
  database role is least-privileged (see Database roles).

Both return JSON naming each check and `ok`/`fail`, never error details.

## Audit log

Every change, sign-in and setup step is recorded as an audit event in the same
transaction as the change (ADR 0025). Auditors read them at `/audit/`. Each
event is also written to the `iki.audit` log stream once it commits, so a
SIEM fed from the logs keeps its own copy. Nobody, the table's owner
included, can update or delete events.

## Observability

- Logs are JSON lines on stdout, one per request plus application events.
  Each line has `trace_id`/`span_id` and a `request_id`, which is also returned
  in the `X-Request-ID` header. Query strings are never logged.
- Traces and metrics go to an OTLP/HTTP collector, configured with the standard
  `OTEL_*` variables. Requests and SQL queries are instrumented. Health
  probes are not traced.

## Tests

`uv run pytest` starts a throwaway Postgres container (Docker required), unless
`IKI_TEST_DATABASE_URL` points at an existing server, as it does in CI.

The browser tests (`tests/test_browser.py`) run Chromium, Firefox and WebKit
against the real server, with axe and the production CSP. They need the built
web app (`make web`) and the browsers (`uv run playwright install`). Without
them they skip, except in CI, where they fail. `-m "not built_web_app"` leaves
them out.

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
iki-server maintain       # delete expired sessions and old rate-limit counters (the chart runs it hourly)
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

| Variable                              | Default       | Meaning                                                                           |
| ------------------------------------- | ------------- | --------------------------------------------------------------------------------- |
| `IKI_SECRET_KEY`                      | required      | Django secret key                                                                 |
| `IKI_ALLOWED_HOSTS`                   | required      | Comma-separated host names the server answers to                                  |
| `IKI_ENVIRONMENT`                     | required      | Deployment name (`dev`, `prod`, …), attached to logs/traces                       |
| `IKI_ENCRYPTION_KEYS`                 | required      | Comma-separated Fernet keys for secrets at rest; first encrypts                   |
| `IKI_PUBLIC_URL`                      | unset         | Address people use, e.g. `https://iki.example.com`; for setup links               |
| `IKI_DATABASE_URL`                    | required      | `postgres://user:pass@host:port/db`                                               |
| `IKI_APP_DATABASE_URL`                | unset         | Migrate Job only: the app role's URL, to name the role to grant                   |
| `IKI_DB_APP_ROLE`                     | unset         | Migrate Job only: the app role's name, instead of the URL                         |
| `IKI_DB_REQUIRE_LEAST_PRIVILEGE`      | `true`        | Readiness fails if the database role has more power than the app needs            |
| `IKI_DB_SSLMODE`                      | `verify-full` | libpq `sslmode`                                                                   |
| `IKI_DB_SSLROOTCERT`                  | unset         | CA bundle used to verify the database certificate                                 |
| `IKI_DB_POOL`                         | `true`        | Use psycopg's connection pool                                                     |
| `IKI_DB_POOL_MIN_SIZE` / `_MAX_SIZE`  | `2` / `10`    | Pool size per process                                                             |
| `IKI_HTTPS`                           | `true`        | Clients use HTTPS: redirects, secure cookies, HSTS                                |
| `IKI_FORWARDED_ALLOW_IPS`             | `127.0.0.1`   | Proxies trusted for `X-Forwarded-*` headers                                       |
| `IKI_KEEP_ALIVE_SECONDS`              | `75`          | Idle connections stay open this long; keep above the load balancer's idle timeout |
| `IKI_CSRF_TRUSTED_ORIGINS`            | unset         | Extra origins allowed to submit forms (comma-separated)                           |
| `IKI_SESSION_IDLE_TIMEOUT_MINUTES`    | `480`         | Sign out after this long without activity                                         |
| `IKI_SESSION_MAX_AGE_HOURS`           | `24`          | Sign out this long after signing in, active or not                                |
| `IKI_API_RATE_LIMIT_PER_MINUTE`       | `600`         | `/api/v1` requests one caller may make per minute                                 |
| `IKI_OAUTH_ACCESS_TOKEN_MINUTES`      | `60`          | OAuth access token lifetime                                                       |
| `IKI_OAUTH_REFRESH_TOKEN_DAYS`        | `30`          | OAuth refresh token lifetime; each use replaces it                                |
| `IKI_OAUTH_CLIENT_METADATA_DOCUMENTS` | `false`       | Fetch apps' Client ID Metadata Documents (needs internet egress)                  |
| `IKI_STATIC_ROOT`                     | `.static`     | Collected static files (`/opt/iki/static` in the image)                           |
| `IKI_DEBUG`                           | `false`       | Django debug mode. Never in production                                            |
| `IKI_LOG_LEVEL`                       | `INFO`        | Root log level                                                                    |
| `IKI_LOG_FORMAT`                      | `json`        | `json` or `console`                                                               |
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | unset         | OTLP/HTTP collector. Unset means traces are not exported                          |
| `OTEL_*`                              |               | Any standard OpenTelemetry SDK variable                                           |

Generate an encryption key with:

```console
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

To rotate, put the new key first, keep the old one after it, run
`iki-server rotate_encryption`, then remove the old key.

## REST API

The API lives at `/api/v1` (ADR 0027), described by the OpenAPI document at
`/api/v1/openapi.json`. Today it covers you (`/me`), people and their org
roles, service accounts, teams, projects and project access.

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
- **Authorization:** the same rules as the web pages. Changes are audited
  with `via=api`, and with the OAuth client's name when a token made them.

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

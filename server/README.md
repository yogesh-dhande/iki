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
iki-server clear_expired_sessions  # delete expired sessions (the chart runs it daily)
```

### First run

Iki has no passwords. Everyone signs in through the organization's OIDC
identity provider (ADR 0023). To set it up, run `iki-server setup_link`
inside the cluster and open the printed link within 60 minutes:

```console
kubectl exec deploy/iki-web -c web -- iki-server setup_link
```

The wizard names the organization, checks the identity provider's discovery
document, has you sign in through it, and makes you the first administrator.
The link works once; creating a new one revokes older ones.

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

| Variable                             | Default       | Meaning                                                                |
| ------------------------------------ | ------------- | ---------------------------------------------------------------------- |
| `IKI_SECRET_KEY`                     | required      | Django secret key                                                      |
| `IKI_ALLOWED_HOSTS`                  | required      | Comma-separated host names the server answers to                       |
| `IKI_ENVIRONMENT`                    | required      | Deployment name (`dev`, `prod`, …), attached to logs/traces            |
| `IKI_ENCRYPTION_KEYS`                | required      | Comma-separated Fernet keys for secrets at rest; first encrypts        |
| `IKI_PUBLIC_URL`                     | unset         | Address people use, e.g. `https://iki.example.com`; for setup links    |
| `IKI_DATABASE_URL`                   | required      | `postgres://user:pass@host:port/db`                                    |
| `IKI_APP_DATABASE_URL`               | unset         | Migrate Job only: the app role's URL, to name the role to grant        |
| `IKI_DB_APP_ROLE`                    | unset         | Migrate Job only: the app role's name, instead of the URL              |
| `IKI_DB_REQUIRE_LEAST_PRIVILEGE`     | `true`        | Readiness fails if the database role has more power than the app needs |
| `IKI_DB_SSLMODE`                     | `verify-full` | libpq `sslmode`                                                        |
| `IKI_DB_SSLROOTCERT`                 | unset         | CA bundle used to verify the database certificate                      |
| `IKI_DB_POOL`                        | `true`        | Use psycopg's connection pool                                          |
| `IKI_DB_POOL_MIN_SIZE` / `_MAX_SIZE` | `2` / `10`    | Pool size per process                                                  |
| `IKI_HTTPS`                          | `true`        | Clients use HTTPS: redirects, secure cookies, HSTS                     |
| `IKI_FORWARDED_ALLOW_IPS`            | `127.0.0.1`   | Proxies trusted for `X-Forwarded-*` headers                            |
| `IKI_CSRF_TRUSTED_ORIGINS`           | unset         | Extra origins allowed to submit forms (comma-separated)                |
| `IKI_SESSION_IDLE_TIMEOUT_MINUTES`   | `480`         | Sign out after this long without activity                              |
| `IKI_SESSION_MAX_AGE_HOURS`          | `24`          | Sign out this long after signing in, active or not                     |
| `IKI_STATIC_ROOT`                    | `.static`     | Collected static files (`/opt/iki/static` in the image)                |
| `IKI_DEBUG`                          | `false`       | Django debug mode. Never in production                                 |
| `IKI_LOG_LEVEL`                      | `INFO`        | Root log level                                                         |
| `IKI_LOG_FORMAT`                     | `json`        | `json` or `console`                                                    |
| `OTEL_EXPORTER_OTLP_ENDPOINT`        | unset         | OTLP/HTTP collector. Unset means traces are not exported               |
| `OTEL_*`                             |               | Any standard OpenTelemetry SDK variable                                |

Generate an encryption key with:

```console
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

To rotate, put the new key first, keep the old one after it, run
`iki-server rotate_encryption`, then remove the old key.

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

# iki-server

The Django project behind Iki's REST API, worker API, MCP server and web UI.
The import package is `iki_server`, so it never shadows the client SDK's
`import iki` (ADR 0020).

## Running

The `iki-server` command wraps Django's management commands:

```console
iki-server migrate        # apply database migrations
iki-server serve          # run the ASGI server (uvicorn) on :8000
iki-server check --deploy # Django's production readiness checks
```

The container image (`server/Dockerfile`, built from the repo root) runs
`iki-server serve` as UID 10001.

## Configuration

Everything comes from environment variables. Required values have no default,
and the server refuses to start without them.

| Variable                             | Default       | Meaning                                                     |
| ------------------------------------ | ------------- | ----------------------------------------------------------- |
| `IKI_SECRET_KEY`                     | required      | Django secret key                                           |
| `IKI_ALLOWED_HOSTS`                  | required      | Comma-separated host names the server answers to            |
| `IKI_ENVIRONMENT`                    | required      | Deployment name (`dev`, `prod`, …), attached to logs/traces |
| `IKI_DATABASE_URL`                   | required      | `postgres://user:pass@host:port/db`                         |
| `IKI_DB_SSLMODE`                     | `verify-full` | libpq `sslmode`                                             |
| `IKI_DB_SSLROOTCERT`                 | unset         | CA bundle used to verify the database certificate           |
| `IKI_DB_POOL`                        | `true`        | Use psycopg's connection pool                               |
| `IKI_DB_POOL_MIN_SIZE` / `_MAX_SIZE` | `2` / `10`    | Pool size per process                                       |
| `IKI_HTTPS`                          | `true`        | Clients use HTTPS: redirects, secure cookies, HSTS          |
| `IKI_FORWARDED_ALLOW_IPS`            | `127.0.0.1`   | Proxies trusted for `X-Forwarded-*` headers                 |
| `IKI_DEBUG`                          | `false`       | Django debug mode. Never in production                      |
| `IKI_LOG_LEVEL`                      | `INFO`        | Root log level                                              |
| `IKI_LOG_FORMAT`                     | `json`        | `json` or `console`                                         |
| `OTEL_EXPORTER_OTLP_ENDPOINT`        | unset         | OTLP/HTTP collector. Unset means traces are not exported    |
| `OTEL_*`                             |               | Any standard OpenTelemetry SDK variable                     |

## Health checks

- `GET /healthz/live`: the process is serving. Checks nothing else.
- `GET /healthz/ready`: Postgres answers and all migrations are applied.

Both return JSON naming each check and `ok`/`fail`, never error details.

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

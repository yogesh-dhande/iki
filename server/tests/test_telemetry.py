import pytest
from django.test import AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from iki_server.observability import telemetry

pytestmark = pytest.mark.django_db


async def test_requests_are_traced_with_service_resource(
    async_client: AsyncClient, spans: InMemorySpanExporter
) -> None:
    await async_client.get("/nope", secure=True)
    (span,) = [s for s in spans.get_finished_spans() if s.kind.name == "SERVER"]
    assert span.attributes is not None
    assert span.attributes.get("http.response.status_code") == 404
    assert span.resource.attributes["service.name"] == "iki-server"
    assert span.resource.attributes["deployment.environment.name"] == "test"


async def test_health_probes_are_not_traced(
    async_client: AsyncClient, spans: InMemorySpanExporter
) -> None:
    await async_client.get("/healthz/ready")
    assert [s for s in spans.get_finished_spans() if s.kind.name == "SERVER"] == []


def test_database_queries_are_traced(spans: InMemorySpanExporter) -> None:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    assert any(s.kind.name == "CLIENT" for s in spans.get_finished_spans())


def test_configure_is_idempotent() -> None:
    first = telemetry.configure_telemetry(service_version="x", environment="y")
    second = telemetry.configure_telemetry(service_version="z", environment="w")
    assert first is not None
    assert first is second


def test_health_checks_stay_ahead_of_the_otel_middleware() -> None:
    from django.conf import settings

    assert settings.MIDDLEWARE[0] == telemetry.HEALTH_MIDDLEWARE
    assert settings.MIDDLEWARE.count(telemetry.HEALTH_MIDDLEWARE) == 1
    assert any("opentelemetry" in m for m in settings.MIDDLEWARE[1:])


#: Values a query string can carry that must stay out of traces: a sign-in code and state,
#: an email address in a SCIM filter, and search text.
SECRETS = ("CODE-7f3a", "STATE-9c1d", "ada@example.com", "crambin")


async def test_query_values_stay_out_of_request_spans(
    async_client: AsyncClient, spans: InMemorySpanExporter
) -> None:
    await async_client.get(
        "/nope",
        {"code": SECRETS[0], "state": SECRETS[1], "filter": f'userName eq "{SECRETS[2]}"'},
        secure=True,
    )
    await async_client.get("/nope", {"q": SECRETS[3]}, secure=True)
    server = [s for s in spans.get_finished_spans() if s.kind.name == "SERVER"]
    assert len(server) == 2
    assert server[0].attributes is not None
    assert server[0].attributes["url.query"] == "code=REDACTED&state=REDACTED&filter=REDACTED"
    for span in spans.get_finished_spans():
        for value in (span.attributes or {}).values():
            assert not any(secret in str(value) for secret in SECRETS), (span.name, value)


def test_query_values_are_redacted_in_the_older_http_attributes_too() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    attributes = {
        "http.target": "/accounts/oidc/x/login/callback/?code=CODE-7f3a&state=STATE-9c1d",
        "http.url": "https://iki.example/search?q=crambin#top",
        "url.full": "https://iki.example/search?q=crambin&page=2",
        "url.query": "q=crambin&flag",
        "url.path": "/search",
    }
    with provider.get_tracer("test").start_as_current_span("GET", attributes=attributes) as span:
        telemetry.redact_query(span, None)
    (finished,) = exporter.get_finished_spans()
    assert dict(finished.attributes or {}) == {
        "http.target": "/accounts/oidc/x/login/callback/?code=REDACTED&state=REDACTED",
        "http.url": "https://iki.example/search?q=REDACTED",
        "url.full": "https://iki.example/search?q=REDACTED&page=REDACTED",
        "url.query": "q=REDACTED&flag=REDACTED",
        "url.path": "/search",
    }

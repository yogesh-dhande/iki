import pytest
from django.test import AsyncClient
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

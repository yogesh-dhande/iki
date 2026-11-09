"""OpenTelemetry tracing and metrics.

Configuration follows the standard ``OTEL_*`` environment variables, so
operators can point Iki at any OTLP collector without Iki-specific settings:

- ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or the per-signal variants) turns on
  export. Without it, spans are still created, so trace IDs still appear
  in logs, but nothing is sent anywhere.
- ``OTEL_SERVICE_NAME`` defaults to ``iki-server``.
- ``OTEL_RESOURCE_ATTRIBUTES`` adds resource attributes.
- ``OTEL_SDK_DISABLED=true`` turns telemetry off entirely.
"""

import os
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

DEFAULT_SERVICE_NAME = "iki-server"

#: Requests to these paths are not traced. Probes would otherwise produce a
#: span every few seconds per pod.
EXCLUDED_URLS = "healthz/"


HEALTH_MIDDLEWARE = "iki_server.health.HealthCheckMiddleware"


def _keep_health_checks_first() -> None:
    """Moves the health check middleware back in front of OpenTelemetry's.

    The Django instrumentation inserts its middleware at the very front, and
    that middleware validates the Host header. Kubelet probes send the pod IP
    as Host, so they would be rejected before reaching the health checks.
    """
    from django.conf import settings

    middleware = [m for m in settings.MIDDLEWARE if m != HEALTH_MIDDLEWARE]
    settings.MIDDLEWARE = [HEALTH_MIDDLEWARE, *middleware]


@dataclass(frozen=True)
class TelemetryState:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider


_state: TelemetryState | None = None


def _exporting(signal: str) -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT")
    )


def configure_telemetry(
    *,
    service_version: str,
    environment: str,
    span_processor: SpanProcessor | None = None,
    metric_reader: MetricReader | None = None,
) -> TelemetryState | None:
    """Sets up tracing, metrics and auto-instrumentation once per process.

    ``span_processor`` and ``metric_reader`` replace the OTLP exporters; tests
    use them to capture telemetry in memory. Returns ``None`` when the SDK is
    disabled. Calling it again returns the existing state unchanged.
    """
    global _state
    if _state is not None:
        return _state
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None

    # Emit the stable semantic conventions (http.response.status_code, ...)
    # rather than the legacy names, unless the operator chose otherwise.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME),
            "service.version": service_version,
            "deployment.environment.name": environment,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    if span_processor is not None:
        tracer_provider.add_span_processor(span_processor)
    elif _exporting("TRACES"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    readers: list[MetricReader] = []
    if metric_reader is not None:
        readers.append(metric_reader)
    elif _exporting("METRICS"):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(meter_provider)

    DjangoInstrumentor().instrument(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        excluded_urls=EXCLUDED_URLS,
    )
    _keep_health_checks_first()
    PsycopgInstrumentor().instrument(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        enable_commenter=False,
    )

    _state = TelemetryState(tracer_provider=tracer_provider, meter_provider=meter_provider)
    return _state

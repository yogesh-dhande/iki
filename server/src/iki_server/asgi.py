"""ASGI entry point.

Telemetry is configured before Django builds its middleware chain, because
OpenTelemetry's Django instrumentation inserts its own middleware.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iki_server.settings")

from django.conf import settings
from django.core.asgi import get_asgi_application

from iki_server.observability.telemetry import configure_telemetry

configure_telemetry(service_version=settings.IKI_VERSION, environment=settings.IKI_ENVIRONMENT)

application = get_asgi_application()

"""ASGI entry point.

Telemetry is configured before Django builds its middleware chain, because
OpenTelemetry's Django instrumentation inserts its own middleware. Django answers
every request; the lifespan starts and stops what runs beside it (the MCP server's
session manager), which Django itself has no hook for.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iki_server.settings")

from django.conf import settings
from django.core.asgi import get_asgi_application

from iki_server.observability.telemetry import configure_telemetry

configure_telemetry(service_version=settings.IKI_VERSION, environment=settings.IKI_ENVIRONMENT)

from contextlib import AsyncExitStack  # noqa: E402
from typing import Any  # noqa: E402

_django = get_asgi_application()


async def _lifespan(receive: Any, send: Any) -> None:  # noqa: ANN401 - ASGI callables
    from iki_server.mcp import runtime

    async with AsyncExitStack() as stack:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await stack.enter_async_context(runtime.running())
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await stack.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return


async def application(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
    if scope["type"] == "lifespan":
        await _lifespan(receive, send)
    else:
        await _django(scope, receive, send)

"""The MCP server's lifetime: the SDK needs its session manager running before it
answers anything, so the web process starts it when it starts (ASGI lifespan) and
stops it when it stops."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.transport_security import TransportSecuritySettings

from iki_server.mcp import bridge, tools

_app: bridge.App | None = None


def app() -> bridge.App | None:
    """The running server's ASGI app, if it's running."""
    return _app


@asynccontextmanager
async def running() -> AsyncIterator[None]:
    global _app
    server = tools.build()
    asgi = server.streamable_http_app(
        streamable_http_path="/",
        # Each request stands alone, and answers with plain JSON: any pod can take any
        # request, and nothing is kept between them.
        stateless_http=True,
        json_response=True,
        # Django checks the Host header (ALLOWED_HOSTS) and the view the Origin.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    async with server.session_manager.run():
        _app = asgi
        try:
            yield
        finally:
            _app = None

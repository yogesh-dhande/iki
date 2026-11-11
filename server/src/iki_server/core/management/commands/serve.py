"""``iki-server serve``: runs the ASGI server."""

import asyncio
import socket
from types import FrameType
from typing import Any

import environ
import uvicorn
from django.core.management.base import BaseCommand, CommandParser

from iki_server.realtime import hub
from iki_server.runs import wakeup

env = environ.Env()


class Server(uvicorn.Server):
    """Ends live-update streams on shutdown.

    uvicorn waits for open responses before it stops, and a live-update
    stream never finishes by itself. Ending them at once lets the pod stop in
    seconds; browsers reconnect to the pods still running.
    """

    _loop: asyncio.AbstractEventLoop | None = None

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(hub.shutting_down)
            self._loop.call_soon_threadsafe(wakeup.shutting_down)


class Command(BaseCommand):
    help = "Serve the Iki web application with uvicorn."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--host", default=env.str("IKI_HOST", default="0.0.0.0"))  # noqa: S104 - containers must listen on all interfaces
        parser.add_argument("--port", type=int, default=env.int("IKI_PORT", default=8000))
        parser.add_argument(
            "--workers",
            type=int,
            default=env.int("IKI_WEB_WORKERS", default=1),
            help="Worker processes. Kubernetes deployments scale pods instead, so keep 1.",
        )
        parser.add_argument(
            "--forwarded-allow-ips",
            default=env.str("IKI_FORWARDED_ALLOW_IPS", default="127.0.0.1"),
            help=(
                "Comma-separated IPs or CIDRs of proxies whose X-Forwarded-For and "
                "X-Forwarded-Proto headers are trusted."
            ),
        )
        parser.add_argument(
            "--keep-alive",
            type=int,
            default=env.int("IKI_KEEP_ALIVE_SECONDS", default=75),
            help=(
                "Seconds an idle client connection stays open. Keep it longer than the "
                "load balancer's idle timeout, or the balancer reuses connections the "
                "server has just closed and answers 502."
            ),
        )
        parser.add_argument("--reload", action="store_true", help="Reload on code changes (dev).")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        settings: dict[str, Any] = {
            "host": options["host"],
            "port": options["port"],
            "workers": options["workers"],
            "reload": options["reload"],
            # The lifespan starts the MCP server's session manager (see asgi.py): without
            # it, the web process mustn't start.
            "lifespan": "on",
            # Logging is already configured by Django settings (JSON lines).
            "log_config": None,
            "access_log": False,
            "proxy_headers": True,
            "forwarded_allow_ips": options["forwarded_allow_ips"],
            "server_header": False,
            "timeout_graceful_shutdown": 30,
            "timeout_keep_alive": options["keep_alive"],
        }
        app = "iki_server.asgi:application"
        if options["workers"] > 1 or options["reload"]:
            # Several processes (or the reloader): uvicorn supervises them,
            # and streams end at the graceful-shutdown timeout instead.
            uvicorn.run(app, **settings)
            return
        Server(uvicorn.Config(app, **settings)).run()

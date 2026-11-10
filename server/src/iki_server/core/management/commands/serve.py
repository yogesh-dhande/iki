"""``iki-server serve``: runs the ASGI server."""

from typing import Any

import environ
import uvicorn
from django.core.management.base import BaseCommand, CommandParser

env = environ.Env()


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
        uvicorn.run(
            "iki_server.asgi:application",
            host=options["host"],
            port=options["port"],
            workers=options["workers"],
            reload=options["reload"],
            # Django has no ASGI lifespan support; turning it off avoids a
            # misleading startup warning.
            lifespan="off",
            # Logging is already configured by Django settings (JSON lines).
            log_config=None,
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips=options["forwarded_allow_ips"],
            server_header=False,
            timeout_graceful_shutdown=30,
            timeout_keep_alive=options["keep_alive"],
        )

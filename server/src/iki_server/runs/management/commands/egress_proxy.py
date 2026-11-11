"""``iki-server egress_proxy``: the proxy through which runs reach the hosts their action
declares (ADR 0053), until stopped (SIGTERM or Ctrl-C)."""

from typing import Any

import environ
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from iki_server.observability.telemetry import configure_telemetry
from iki_server.runs import egress_proxy

env = environ.Env()


class Command(BaseCommand):
    help = "Run the egress proxy: runs reach the hosts their action declares, and no others."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--host",
            default=env.str("IKI_EGRESS_HOST", default="0.0.0.0"),  # noqa: S104 - a pod's port
            help="Address to listen on.",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=env.int("IKI_EGRESS_PORT", default=3128),
            help="Port to listen on.",
        )
        parser.add_argument(
            "--grace",
            type=float,
            default=env.float("IKI_EGRESS_GRACE_SECONDS", default=25.0),
            help="Seconds open connections get to finish after SIGTERM.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        configure_telemetry(
            service_version=settings.IKI_VERSION, environment=settings.IKI_ENVIRONMENT
        )
        egress_proxy.run(options["host"], options["port"], grace=options["grace"])

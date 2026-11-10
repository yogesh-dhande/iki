"""``iki-server tasks``: runs background tasks until stopped (SIGTERM or Ctrl-C)."""

from typing import Any

import environ
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from iki_server.observability.telemetry import configure_telemetry
from iki_server.tasks.runner import Runner

env = environ.Env()


class Command(BaseCommand):
    help = "Run background tasks: email, webhooks and other work done outside requests."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--queue",
            action="append",
            dest="queues",
            help="A queue to take tasks from (repeat for more). Default: every queue.",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=env.int("IKI_TASKS_CONCURRENCY", default=4),
            help="Tasks run at once. Scale out with more runners, not just more threads.",
        )
        parser.add_argument(
            "--grace",
            type=float,
            default=env.float("IKI_TASKS_GRACE_SECONDS", default=25.0),
            help=(
                "Seconds running tasks get to finish after SIGTERM, before another runner "
                "retries them."
            ),
        )
        parser.add_argument(
            "--health-port",
            type=int,
            default=env.int("IKI_TASKS_HEALTH_PORT", default=8001),
            help="Port for /healthz/live and /healthz/ready (0 turns them off).",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        if options["concurrency"] < 1:
            self.stderr.write("--concurrency must be at least 1.")
            raise SystemExit(2)
        configure_telemetry(
            service_version=settings.IKI_VERSION, environment=settings.IKI_ENVIRONMENT
        )
        queues = options["queues"] or sorted(settings.TASKS["default"]["QUEUES"])
        runner = Runner(queues=queues, concurrency=options["concurrency"])
        runner.serve(grace=options["grace"], health_port=options["health_port"] or None)

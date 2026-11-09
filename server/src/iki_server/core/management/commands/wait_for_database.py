"""``iki-server wait_for_database``: blocks until Postgres accepts connections.

Runs before migrations (as an init container), so a database that is still
starting, or failing over, delays the deploy instead of failing it.
"""

import time
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DEFAULT_DB_ALIAS, connections

logger = structlog.get_logger("iki.wait_for_database")


class Command(BaseCommand):
    help = "Wait until the database accepts connections."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--timeout", type=float, default=300.0, help="Give up after this many seconds."
        )
        parser.add_argument("--interval", type=float, default=2.0, help="Seconds between attempts.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        timeout: float = options["timeout"]
        interval: float = options["interval"]
        deadline = time.monotonic() + timeout
        attempt = 0
        connection = connections[DEFAULT_DB_ALIAS]
        while True:
            attempt += 1
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
            except Exception as exc:
                connection.close()
                if time.monotonic() + interval > deadline:
                    msg = f"database not reachable after {timeout:.0f}s ({attempt} attempts)"
                    raise CommandError(msg) from exc
                # Only the exception type: messages can contain connection details.
                logger.info("database not ready", attempt=attempt, error=type(exc).__name__)
                time.sleep(interval)
            else:
                logger.info("database ready", attempt=attempt)
                return

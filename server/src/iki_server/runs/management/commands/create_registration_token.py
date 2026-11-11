"""``iki-server create_registration_token``: makes a token a pool of workers registers with.

For installs set up from code rather than by hand, such as a GitOps-managed
cluster, and the dev cluster. Run it inside the cluster and store what it
prints in the pool's Secret::

    kubectl exec -n iki deploy/iki-web -- iki-server create_registration_token \\
      --tier backend --label "Backend pool" > token
    kubectl create secret generic iki-worker-registration -n iki --from-file=token

Being able to run it is the proof of authority, as for ``setup_link``. The
audit log records it as made from the command line. Compute admins see the
token in Compute settings, and can revoke it there.
"""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.runs import workers

#: The tiers whose workers register with a token. Client-tier workers sign in as their person.
TIERS = ("backend", "container", "vm")


@contextmanager
def _logs_to_stderr() -> Iterator[None]:
    """Sends log lines (such as the audit event's) to standard error meanwhile, so standard
    output holds only the token, wherever the logs normally go."""
    loggers = [logging.getLogger(), *map(logging.getLogger, logging.root.manager.loggerDict)]
    to_stdout = [
        handler
        for logger in loggers
        for handler in getattr(logger, "handlers", [])
        if isinstance(handler, logging.StreamHandler)
        and handler.stream in (sys.stdout, sys.__stdout__)
    ]
    moved = [(handler, handler.setStream(sys.stderr)) for handler in to_stdout]
    try:
        yield
    finally:
        for handler, stream in moved:
            handler.setStream(stream)


class Command(BaseCommand):
    help = "Make a worker registration token, and print it (only it) on standard output."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tier", required=True, choices=TIERS)
        parser.add_argument(
            "--label", required=True, help="What it's for, such as 'GPU pool (us-east-1)'."
        )
        parser.add_argument(
            "--expires-in-days",
            type=int,
            help="When it stops working. Workers already registered keep working.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        label: str = options["label"].strip()
        if not label or len(label) > 100:
            raise CommandError("--label must be 1 to 100 characters.")
        days: int | None = options["expires_in_days"]
        if days is not None and not 1 <= days <= 3650:
            raise CommandError("--expires-in-days must be between 1 and 3650.")
        expires_at = timezone.now() + timedelta(days=days) if days else None
        with _logs_to_stderr(), audit.cli():
            token, value = workers.create_registration(
                label=label, tier=options["tier"], by=None, expires_at=expires_at
            )
        self.stderr.write(
            f"Made registration token …{token.hint} for {token.tier} workers ({token.label}). "
            "It isn't shown again: store it as the workers' secret."
        )
        self.stdout.write(value, ending="")

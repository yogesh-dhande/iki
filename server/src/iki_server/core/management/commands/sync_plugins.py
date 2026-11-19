"""``iki-server sync_plugins``: triggers, HRID sequences, comments and views for installed plugins.

``migrate_deployment`` does this in its transaction on every deploy (ADR 0134). Run it
alone to make them again outside a deploy. See :mod:`iki_server.plugins.sync`.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from iki_server.audit import log as audit
from iki_server.entities import constraints, unique
from iki_server.plugins import sync


class Command(BaseCommand):
    help = "Make the installed plugins' triggers, HRID sequences, comments and analyst views."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--analyst-role",
            help="A Postgres role to give read-only access to the views (or IKI_DB_ANALYST_ROLE).",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        analyst = options.get("analyst_role") or sync.analyst_role()
        if analyst and (problem := sync.check_analyst(analyst)):
            raise CommandError(problem)
        try:
            with audit.cli(), transaction.atomic():
                report = sync.sync(analyst)
        except (unique.Clash, constraints.Clash) as error:
            raise CommandError(str(error)) from None
        for line in sync.describe(report):
            self.stdout.write(line)

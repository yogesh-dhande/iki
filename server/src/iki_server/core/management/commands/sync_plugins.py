"""``iki-server sync_plugins``: HRID sequences, comments and analyst views for installed plugins.

The Helm migrate Job runs it as the migrator, after ``migrate`` and before
``grant_app_privileges``. See :mod:`iki_server.plugins.sync`.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction

from iki_server.entities import unique
from iki_server.plugins import sync


class Command(BaseCommand):
    help = "Create HRID sequences, comments and analyst views for the installed plugins."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--analyst-role",
            help="A Postgres role to give read-only access to the views (or IKI_DB_ANALYST_ROLE).",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        analyst = options.get("analyst_role") or sync.analyst_role()
        if analyst:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [analyst])
                if cursor.fetchone() is None:
                    raise CommandError(f"Postgres has no role {analyst!r}. Create it first.")
        try:
            with transaction.atomic():
                report = sync.sync(analyst)
        except unique.Clash as error:
            raise CommandError(str(error)) from None
        for name in report.sequences:
            self.stdout.write(f"  HRID sequence {name}")
        for key in report.unique:
            self.stdout.write(f"  unique {key}")
        for name in report.views:
            self.stdout.write(f"  view {name}")
        if report.analyst:
            self.stdout.write(f"  {report.analyst} can read the views")
        self.stdout.write(
            f"Synced {len(report.views)} entity type(s): {len(report.sequences)} HRID "
            f"sequence(s), {report.comments} comment(s)."
        )

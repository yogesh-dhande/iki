"""``iki-server migrate_deployment``: migrate the database to this build, forwards or back.

The Helm migrate Job runs it as the migrator, in place of ``migrate``. See
:mod:`iki_server.plugins.deploy`.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from iki_server.plugins.deploy import DeployError, migrate_deployment


class Command(BaseCommand):
    help = "Migrate the database to this build: reverse plugin migrations it lacks, then migrate."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        try:
            report = migrate_deployment(self.stdout)
        except DeployError as error:
            raise CommandError(str(error)) from None
        for app, name in report.reversed:
            self.stdout.write(f"  Reversed {app}.{name} (rolled back)")
        for app, name in report.archived:
            self.stdout.write(f"  Kept a copy of {app}.{name}, to reverse it on a rollback")
        self.stdout.write("The database matches this build.")

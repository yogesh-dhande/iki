"""``iki-server migrate_deployment``: migrate the database to this build, forwards or back.

The Helm migrate Job runs it as the migrator, in place of ``migrate``,
``sync_plugins`` and ``grant_app_privileges``: after Iki's own migrations, it does
the three in one transaction (ADR 0134). See :mod:`iki_server.plugins.deploy`.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from iki_server.core.management.commands.grant_app_privileges import app_role, wait_for
from iki_server.entities import constraints, unique
from iki_server.plugins import sync
from iki_server.plugins.deploy import KEPT_NOTHING, DeployError, migrate_deployment
from iki_spec import naming


class Command(BaseCommand):
    help = (
        "Migrate the database to this build: reverse plugin migrations it lacks, migrate, "
        "sync the plugins and grant the app role its privileges, in one transaction."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--role", help="The app's Postgres role (or IKI_DB_APP_ROLE, or IKI_APP_DATABASE_URL)."
        )
        parser.add_argument(
            "--no-grant",
            action="store_true",
            help="Grant no app role anything: for a database with one role, such as a test's.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=120,
            help="Seconds to wait for the app's role to exist (an operator may be creating it).",
        )
        parser.add_argument(
            "--analyst-role",
            help="A Postgres role to give read-only access to the views (or IKI_DB_ANALYST_ROLE).",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        role = None
        if not options["no_grant"]:
            try:
                role = app_role(options.get("role"))
            except CommandError as error:
                raise CommandError(
                    f"{error} Or pass --no-grant for a database with one role."
                ) from None
            wait_for(role, options["timeout"], self.stderr)
        analyst = options.get("analyst_role") or sync.analyst_role()
        if analyst and (problem := sync.check_analyst(analyst)):
            raise CommandError(problem)
        try:
            report = migrate_deployment(self.stdout, app_role=role, analyst_role=analyst)
        except DeployError as error:
            raise CommandError(str(error)) from None
        except (unique.Clash, constraints.Clash) as error:
            raise CommandError(f"{error}\n{KEPT_NOTHING}") from None
        for line in report.purged:
            self.stdout.write(f"  Purged {line}")
        for app, name in report.reversed:
            self.stdout.write(f"  Reversed {_plugin(app)} {name} (rolled back)")
        for app, name in report.archived:
            self.stdout.write(
                f"  Kept a copy of {_plugin(app)} {name}, to reverse it on a rollback"
            )
        for line in sync.describe(report.synced):
            self.stdout.write(line)
        for statement in report.granted:
            self.stdout.write(f"  {statement}")
        if role:
            self.stdout.write(f"Granted app privileges to {role}.")
        self.stdout.write(
            "The database matches this build: the plugins' migrations, their guards, views "
            "and grants committed in one transaction."
        )


def _plugin(label: str) -> str:
    """The plugin an app label is, by name: ``iki/protein`` for ``iki__protein``."""
    return naming.plugin_of_label(label) or label

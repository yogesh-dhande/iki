"""``iki-server grant_app_privileges``: gives the app's database role what it needs, no more.

The Helm migrate Job runs it as the migrator, right after ``migrate``, so new
tables are usable by the app before new web pods start (ADR 0025). The app
role's name comes from ``--role``, ``IKI_DB_APP_ROLE``, or the user name in
``IKI_APP_DATABASE_URL``.
"""

import os
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction

from iki_server.core import db_roles


def app_role(explicit: str | None) -> str:
    if explicit:
        return explicit
    if role := os.environ.get("IKI_DB_APP_ROLE"):
        return role
    if url := os.environ.get("IKI_APP_DATABASE_URL"):
        user = urlsplit(url).username
        if user:
            return unquote(user)
    raise CommandError("Name the app's role with --role, IKI_DB_APP_ROLE or IKI_APP_DATABASE_URL.")


class Command(BaseCommand):
    help = "Grant the app's database role exactly the privileges it needs."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--role", help="The app's Postgres role.")
        parser.add_argument(
            "--timeout",
            type=float,
            default=120,
            help="Seconds to wait for the role to exist (an operator may still be creating it).",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        role = app_role(options.get("role"))
        deadline = time.monotonic() + options["timeout"]
        while True:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_user, EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                    [role],
                )
                current, exists = cursor.fetchone()
            if exists or role == current or time.monotonic() >= deadline:
                break
            self.stderr.write(f"Waiting for Postgres role {role!r}…")
            time.sleep(2)
        if role == current:
            raise CommandError(
                f"The app and the migrator are both {role!r}. Give the app its own role, so it "
                "can't change the schema or the audit log."
            )
        if not exists:
            raise CommandError(f"Postgres has no role {role!r}. Create it first.")
        with transaction.atomic():
            statements = db_roles.grant(role)
        for statement in statements:
            self.stdout.write(f"  {statement}")
        self.stdout.write(f"Granted app privileges to {role}.")

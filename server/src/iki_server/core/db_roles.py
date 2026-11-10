"""Least privilege in Postgres: the migrator changes the schema, the app only uses it.

Two roles (ADR 0025):

- The **migrator** owns every table. Only the Helm migrate Job connects as
  it, to run ``migrate`` and then ``grant_app_privileges``.
- The **app** role (web pods, maintenance jobs) can read and write rows, but
  can't create, alter or drop anything. On append-only tables (the audit log)
  it can only INSERT and SELECT. It can only read Django's migration history.

:func:`problems` reports what is wrong with the current connection's role,
so a web pod connected with too much power never turns ready.
"""

from collections.abc import Iterable

from django.apps import apps
from django.db import connection
from django.db.backends.utils import CursorWrapper
from psycopg import sql

#: Tables the app may only read.
READ_ONLY_TABLES = ("django_migrations",)


def append_only_tables() -> list[str]:
    """Tables of models that declare ``APPEND_ONLY = True``."""
    return sorted(
        model._meta.db_table for model in apps.get_models() if getattr(model, "APPEND_ONLY", False)
    )


def _existing(cursor: CursorWrapper, tables: Iterable[str]) -> list[str]:
    cursor.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
        "AND tablename = ANY(%s)",
        [list(tables)],
    )
    return sorted(row[0] for row in cursor.fetchall())


def grant(app_role: str) -> list[str]:
    """Gives ``app_role`` exactly the privileges the app needs. Idempotent.

    Runs as the migrator (the owner of every table). Returns the statements
    it ran, for the command's output.
    """
    role = sql.Identifier(app_role)
    statements: list[sql.Composable] = [
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(connection.settings_dict["NAME"]), role
        ),
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier("public"), role),
        sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(sql.Identifier("public"), role),
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(
            role
        ),
        sql.SQL(
            "REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM {}"
        ).format(role),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role),
    ]
    with connection.cursor() as cursor:
        statements += [
            sql.SQL("REVOKE UPDATE, DELETE ON {} FROM {}").format(sql.Identifier(table), role)
            for table in _existing(cursor, append_only_tables())
        ]
        statements += [
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {} FROM {}").format(
                sql.Identifier(table), role
            )
            for table in _existing(cursor, READ_ONLY_TABLES)
        ]
        rendered = []
        for statement in statements:
            cursor.execute(statement)
            rendered.append(statement.as_string(cursor.connection))
    return rendered


def problems() -> list[str]:
    """Why the current database role has more power than the app should. Empty if none."""
    found = []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        superuser, createrole, createdb, bypassrls = cursor.fetchone()
        if superuser:
            found.append("is a superuser")
        if createrole:
            found.append("can create roles")
        if createdb:
            found.append("can create databases")
        if bypassrls:
            found.append("bypasses row-level security")
        cursor.execute("SELECT has_schema_privilege(current_user, current_schema(), 'CREATE')")
        if cursor.fetchone()[0]:
            found.append("can create objects in the schema")
        cursor.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema() "
            "AND pg_has_role(current_user, tableowner, 'USAGE')"
        )
        if cursor.fetchone()[0]:
            found.append("owns tables (or is a member of their owner)")
        for table in _existing(cursor, append_only_tables()):
            for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                cursor.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)", [table, privilege]
                )
                if cursor.fetchone()[0]:
                    found.append(f"can {privilege} {table}")
    return found

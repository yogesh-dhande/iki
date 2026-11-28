"""Compare a migrated record table and its latest append-only snapshots in one SQL snapshot."""

from collections.abc import Iterable, Mapping
from copy import copy
from functools import partial
from typing import Any

from django.db import connection, migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from psycopg import sql

from iki_server.entities import registry
from iki_server.entities.values import Part, row_columns, shape
from iki_server.plugins.operations import MigrateValues
from iki_server.plugins.preflight_records import lineage
from iki_spec import naming
from iki_spec.model import Field, Plugin, stored_in_list_table


def shadowed_history(before: Mapping[str, Plugin], after: Mapping[str, Plugin]) -> Iterable[str]:
    """A field's previous snapshots stay in the table that declared it. Reading the
    same name from the other side of an inheritance link would read different evidence.
    """
    if before == after:
        return
    old = {n: t for plugin in before.values() for n, t in plugin.types.items()}
    new = {n: t for plugin in after.values() for n, t in plugin.types.items()}
    for name in sorted(old.keys() & new.keys()):
        for declared, schema in ((old[name], after), (new[name], before)):
            for parent in lineage(name, schema)[1:]:
                for field in sorted(declared.fields.keys() & parent.current_fields.keys()):
                    yield (
                        f"{name}.{field} and its parent {parent.qualified_name}.{field} use "
                        "different history tables. Give the new field a distinct name so "
                        "earlier revisions still read from their original table."
                    )


def differences(operations: list[Operation]) -> Iterable[str]:
    if not operations:
        return
    parts = {p.model: p for t in registry.types() if t != naming.ENTITY for p in shape(t).parts}
    for part in parts.values():
        models = [part.model, part.history] + [
            part.list_model(f.name)
            for f in part.fields.values()
            if not f.removed and stored_in_list_table(f)
        ]
        if any(
            op.references_model(model.__name__, model._meta.app_label)
            for model in models
            for op in operations
        ):
            yield from _differences(part)


def _differences(part: Part) -> Iterable[str]:
    # A part can have fewer history rows than the entity has revisions. The latest
    # row of this part remains authoritative until a revision touches it again.
    fields = [f for f in part.fields.values() if not f.removed]
    comparisons: list[sql.Composable] = [
        sql.SQL("({}, {}, {})").format(sql.Literal(f.name), *_values(part, f)) for f in fields
    ]
    # Include both sides so losing a row is detectable even on a part with no fields.
    comparisons.append(sql.SQL("('record', to_jsonb(t.entity_id), to_jsonb(h.entity_id))"))
    comparisons.append(sql.SQL("('history revision', to_jsonb(h.revision), to_jsonb(r.number))"))
    query = sql.SQL("""
        SELECT e.hrid, COALESCE(h.revision, e.revision), v.field, v.live::text, v.kept::text
        FROM {} t FULL JOIN (
            SELECT DISTINCT ON (entity_id) * FROM {} ORDER BY entity_id, revision DESC
        ) h USING (entity_id)
        JOIN iki_entity e ON e.id = COALESCE(t.entity_id, h.entity_id)
        LEFT JOIN iki_revision r ON r.entity_id = e.id
            AND r.number = h.revision AND r.number <= e.revision
        CROSS JOIN LATERAL (VALUES {}) v(field, live, kept)
        WHERE v.live IS DISTINCT FROM v.kept
        ORDER BY e.hrid, v.field LIMIT 10
    """).format(
        sql.Identifier(part.model._meta.db_table),
        sql.Identifier(part.history._meta.db_table),
        sql.SQL(", ").join(comparisons),
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        for hrid, revision, field, live, kept in cursor.fetchall():
            if field == "history revision":
                yield f"{hrid}'s latest snapshot names revision {revision}, outside its trail."
                continue
            yield (
                f"{hrid}'s {field} is {live or 'null'}, but its revision {revision} "
                f"holds {kept or 'null'}."
            )


def _column(model: type[models.Model], name: str, alias: str) -> sql.Identifier:
    field = model._meta.get_field(name)
    assert isinstance(field, models.Field) and field.column  # noqa: S101 - a manifest column
    return sql.Identifier(alias, field.column)


def _values(part: Part, field: Field) -> tuple[sql.Composable, sql.Composable]:
    kept = _column(part.history, field.name, "h")
    if not stored_in_list_table(field):
        live = _column(part.model, field.name, "t")
        return sql.SQL("to_jsonb({})").format(live), sql.SQL("to_jsonb({})").format(kept)
    model = part.list_model(field.name)
    table = sql.Identifier(model._meta.db_table)
    if not field.is_rows:
        return (
            sql.SQL(
                "to_jsonb(ARRAY(SELECT item_id FROM {} "
                "WHERE entity_id=t.entity_id ORDER BY position))"
            ).format(table),
            sql.SQL("COALESCE(to_jsonb({}), '[]'::jsonb)").format(kept),
        )

    # Decode both sides as the declared SQL types. In particular this gives timestamps
    # the same spelling and reads a cell added to retained rows as NULL.
    columns = row_columns(field)
    live_cells = sql.SQL(", ").join(
        sql.SQL("{} AS {}").format(sql.Identifier(col), sql.Identifier(name))
        for name, col in columns.items()
    )
    schema = sql.SQL(", ").join(
        sql.SQL("{} {}").format(
            sql.Identifier(name), sql.SQL(str(model._meta.get_field(name).db_type(connection)))
        )
        for name in columns
    )
    return (
        sql.SQL(
            "to_jsonb(ARRAY(SELECT to_jsonb(r) FROM (SELECT {} FROM {} "
            "WHERE entity_id=t.entity_id ORDER BY position) r))"
        ).format(live_cells, table),
        sql.SQL("to_jsonb(ARRAY(SELECT to_jsonb(r) FROM jsonb_to_recordset({}) AS r({})))").format(
            kept, schema
        ),
    )


def guard_raw_operations(operations: Iterable[Operation]) -> list[Operation]:
    """Bracket opaque operations in execution order, including when Django reverses them.
    A migrator owns the tables and can temporarily take off their append-only triggers.
    """
    guarded: list[Operation] = []
    for op in operations:
        if isinstance(op, migrations.SeparateDatabaseAndState):
            op = copy(op)
            op.database_operations = guard_raw_operations(op.database_operations)
        if isinstance(op, migrations.RunSQL | migrations.RunPython | MigrateValues) or not (
            type(op).__module__.startswith("django.db.migrations.operations.")
            or type(op).__module__ == "iki_server.plugins.operations"
        ):
            migrated = isinstance(op, MigrateValues)
            save = partial(_save_history, migrated=migrated)
            check = partial(_check_history, migrated=migrated)
            guarded.extend(
                [
                    migrations.RunPython(save, check),
                    op,
                    migrations.RunPython(check, save),
                ]
            )
        else:
            guarded.append(op)
    return guarded


def _history_tables(editor: BaseDatabaseSchemaEditor) -> list[str]:
    with editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
            "AND right(tablename, 9) = '__history' ORDER BY tablename"
        )
        return [table for (table,) in cursor.fetchall()]


# A newly added NULL column adds no value to the snapshot. Strip only top-level
# SQL nulls so a null key inside a stored JSON object still contributes to its digest.
_DIGEST = """(SELECT sha256(convert_to(jsonb_object_agg(key, value)::text, 'UTF8'))
             FROM jsonb_each(to_jsonb(h)) WHERE value <> 'null'::jsonb)"""


def _save_history(apps: Any, editor: BaseDatabaseSchemaEditor, *, migrated: bool) -> None:  # noqa: ANN401 - migration callback
    if not migrated:
        # Opaque code can write any record, including from a nested savepoint.
        # Finish concurrent revision writers first. From here until commit, another
        # session cannot append a revision between our before and after snapshots.
        editor.execute("LOCK TABLE iki_revision IN SHARE MODE")
    # PostgreSQL updates the trigger's catalog tuple when it is disabled, even if
    # the same operation enables it again. This covers empty tables and indirect SQL.
    editor.execute("""
        CREATE TEMP TABLE iki_kept_guards ON COMMIT DROP AS
        SELECT t.oid, t.ctid AS original, c.relname, t.tgname
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relnamespace = current_schema()::regnamespace
          AND t.tgname IN ('iki_history_no_change', 'iki_history_no_truncate')
    """)
    # Read the heads first. A concurrent writer may append a later revision while
    # these snapshots are collected, but adding history to an older one rewrites it.
    editor.execute(
        "CREATE TEMP TABLE iki_kept_heads ON COMMIT DROP AS SELECT id, revision FROM iki_entity"
    )
    editor.execute("ALTER TABLE iki_kept_heads ADD PRIMARY KEY (id)")
    editor.execute(
        "CREATE TEMP TABLE iki_kept_history (relation text, entity_id uuid, revision integer, "
        "digest bytea, PRIMARY KEY(relation, entity_id, revision)) ON COMMIT DROP"
    )
    for table in _history_tables(editor):
        editor.execute(
            "INSERT INTO iki_kept_history SELECT %s, entity_id, revision, "  # noqa: S608 - quoted table
            f"{_DIGEST} "
            f"FROM {editor.quote_name(table)} h",
            [table],
        )


def _check_history(apps: Any, editor: BaseDatabaseSchemaEditor, *, migrated: bool) -> None:  # noqa: ANN401 - migration callback
    from iki_server.plugins.deploy import DeployError

    with editor.connection.cursor() as cursor:
        cursor.execute("""
            SELECT k.relname, k.tgname FROM iki_kept_guards k
            LEFT JOIN pg_trigger t ON t.oid = k.oid
            WHERE t.ctid IS DISTINCT FROM k.original OR t.tgenabled NOT IN ('O', 'A')
            LIMIT 1
        """)
        if guard := cursor.fetchone():
            raise DeployError(f"Migration changed {guard[0]}'s append-only guard {guard[1]}.")
        cursor.execute("SELECT DISTINCT relation FROM iki_kept_history ORDER BY relation")
        saved = [table for (table,) in cursor.fetchall()]
        tables = _history_tables(editor)
        for table in set(saved) - set(tables):
            raise DeployError(f"Migration removed {table} and its recorded history.")
        for table in tables:
            cursor.execute(
                sql.SQL("""
                    SELECT COALESCE(e.hrid, k.entity_id::text, h.entity_id::text),
                           COALESCE(k.revision, h.revision),
                           k.digest IS NOT NULL OR h.revision <= b.revision
                    FROM (SELECT * FROM iki_kept_history WHERE relation = %s) k
                    FULL JOIN {} h USING (entity_id, revision)
                    LEFT JOIN iki_entity e ON e.id = COALESCE(k.entity_id, h.entity_id)
                    LEFT JOIN iki_kept_heads b ON b.id = h.entity_id
                    WHERE (k.digest IS NOT NULL AND (h.entity_id IS NULL OR
                           {} <> k.digest))
                       OR (k.digest IS NULL AND b.id IS NOT NULL
                           AND (h.revision <= b.revision OR NOT %s))
                    LIMIT 1
                """).format(sql.Identifier(table), sql.SQL(_DIGEST)),
                [table, migrated],
            )
            if row := cursor.fetchone():
                change = "rewritten" if row[2] else "appended by a raw migration"
                raise DeployError(
                    f"{row[0]}'s revision {row[1]} in {table} was {change}. "
                    "Record backfills with MigrateValues."
                )
    editor.execute("DROP TABLE iki_kept_history, iki_kept_heads, iki_kept_guards")

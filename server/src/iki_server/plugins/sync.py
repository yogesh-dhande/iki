"""What installed plugins need besides their migrations: ``iki-server sync_plugins``.

The Helm migrate Job runs it as the migrator, after ``migrate`` and before
``grant_app_privileges``. It is idempotent, and works from the manifests the
build installed (:mod:`iki_server.entities.registry`):

- **HRID sequences**, one per prefix (``iki_hrid_prt``). They are created if
  missing and never dropped, so a number is never handed out twice.
- **Comments** on every plugin table and column: labels, descriptions,
  units and what references point to. Database tools and data catalogs
  show them, and they change without a migration.
- **Read-only views** for analysts and BI tools, in the schema ``iki_views``.
  There is one per type (``iki_views.protein_protein``), joining the core
  entity row, the type's table, its parents' tables and every installed
  extension. Lists of references or files appear as arrays. The views use
  ``security_invoker``, so whoever queries one needs the right to read what
  it reads.
- **An analyst role**, if ``IKI_DB_ANALYST_ROLE`` names one. It gets
  read-only access to the views and the tables behind them, current values
  and history, and nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.apps import apps
from django.db import connection, models
from psycopg import sql

from iki_server.entities import hrids, registry
from iki_spec import naming
from iki_spec.model import EntityType, Extension, Field

VIEWS_SCHEMA = "iki_views"

#: The core entity row's columns, first in every view.
ENTITY_COLUMNS = (
    "id",
    "hrid",
    "type",
    "owner_project_id",
    "created_by_id",
    "created_at",
    "archived_at",
)


@dataclass
class Report:
    sequences: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    comments: int = 0
    analyst: str | None = None


def sync(analyst_role: str | None = None) -> Report:
    """Runs every step, in one transaction (the caller's)."""
    report = Report()
    with connection.cursor() as cursor:
        for prefix in sorted(_prefixes()):
            name = hrids.sequence_name(prefix)
            cursor.execute(
                sql.SQL("CREATE SEQUENCE IF NOT EXISTS {} AS bigint MINVALUE 1 NO CYCLE").format(
                    sql.Identifier(name)
                )
            )
            cursor.execute(
                sql.SQL("COMMENT ON SEQUENCE {} IS {}").format(
                    sql.Identifier(name),
                    sql.Literal(f"HRID numbers for the prefix {prefix}. Never reset or reused."),
                )
            )
            report.sequences.append(name)
        for statement in _comments():
            cursor.execute(statement)
            report.comments += 1
        report.views = _views(cursor)
        if analyst_role:
            for statement in _analyst_grants(analyst_role):
                cursor.execute(statement)
            report.analyst = analyst_role
    return report


def analyst_role() -> str | None:
    return os.environ.get("IKI_DB_ANALYST_ROLE") or None


def _prefixes() -> set[str]:
    return {
        t.hrid_prefix
        for t in registry.types().values()
        if t.hrid_prefix is not None and not t.abstract
    }


# --- Models ------------------------------------------------------------------------------------


def _type_model(t: EntityType) -> type[models.Model]:
    return registry.model(t.qualified_name)


def _extension_model(e: Extension) -> type[models.Model]:
    return apps.get_model(e.plugin, naming.extension_model(e.extends_type))


def _history_model(plugin: str, model: type[models.Model]) -> type[models.Model]:
    return apps.get_model(plugin, naming.history_model(model.__name__))


def _list_model(plugin: str, owner: type[models.Model], f: Field) -> type[models.Model]:
    return apps.get_model(plugin, naming.list_model(owner.__name__, f.name))


def _is_list_table(f: Field) -> bool:
    return f.type == "list" and f.items is not None and f.items.type in ("ref", "file")


def _column(model: type[models.Model], name: str) -> str:
    column = model._meta.get_field(name).column  # type: ignore[union-attr]
    return str(column)


def _tables() -> Iterable[tuple[str, type[models.Model], dict[str, Field], str]]:
    """(plugin, model, fields, description) for every type's and extension's own table."""
    for t in registry.types().values():
        if t.plugin == "core":
            continue
        described = f"{t.label} ({t.qualified_name}), schema version {t.schema_version}."
        yield t.plugin, _type_model(t), t.fields, _sentence(described, t.description)
    for e in registry.extensions():
        described = f"Fields the {e.plugin} plugin adds to {e.extends_type}."
        yield e.plugin, _extension_model(e), e.fields, _sentence(described, e.description)


# --- Comments ----------------------------------------------------------------------------------


def _sentence(*parts: str | None) -> str:
    out = []
    for part in parts:
        if part:
            text = part.strip()
            out.append(text if text.endswith((".", "!", "?")) else text + ".")
    return " ".join(out)


def field_comment(f: Field) -> str:
    """What a column holds: its label and description, its unit, what it refers to."""
    item = f.items if f.type == "list" and f.items is not None else f
    unit = f"In {item.unit} (UCUM)." if item.type == "quantity" else None
    target = f"Refers to a {item.to}." if item.type == "ref" else None
    blob = "Refers to a file (iki_blob)." if item.type == "file" else None
    removed = "Removed: kept for history only." if f.removed else None
    return _sentence(f.label, f.description, unit, target, blob, removed)


def _comment_on_table(table: str, text: str, *, schema: str | None = None) -> sql.Composable:
    name = sql.Identifier(schema, table) if schema else sql.Identifier(table)
    kind = sql.SQL("VIEW") if schema else sql.SQL("TABLE")
    return sql.SQL("COMMENT ON {} {} IS {}").format(kind, name, sql.Literal(text))


def _comment_on_column(
    table: str, column: str, text: str, *, schema: str | None = None
) -> sql.Composable:
    name = sql.Identifier(schema, table, column) if schema else sql.Identifier(table, column)
    return sql.SQL("COMMENT ON COLUMN {} IS {}").format(name, sql.Literal(text))


def _comments() -> list[sql.Composable]:
    out: list[sql.Composable] = []
    for plugin, model, fields, described in _tables():
        table = model._meta.db_table
        history = _history_model(plugin, model)
        out.append(_comment_on_table(table, described))
        out.append(
            _comment_on_table(
                history._meta.db_table,
                f"History of {table}: its values at each revision of each entity. Append-only.",
            )
        )
        for f in fields.values():
            text = field_comment(f)
            out.append(_comment_on_column(history._meta.db_table, _column(history, f.name), text))
            if f.removed:
                continue
            if _is_list_table(f):
                items = _list_model(plugin, model, f)
                out.append(
                    _comment_on_table(
                        items._meta.db_table,
                        f"Items of {table}.{f.name}, one row each, in order of position. {text}",
                    )
                )
            else:
                out.append(_comment_on_column(table, _column(model, f.name), text))
    return out


# --- Views -------------------------------------------------------------------------------------


def _views(cursor: Any) -> list[str]:  # noqa: ANN401 - Django's cursor
    cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(VIEWS_SCHEMA)))
    cursor.execute(
        sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
            sql.Identifier(VIEWS_SCHEMA),
            sql.Literal("Read-only views of every entity type, for analysts. Managed by Iki."),
        )
    )
    # Views are rebuilt from scratch, since a changed type may have changed their
    # columns. A view someone else built on one of these makes the drop fail,
    # and the deploy with it, rather than being dropped silently.
    cursor.execute(
        "SELECT viewname FROM pg_views WHERE schemaname = %s ORDER BY viewname", [VIEWS_SCHEMA]
    )
    for (name,) in cursor.fetchall():
        cursor.execute(sql.SQL("DROP VIEW {}").format(sql.Identifier(VIEWS_SCHEMA, name)))
    made = []
    for t in sorted(registry.types().values(), key=lambda t: t.qualified_name):
        if t.plugin == "core":
            continue
        name, select, comments = _view(t)
        cursor.execute(
            sql.SQL("CREATE VIEW {} WITH (security_invoker = true) AS {}").format(
                sql.Identifier(VIEWS_SCHEMA, name), select
            )
        )
        for statement in comments:
            cursor.execute(statement)
        made.append(f"{VIEWS_SCHEMA}.{name}")
    return made


def _view(t: EntityType) -> tuple[str, sql.Composable, list[sql.Composable]]:
    """The view's name, its SELECT, and its comments."""
    name = t.table
    columns: list[sql.Composable] = [
        sql.SQL("e.{}").format(sql.Identifier(c)) for c in ENTITY_COLUMNS
    ]
    joins: list[sql.Composable] = []
    comments: list[sql.Composable] = [
        _comment_on_table(
            name,
            _sentence(
                f"Every {t.label} ({t.qualified_name}) with its fields, its parent types' and "
                "those other plugins add. Read-only.",
                t.description,
            ),
            schema=VIEWS_SCHEMA,
        )
    ]
    chain = [*reversed(registry.ancestors(t)), t]
    parts: list[tuple[str, type[models.Model], dict[str, Field], bool]] = [
        (owner.plugin, _type_model(owner), owner.fields, True) for owner in chain
    ]
    names = {owner.qualified_name for owner in chain}
    parts += [
        (e.plugin, _extension_model(e), e.fields, False)
        for e in registry.extensions()
        if e.extends_type in names
    ]
    for index, (plugin, model, fields, inner) in enumerate(parts):
        alias = sql.Identifier(f"t{index}")
        join = sql.SQL("JOIN") if inner else sql.SQL("LEFT JOIN")
        joins.append(
            sql.SQL(" {} {} {} ON {}.entity_id = e.id").format(
                join, sql.Identifier(model._meta.db_table), alias, alias
            )
        )
        for f in fields.values():
            if f.removed:
                continue
            if _is_list_table(f):
                items = _list_model(plugin, model, f)
                column = sql.SQL(
                    "ARRAY(SELECT l.item_id FROM {} l WHERE l.entity_id = e.id "
                    "ORDER BY l.position) AS {}"
                ).format(sql.Identifier(items._meta.db_table), sql.Identifier(f.name))
                label = f.name
            else:
                label = _column(model, f.name)
                column = sql.SQL("{}.{}").format(alias, sql.Identifier(label))
            columns.append(column)
            comments.append(_comment_on_column(name, label, field_comment(f), schema=VIEWS_SCHEMA))
    select = sql.SQL("SELECT {} FROM iki_entity e{}").format(
        sql.SQL(", ").join(columns), sql.SQL("").join(joins)
    )
    return name, select, comments


# --- The analyst role ----------------------------------------------------------------------------


def _analyst_grants(role: str) -> list[sql.Composable]:
    who = sql.Identifier(role)
    tables = ["iki_entity"]
    for plugin, model, fields, _ in _tables():
        tables += [model._meta.db_table, _history_model(plugin, model)._meta.db_table]
        tables += [
            _list_model(plugin, model, f)._meta.db_table
            for f in fields.values()
            if _is_list_table(f) and not f.removed
        ]
    return [
        sql.SQL("GRANT USAGE ON SCHEMA {}, public TO {}").format(sql.Identifier(VIEWS_SCHEMA), who),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
            sql.Identifier(VIEWS_SCHEMA), who
        ),
        sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in tables), who
        ),
    ]

"""Unique fields: no two entities in a project share a value (ADR 0082).

A plugin marks an indexed string field ``unique``, such as the ID of the
record a LIMS or ELN keeps for an entity. Each value is held once per owner
project in :class:`UniqueValue`, which triggers on the plugin tables keep in
step (``sync_plugins`` installs them, see :func:`install`), so the database
refuses a duplicate whoever writes it. Before writing, :func:`taken` finds
which entity has a value already, so a person is told which.

Projects are kept apart: the same record may be in two projects, as two
entities, and nothing here says what another project holds.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import IntegrityError, models
from psycopg import sql

from iki_server.entities import registry
from iki_server.entities.models import UNIQUE_CONSTRAINT, UniqueValue
from iki_server.entities.values import Shape
from iki_server.org.models import Project
from iki_spec.model import Field

__all__ = [
    "Clash",
    "Column",
    "clash_message",
    "columns",
    "holders",
    "install",
    "is_clash",
    "key",
    "link",
    "taken",
]

#: The triggers' names start with this; ``sync_plugins`` drops those no field needs.
TRIGGER_PREFIX = "iki_unique__"


@dataclass(frozen=True)
class Column:
    table: str
    column: str

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"


def key(model: type[models.Model], name: str) -> str:
    """The key a field's values are held under: ``table.column``."""
    field = model._meta.get_field(name)
    return f"{model._meta.db_table}.{field.column}"  # type: ignore[union-attr]


def columns(tables: Iterable[tuple[type[models.Model], Mapping[str, Any]]]) -> list[Column]:
    """The unique columns of these (model, fields) tables, fields removed or not unique left out."""
    out = []
    for model, fields in tables:
        for f in fields.values():
            if f.unique and not f.removed:
                column = model._meta.get_field(f.name).column  # type: ignore[union-attr]
                out.append(Column(model._meta.db_table, str(column)))
    return out


def taken(
    project: Project, form: Shape, values: Mapping[str, Any], *, entity: UUID | None = None
) -> dict[str, list[str]]:
    """Each of ``values`` that a unique field of ``form`` has on another entity in the
    project: field name → what's wrong. ``entity`` is the one being changed, if any."""
    errors: dict[str, list[str]] = {}
    for part in form.parts:
        for name, f in part.fields.items():
            value = values.get(name)
            if not f.unique or f.removed or value is None:
                continue
            rows = UniqueValue.objects.filter(
                project=project, key=key(part.model, name), value=str(value)
            )
            if entity is not None:
                rows = rows.exclude(entity_id=entity)
            found = rows.values_list("entity__hrid", flat=True).first()
            if found is not None:
                errors[name] = [f"is {found}’s already: no two entities in a project share it."]
    return errors


def link(f: Field, plugin: str) -> str | None:
    """Where a field's values lead in another system: its link with the plugin's settings
    filled in and ``{value}`` left for each value, or None. Only an https link is given, so a
    deployment's setting can't make one run script or leave TLS."""
    if f.link is None:
        return None
    values = registry.settings(plugin)
    out = re.sub(
        r"\{settings\.([^{}]+)\}", lambda m: str(values.get(m[1]) or "").rstrip("/"), f.link
    )
    if not out.startswith("https://") or "{value}" not in out or len(out) <= len("https://"):
        return None
    return out


def holders(field_key: str, value: str) -> models.QuerySet[UniqueValue, UUID]:
    """The IDs of the entities, in any project, with this value of a unique field: narrow
    them to what the reader can see before showing any."""
    return UniqueValue.objects.filter(key=field_key, value=value).values_list(
        "entity_id", flat=True
    )


class Clash(Exception):  # noqa: N818 - reads as "the values clash"
    """Entities already share a value of a field marked unique, so it can't be."""


def is_clash(error: IntegrityError) -> bool:
    """Whether a database error is a unique field's value written twice in a project."""
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None) == UNIQUE_CONSTRAINT


def clash_message(error: IntegrityError) -> str:
    """What a unique field's value written twice in a project was, in a sentence."""
    detail = getattr(getattr(error.__cause__, "diag", None), "message_detail", None) or ""
    # Key (project_id, key, value)=(…, protein_protein.lims_id, LIMS-1) already exists.
    found = re.search(r"=\([^,]+, ([^,]+), (.*)\) already exists", detail)
    if found is None:
        return "An entity in the project has one of these unique values already."
    return f"An entity in the project has `{found[1]}` “{found[2]}” already, or two lines do."


def install(cursor: Any, wanted: list[Column]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """Triggers keeping ``wanted`` columns' values, and the values already there. Drops
    triggers and values of fields no longer unique. Raises :class:`Clash` when entities in
    a project already share a value: the deploy stops, saying which."""
    keys = [c.key for c in wanted]
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND t.tgname LIKE %s",
        [TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    current = {(table, name) for table, name in cursor.fetchall()}
    needed = {(c.table, _trigger(c)) for c in wanted}
    for table, name in sorted(current - needed):
        cursor.execute(
            sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
        )
    cursor.execute("DELETE FROM iki_unique_value WHERE NOT (key = ANY(%s))", [keys])
    clashes = [found for c in wanted for found in _clashes(cursor, c)]
    if clashes:
        raise Clash(
            "Some entities share a value of a field marked unique, so it can't be until they "
            "don't: " + "; ".join(clashes) + ". Change or clear one of each, or leave "
            "`unique` off, then deploy again."
        )
    for c in wanted:
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE TRIGGER {name} AFTER INSERT OR UPDATE OF {column} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION iki_unique_value_keep({arg}, {key})"
            ).format(
                name=sql.Identifier(_trigger(c)),
                column=sql.Identifier(c.column),
                table=sql.Identifier(c.table),
                arg=sql.Literal(c.column),
                key=sql.Literal(c.key),
            )
        )
        _fill(cursor, c)
    return keys


def _trigger(c: Column) -> str:
    return (TRIGGER_PREFIX + c.column)[:63]


def _clashes(cursor: Any, c: Column) -> list[str]:  # noqa: ANN401
    cursor.execute(
        sql.SQL(
            "SELECT p.name, t.{column}::text, array_agg(e.hrid ORDER BY e.hrid) "
            "FROM {table} t JOIN iki_entity e ON e.id = t.entity_id "
            "JOIN iki_project p ON p.id = e.owner_project_id "
            "WHERE t.{column} IS NOT NULL GROUP BY p.name, t.{column} HAVING count(*) > 1 "
            "ORDER BY p.name, t.{column} LIMIT 10"
        ).format(table=sql.Identifier(c.table), column=sql.Identifier(c.column))
    )
    return [
        f"{', '.join(hrids)} in {project} have `{c.key}` “{value}”"
        for project, value, hrids in cursor.fetchall()
    ]


def _fill(cursor: Any, c: Column) -> None:  # noqa: ANN401
    """Values written while no trigger kept them: a field just marked unique."""
    names = {"table": sql.Identifier(c.table), "column": sql.Identifier(c.column)}
    cursor.execute(
        sql.SQL(
            "DELETE FROM iki_unique_value u USING {table} t WHERE u.key = %s "
            "AND u.entity_id = t.entity_id AND u.value IS DISTINCT FROM t.{column}::text"
        ).format(**names),
        [c.key],
    )
    cursor.execute(
        sql.SQL(
            "INSERT INTO iki_unique_value (project_id, key, value, entity_id) "
            "SELECT e.owner_project_id, %s, t.{column}::text, e.id "
            "FROM {table} t JOIN iki_entity e ON e.id = t.entity_id "
            "WHERE t.{column} IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM iki_unique_value u WHERE u.entity_id = e.id AND u.key = %s)"
        ).format(**names),
        [c.key, c.key],
    )

"""What a reference may refer to, held by the database (ADR 0136).

A reference to one concrete type is a foreign key to that type's table (or its registry
entries'), which holds it to its type. One to an abstract type, or to several types, is a
foreign key to ``iki_entity``, which any entity is in: ``sync_plugins`` puts two statement
triggers on each table holding one, a type's, a list's or a list of rows', which call
``iki_reference_types`` (made by ``iki_entities``' migration) with each such column and the
types it may name, its types and their subtypes. A write naming anything else is refused,
whatever makes it. A trigger whose types are new checks what the table holds already, and
the sync stops, naming what doesn't fit, if anything doesn't.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from django.db import models
from psycopg import sql

from iki_server.entities import registry
from iki_spec import naming
from iki_spec.model import Field

__all__ = ["TRIGGER_PREFIX", "Mismatch", "Table", "checked", "install", "tables"]

#: The triggers' names start with this; ``sync_plugins`` drops those no table needs.
TRIGGER_PREFIX = "iki_reference_types__"
_EVENTS = {"ins": "INSERT", "upd": "UPDATE"}


class Mismatch(Exception):  # noqa: N818 - reads as "the references are a mismatch"
    """Values a table holds already that its references' types don't allow."""


@dataclass(frozen=True)
class Table:
    """A table with references to check: (column, the types it may name, as text[])."""

    name: str
    columns: tuple[tuple[str, str], ...]

    @property
    def args(self) -> tuple[str, ...]:
        return tuple(x for pair in self.columns for x in pair)


def allowed(f: Field, *, entry: bool = False) -> list[str] | None:
    """The types a reference may name, if the database must be told: None when its foreign
    key holds it to its type already, or it may name any entity. One of a registry entry's
    (``entry``) to any entity names any entry (ADR 0161): an entity of a type with an
    identity."""
    from iki_server.entities.values import referable

    if entry and f.to and naming.ENTITY in f.to:
        return sorted(t for t in registry.types() if registry.has_entries(t))
    if not f.to or naming.ENTITY in f.to:
        return None
    if len(f.to) == 1:
        target = registry.types().get(f.to[0])
        if target is None or not target.abstract:
            return None
    return sorted(referable(f))


def checked(f: Field) -> bool:
    """Whether a reference's types are checked by a trigger rather than its foreign key."""
    return allowed(f) is not None


def _literal(types: list[str]) -> str:
    """A text[] literal: type names have no quotes, commas or braces in them."""
    return "{" + ",".join(types) + "}"


def tables(
    parts: Iterable[tuple[str, type[models.Model], Mapping[str, Field]]],
) -> list[Table]:
    """The tables of these (plugin, model, fields) whose references need their types
    checked: the model's own, and those of its lists of references."""
    from iki_server.entities.values import is_list_table, list_model

    out: dict[str, list[tuple[str, str]]] = {}
    for plugin, model, fields in parts:
        entry = bool(getattr(model, "IKI_ENTRY", False))
        for f in fields.values():
            if f.removed:
                continue
            if f.type == "ref" and (types := allowed(f, entry=entry)) is not None:
                column = str(model._meta.get_field(f.name).column)  # type: ignore[union-attr]
                out.setdefault(model._meta.db_table, []).append((column, _literal(types)))
            elif is_list_table(f) and f.items is not None and f.items.type == "ref":
                types = allowed(f.items, entry=entry)
                if types is not None:
                    table = list_model(plugin, model, f.name)._meta.db_table
                    out.setdefault(table, []).append(("item_id", _literal(types)))
            elif f.is_rows and f.items is not None:
                # A list of rows' references (ADR 0137), each in its own column.
                table = list_model(plugin, model, f.name)._meta.db_table
                for name, cell in f.items.fields.items():
                    if cell.type == "ref" and (types := allowed(cell, entry=entry)) is not None:
                        out.setdefault(table, []).append((f"{name}_id", _literal(types)))
    return [Table(name, tuple(columns)) for name, columns in sorted(out.items())]


def _current(cursor: Any) -> dict[str, tuple[str, ...]]:  # noqa: ANN401 - Django's cursor
    """Each table with these triggers, and its insert trigger's arguments."""
    cursor.execute(
        "SELECT c.relname, t.tgname, t.tgargs FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal AND t.tgname LIKE %s",
        [TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    found: dict[str, tuple[str, ...]] = {}
    names: dict[str, set[str]] = {}
    for table, name, raw in cursor.fetchall():
        names.setdefault(table, set()).add(name)
        if name == TRIGGER_PREFIX + "ins":
            args = bytes(raw).split(b"\x00")[:-1] if raw else []
            found[table] = tuple(a.decode() for a in args)
    complete = {TRIGGER_PREFIX + n for n in _EVENTS}
    return {t: found.get(t, ()) if n == complete else ("-",) for t, n in names.items()}


def install(cursor: Any, wanted: list[Table]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """The triggers each of ``wanted`` needs, checking what it holds where they're new or
    changed; drops the rest. Raises :class:`Mismatch` naming values that don't fit."""
    current = _current(cursor)
    needed = {t.name: t for t in wanted}
    for table in sorted(set(current) - set(needed)):
        _drop(cursor, table)
    for t in wanted:
        if current.get(t.name) == t.args:
            continue
        _check(cursor, t)
        _drop(cursor, t.name)
        for suffix, event in _EVENTS.items():
            cursor.execute(
                sql.SQL(
                    "CREATE TRIGGER {name} AFTER {event} ON {table} REFERENCING NEW TABLE AS "
                    "added FOR EACH STATEMENT EXECUTE FUNCTION iki_reference_types({args})"
                ).format(
                    name=sql.Identifier(TRIGGER_PREFIX + suffix),
                    event=sql.SQL(event),
                    table=sql.Identifier(t.name),
                    args=sql.SQL(", ").join(sql.Literal(a) for a in t.args),
                )
            )
    return sorted(needed)


def _check(cursor: Any, t: Table) -> None:  # noqa: ANN401
    for column, types in t.columns:
        cursor.execute(
            sql.SQL(
                "SELECT r.hrid, e.hrid, e.type FROM {table} t "
                "JOIN iki_entity e ON e.id = t.{column} JOIN iki_entity r ON r.id = t.entity_id "
                "WHERE NOT (e.type = ANY (%s::text[])) ORDER BY r.hrid LIMIT 5"
            ).format(table=sql.Identifier(t.name), column=sql.Identifier(column)),
            [types],
        )
        wrong = cursor.fetchall()
        if wrong:
            named = ", ".join(f"{r} names {e} (a {kind})" for r, e, kind in wrong)
            raise Mismatch(
                f"{t.name}.{column} can refer only to {types.strip('{}').replace(',', ', ')}, "
                f"and {named}: change those values, or let the reference name their types."
            )


def _drop(cursor: Any, table: str) -> None:  # noqa: ANN401
    for suffix in _EVENTS:
        cursor.execute(
            sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                sql.Identifier(TRIGGER_PREFIX + suffix), sql.Identifier(table)
            )
        )

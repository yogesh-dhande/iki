"""References' edges (ADR 0123): the triggers ``sync_plugins`` puts on plugins' tables.

Every table holding a type's or an extension's values, of records or of registry entries
(ADR 0115), gets three statement triggers if it has ref fields, and so does each table of
a list of references and of a list of rows with references (ADR 0137): they call
``iki_graph_refs``, ``iki_graph_items`` or ``iki_graph_rows`` (made by ``iki_graph``'s
migrations), which keep ``iki_graph_edge`` in step with the columns. A
table whose trigger is new, or names other columns now, is filled in: edges it lacks are
added and ones it no longer holds taken away. Edges of fields removed, or of plugins
purged, go.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from django.db import models
from psycopg import sql

from iki_spec.model import Field

__all__ = ["TRIGGER_PREFIX", "Table", "install", "tables"]

#: The triggers' names start with this; ``sync_plugins`` drops those no table needs.
TRIGGER_PREFIX = "iki_graph__"
_EVENTS = {"ins": "INSERT", "upd": "UPDATE", "del": "DELETE"}


@dataclass(frozen=True)
class Table:
    """A table whose references make edges: a type's (``columns``: each column and the field
    it holds), a list's (``item``: the list's field), or a list of rows' (``columns``, each
    row's reference and its field as ``components.part``, with ``rows``)."""

    name: str
    columns: tuple[tuple[str, str], ...] = ()
    item: str = ""
    rows: bool = False

    @property
    def args(self) -> tuple[str, ...]:
        return (self.item,) if self.item else tuple(x for pair in self.columns for x in pair)

    @property
    def sources(self) -> list[str]:
        return [self.name] if self.item else [f"{self.name}.{c}" for c, _ in self.columns]

    @property
    def function(self) -> str:
        return (
            "iki_graph_items" if self.item else "iki_graph_rows" if self.rows else "iki_graph_refs"
        )


def tables(
    parts: Iterable[tuple[str, type[models.Model], Mapping[str, Field]]],
) -> list[Table]:
    """The tables of these (plugin, model, fields) that hold references."""
    from iki_server.entities.values import is_list_table
    from iki_spec import naming

    out: dict[str, Table] = {}
    for plugin, model, fields in parts:
        columns = []
        for f in fields.values():
            if f.removed:
                continue
            if f.type == "ref":
                column = model._meta.get_field(f.name).column  # type: ignore[union-attr]
                columns.append((str(column), f.name))
            elif is_list_table(f) and f.items is not None and f.items.type in ("ref", "row"):
                from django.apps import apps

                items = apps.get_model(
                    naming.app_label(plugin), naming.list_model(model.__name__, f.name)
                )
                table = items._meta.db_table
                if f.items.type == "ref":
                    out[table] = Table(table, item=f.name)
                    continue
                cells = tuple(
                    (f"{n}_id", f"{f.name}.{n}")
                    for n, cell in f.items.fields.items()
                    if cell.type == "ref"
                )
                if cells:
                    out[table] = Table(table, columns=cells, rows=True)
        if columns:
            table = model._meta.db_table
            out[table] = Table(table, columns=tuple(columns))
    return [out[k] for k in sorted(out)]


def _current(cursor: Any) -> dict[str, tuple[str, ...]]:  # noqa: ANN401 - Django's cursor
    """Each table with the graph's triggers, and the arguments its insert trigger has."""
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
    # A table missing one of its three triggers is installed again.
    complete = {TRIGGER_PREFIX + n for n in _EVENTS}
    return {t: found.get(t, ()) if n == complete else ("-",) for t, n in names.items()}


def install(cursor: Any, wanted: list[Table]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """The triggers each of ``wanted`` needs, filled in where they're new; drops the rest,
    with their edges. Returns the tables."""
    current = _current(cursor)
    needed = {t.name: t for t in wanted}
    for table in sorted(set(current) - set(needed)):
        _drop(cursor, table)
    cursor.execute(
        "DELETE FROM iki_graph_edge WHERE kind = 'reference' AND NOT (source = ANY(%s))",
        [[s for t in wanted for s in t.sources]],
    )
    for t in wanted:
        if current.get(t.name) == t.args:
            continue
        if t.name in current:
            _drop(cursor, t.name)
        function = t.function
        for suffix, event in _EVENTS.items():
            transition = {
                "INSERT": "NEW TABLE AS added",
                "UPDATE": "OLD TABLE AS removed NEW TABLE AS added",
                "DELETE": "OLD TABLE AS removed",
            }[event]
            cursor.execute(
                sql.SQL(
                    "CREATE TRIGGER {name} AFTER {event} ON {table} REFERENCING {transition} "
                    "FOR EACH STATEMENT EXECUTE FUNCTION {function}({args})"
                ).format(
                    name=sql.Identifier(TRIGGER_PREFIX + suffix),
                    event=sql.SQL(event),
                    table=sql.Identifier(t.name),
                    transition=sql.SQL(transition),
                    function=sql.Identifier(function),
                    args=sql.SQL(", ").join(sql.Literal(a) for a in t.args),
                )
            )
        _fill(cursor, t)
    return sorted(needed)


def _drop(cursor: Any, table: str) -> None:  # noqa: ANN401
    for suffix in _EVENTS:
        cursor.execute(
            sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                sql.Identifier(TRIGGER_PREFIX + suffix), sql.Identifier(table)
            )
        )


def _fill(cursor: Any, t: Table) -> None:  # noqa: ANN401
    """Edges written while no trigger kept them, and ones whose value has gone since."""
    table = sql.Identifier(t.name)
    if t.item:
        cursor.execute(
            sql.SQL(
                "DELETE FROM iki_graph_edge g WHERE g.kind = 'reference' AND g.source = %s "
                "AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.entity_id = g.node_id "
                "AND t.position = g.position AND t.item_id = g.on_id)"
            ).format(table=table),
            [t.name],
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO iki_graph_edge (node_id, on_id, kind, field, source, position) "
                "SELECT t.entity_id, t.item_id, 'reference', %s, %s, t.position FROM {table} t "
                "WHERE t.item_id <> t.entity_id AND NOT EXISTS (SELECT 1 FROM iki_graph_edge g "
                "WHERE g.kind = 'reference' AND g.source = %s AND g.node_id = t.entity_id "
                "AND g.position = t.position)"
            ).format(table=table),
            [t.item, t.name, t.name],
        )
        return
    if t.rows:
        _fill_rows(cursor, t)
        return
    for column, field in t.columns:
        source = f"{t.name}.{column}"
        names = {"table": table, "column": sql.Identifier(column)}
        cursor.execute(
            sql.SQL(
                "DELETE FROM iki_graph_edge g WHERE g.kind = 'reference' AND g.source = %s "
                "AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.entity_id = g.node_id "
                "AND t.{column} = g.on_id)"
            ).format(**names),
            [source],
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO iki_graph_edge (node_id, on_id, kind, field, source) "
                "SELECT t.entity_id, t.{column}, 'reference', %s, %s FROM {table} t "
                "WHERE t.{column} IS NOT NULL AND t.{column} <> t.entity_id AND NOT EXISTS ("
                "SELECT 1 FROM iki_graph_edge g WHERE g.kind = 'reference' AND g.source = %s "
                "AND g.node_id = t.entity_id)"
            ).format(**names),
            [field, source, source],
        )


def _fill_rows(cursor: Any, t: Table) -> None:  # noqa: ANN401
    """:func:`_fill` for a list of rows' table: an edge per row and reference."""
    for column, field in t.columns:
        source = f"{t.name}.{column}"
        names = {"table": sql.Identifier(t.name), "column": sql.Identifier(column)}
        cursor.execute(
            sql.SQL(
                "DELETE FROM iki_graph_edge g WHERE g.kind = 'reference' AND g.source = %s "
                "AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.entity_id = g.node_id "
                "AND t.position = g.position AND t.{column} = g.on_id)"
            ).format(**names),
            [source],
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO iki_graph_edge (node_id, on_id, kind, field, source, position) "
                "SELECT t.entity_id, t.{column}, 'reference', %s, %s, t.position FROM {table} t "
                "WHERE t.{column} IS NOT NULL AND t.{column} <> t.entity_id AND NOT EXISTS ("
                "SELECT 1 FROM iki_graph_edge g WHERE g.kind = 'reference' AND g.source = %s "
                "AND g.node_id = t.entity_id AND g.position = t.position)"
            ).format(**names),
            [field, source, source],
        )

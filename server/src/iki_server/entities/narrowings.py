"""What a subtype narrows in a field it inherits, held by the database (ADR 0283).

A field's column is in the table of the type declaring it (ADR 0007), which every
subtype's records have a row in. Its own constraints are that table's: ``NOT NULL`` for a
required field, CHECKs for an enum's values and a number's bounds. A subtype that narrows
it (``refines: { material: { required: true } }``) can't add a CHECK there, since a row
doesn't say which subtype's record it is, nor on its own table, which doesn't hold the
column. So ``sync_plugins`` puts a trigger on the declaring table that, for a row of a
record of the narrowing type or one of its subtypes, holds the column to the narrowing:
``required``, ``values``, ``min`` and ``max``, as the declaring table holds its own. A
person, a run, a bulk import and SQL are all held to it. Records already there that don't
fit stop the deploy, naming them, as rules do (ADR 0127).

Every save checks the same narrowing first, from the type's effective declaration
(ADR 0282), and says what's wrong; the trigger refuses whatever gets past that.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from psycopg import sql

from iki_server.entities import registry
from iki_server.entities.values import FOREIGN_KEYS, is_list_table
from iki_spec import naming
from iki_spec.model import Field, in_entry

__all__ = ["TRIGGER_PREFIX", "Narrowing", "Unfit", "install", "wanted"]

#: The triggers' and their functions' names start with this; ``sync_plugins`` drops the
#: ones no narrowing needs.
TRIGGER_PREFIX = "iki_narrowed__"
#: How many records that don't fit a narrowing a stopped deploy names.
SHOWN = 5


class Unfit(Exception):  # noqa: N818 - reads as "the records are unfit"
    """Records already there don't fit a narrowing the database is to hold."""


@dataclass(frozen=True)
class Narrowing:
    """One type's narrowing of one inherited field, where the database holds it."""

    #: The narrowing type, and it with every subtype: the records it holds.
    type_name: str
    types: tuple[str, ...]
    #: The declaring type's table holding the column, and the column.
    table: str
    column: str
    field: str
    #: What it holds: ``required``, ``values``, ``min``, ``max``.
    part: str
    value: Any

    def condition(self, row: str) -> sql.Composable:
        """True of a row of ``row`` (``NEW``, or a table alias) that fits."""
        column = sql.SQL("{}.{}").format(sql.SQL(row), sql.Identifier(self.column))
        if self.part == "required":
            return sql.SQL("{} IS NOT NULL").format(column)
        if self.part == "values":
            values = sql.SQL("ARRAY[{}]::text[]").format(
                sql.SQL(", ").join(sql.Literal(v) for v in self.value)
            )
            if isinstance(self.value, list):
                return sql.SQL("{c} IS NULL OR {c} <@ {v}").format(c=column, v=values)
            return sql.SQL("{c} IS NULL OR {c} = ANY({v})").format(c=column, v=values)
        operator = sql.SQL(">=" if self.part == "min" else "<=")
        return sql.SQL("{c} IS NULL OR {c} {op} {v}").format(
            c=column, op=operator, v=sql.Literal(self.value)
        )

    @property
    def message(self) -> str:
        if self.part == "required":
            return f"{self.type_name} records need {self.field} (ADR 0283)."
        if self.part == "values":
            return (
                f"{self.type_name} records' {self.field} is one of "
                f"{', '.join(map(str, self.value))} (ADR 0283)."
            )
        bound = "at least" if self.part == "min" else "at most"
        return f"{self.type_name} records' {self.field} is {bound} {self.value} (ADR 0283)."


def _column(f: Field) -> str:
    return f"{f.name}_id" if f.type in FOREIGN_KEYS else f.name


def _parts(narrowed: Field, declared: Field) -> list[tuple[str, Any]]:
    """What ``narrowed`` holds that ``declared``, the declaring type's, doesn't."""
    out: list[tuple[str, Any]] = []
    if narrowed.required and not declared.required:
        out.append(("required", True))
    item, base = narrowed, declared
    listed = narrowed.type == "list" and narrowed.items is not None and declared.items is not None
    if listed:
        assert narrowed.items is not None and declared.items is not None  # noqa: S101
        item, base = narrowed.items, declared.items
    if item.type == "enum" and item.values != base.values:
        out.append(("values", list(item.values) if listed else tuple(item.values)))
    if not listed:
        if narrowed.min is not None and narrowed.min != declared.min:
            out.append(("min", narrowed.min))
        if narrowed.max is not None and narrowed.max != declared.max:
            out.append(("max", narrowed.max))
    return out


def wanted() -> tuple[Narrowing, ...]:
    """Every narrowing of a column the database is to hold, of every installed type."""
    out: list[Narrowing] = []
    types = registry.types()
    for name in sorted(types):
        if name == naming.ENTITY:
            continue
        declared = registry.effective(name)
        for field_name, f in declared.fields.items():
            if f.removed or declared.sources.get(f"refines.{field_name}") != name:
                continue
            if is_list_table(f):
                continue  # held in every save: it has no column
            owner = types[declared.declared_by(field_name)]
            base = registry.effective(owner.qualified_name).fields[field_name]
            table = owner.table
            if in_entry(base, owner.identity) or base.registry is not None:
                table = naming.entry_table(table)
            subtypes = tuple(sorted(registry.subtypes(name)))
            out.extend(
                Narrowing(name, subtypes, table, _column(f), field_name, part, value)
                for part, value in _parts(f, base)
            )
    return tuple(out)


def _function(table: str) -> str:
    return (TRIGGER_PREFIX + table)[:63]


def install(cursor: Any, narrowings: Iterable[Narrowing]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """A trigger on each table holding a narrowed column, and none on tables that no longer
    need one. Raises :class:`Unfit` when records already don't fit: the deploy stops,
    saying which."""
    by_table: dict[str, list[Narrowing]] = defaultdict(list)
    for n in narrowings:
        by_table[n.table].append(n)
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND t.tgname LIKE %s",
        [TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    for table, name in cursor.fetchall():
        if table not in by_table:
            cursor.execute(
                sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
            )
            cursor.execute(sql.SQL("DROP FUNCTION IF EXISTS {}()").format(sql.Identifier(name)))
    unfit = [found for table in sorted(by_table) for found in _unfit(cursor, by_table[table])]
    if unfit:
        raise Unfit(
            "Some records already don't fit what their type narrows a field it inherits to "
            "(ADR 0283): " + "; ".join(unfit) + ". Correct them, or take the narrowing out, "
            "then deploy again."
        )
    for table, found in sorted(by_table.items()):
        checks = sql.SQL("\n").join(
            sql.SQL(
                "  IF kind = ANY({types}) AND NOT ({condition}) THEN\n"
                "    RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = {message},"
                " COLUMN = {column}, TABLE = {table};\n"
                "  END IF;"
            ).format(
                types=sql.SQL("ARRAY[{}]::text[]").format(
                    sql.SQL(", ").join(sql.Literal(t) for t in n.types)
                ),
                condition=n.condition("NEW"),
                message=sql.Literal(n.message),
                column=sql.Literal(n.column),
                table=sql.Literal(table),
            )
            for n in found
        )
        name = sql.Identifier(_function(table))
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $body$\n"
                "DECLARE kind text;\n"
                "BEGIN\n"
                "  SELECT type INTO kind FROM iki_entity WHERE id = NEW.entity_id;\n"
                "{checks}\n"
                "  RETURN NULL;\n"
                "END $body$"
            ).format(name=name, checks=checks)
        )
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE TRIGGER {name} AFTER INSERT OR UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION {name}()"
            ).format(name=name, table=sql.Identifier(table))
        )
    return sorted(
        f"{n.table}.{n.column}:{n.type_name}:{n.part}" for t in by_table.values() for n in t
    )


def _unfit(cursor: Any, narrowings: list[Narrowing]) -> list[str]:  # noqa: ANN401 - Django's cursor
    out = []
    for n in narrowings:
        cursor.execute(
            sql.SQL(
                "SELECT e.hrid FROM {table} AS x JOIN iki_entity e ON e.id = x.entity_id "
                "WHERE e.type = ANY(%s) AND NOT ({condition}) ORDER BY e.hrid LIMIT {shown}"
            ).format(
                table=sql.Identifier(n.table),
                condition=n.condition("x"),
                shown=sql.Literal(SHOWN),
            ),
            [list(n.types)],
        )
        hrids = [row[0] for row in cursor.fetchall()]
        if hrids:
            out.append(f"{', '.join(hrids)} ({n.message.removesuffix(' (ADR 0283).')})")
    return out

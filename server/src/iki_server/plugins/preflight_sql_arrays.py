"""Exact per-record checks on primitive lists stored in PostgreSQL array columns."""

from __future__ import annotations

from collections.abc import Iterable

from django.core.management.base import CommandError
from psycopg import sql

from iki_server.entities.values import is_list_table
from iki_server.plugins.makemigrations import linear_conversion
from iki_server.plugins.preflight_projection import fill_default, project, sql_type, typed_default
from iki_server.plugins.preflight_sql import Scope
from iki_server.plugins.preflight_sql_fields import Check, invalid_value
from iki_spec.model import Field
from iki_spec.units import UnitError

ITEM = sql.SQL("item")


def _default_array(item: Field, values: list[object]) -> sql.Composable:
    literals = (typed_default(item, value) for value in values)
    return sql.SQL("ARRAY[{}]::{}[]").format(sql.SQL(", ").join(literals), sql_type(item))


def _item_rows(
    raw: sql.Composable, previous: Field | None, item: Field, default: list[object] | None
) -> sql.Composable:
    """Project each stored item, or the backfilled items when the owning cell is null."""
    rows: list[sql.Composable] = []
    if previous:
        value = project(item.name, {item.name: previous}, {item.name: item}, lambda _: ITEM)
        rows.append(sql.SQL("SELECT {} AS value FROM unnest({}) AS i(item)").format(value, raw))
    if default:
        # AlterField fills null cells before ConvertUnit converts all stored quantities.
        value = ITEM
        if previous and item.type == previous.type == "quantity" and previous.unit != item.unit:
            value = project(item.name, {item.name: previous}, {item.name: item}, lambda _: ITEM)
        rows.append(
            sql.SQL("SELECT {} AS value FROM unnest({}) AS i(item) WHERE {} IS NULL").format(
                value, _default_array(item, default), raw
            )
        )
    return sql.SQL(" UNION ALL ").join(rows)


def _any_item(rows: sql.Composable, predicate: sql.Composable) -> sql.Composable:
    # An owner counts once even if every element violates the new requirement.
    return sql.SQL("EXISTS (SELECT 1 FROM ({}) AS items WHERE {})").format(rows, predicate)


def _checks(scope: Scope, name: str, old: Field | None, new: Field) -> Iterable[Check]:
    item = new.items
    assert item is not None  # noqa: S101 - validated list
    previous = old.items if old else None
    raw = scope.column(name)
    default = fill_default(old, new)
    if default is not None and not isinstance(default, list):
        raise ValueError(f"Invalid list default for {name}.")
    if not previous and old:
        yield Check(
            "value",
            name,
            "Stored values must be lists.",
            True,
            sql.SQL("{} IS NOT NULL").format(raw),
        )
    if not previous and default is None:
        return
    size = sql.SQL("cardinality({})").format(raw) if previous else sql.SQL("NULL::integer")
    if default is not None:
        size = sql.SQL("(CASE WHEN {} IS NULL THEN {} ELSE {} END)").format(
            raw, sql.Literal(len(default)), size
        )
    bounds: list[sql.Composable] = []
    if new.min_items is not None:
        bounds.append(sql.SQL("{} < {}").format(size, sql.Literal(new.min_items)))
    if new.max_items is not None:
        bounds.append(sql.SQL("{} > {}").format(size, sql.Literal(new.max_items)))
    if bounds:
        yield Check(
            "value",
            name,
            "List lengths fall outside the proposed bounds.",
            True,
            sql.SQL(" OR ").join(bounds),
        )
    if not default and (previous is None or previous == item):
        return

    if previous and item.type == previous.type == "quantity" and previous.unit != item.unit:
        affected = sql.SQL("{} > 0").format(size)
        try:
            scale, offset = linear_conversion(previous.unit or "", item.unit or "", name)
        except (CommandError, UnitError, OverflowError) as error:
            yield Check("unit", name, str(error), True, affected, True)
            return
        yield Check(
            "unit",
            name,
            f"{previous.unit} to {item.unit}: new = old × {scale!r} {offset:+}.",
            False,
            affected,
            True,
        )

    rows = _item_rows(raw, previous, item, default)
    if item.pattern and (previous is None or previous.pattern != item.pattern or default):
        pattern = sql.SQL("value !~ {}").format(sql.Literal(f"^(?:{item.pattern})$"))
        yield Check(
            "pattern", name, "List items fail the proposed pattern.", True, _any_item(rows, pattern)
        )
    invalid = invalid_value(item, sql.SQL("value"), include_pattern=False)
    # The scalar projection returns typed null for incompatible types or overflow.
    failure: sql.Composable = sql.SQL("value IS NULL")
    if invalid is not None:
        failure = sql.SQL("value IS NULL OR ({})").format(invalid)
    kind = "enum" if item.type == "enum" else "value"
    message = (
        f"List items must be one of {', '.join(item.values)}."
        if item.type == "enum"
        else "List items fail the proposed requirements."
    )
    yield Check(kind, name, message, True, _any_item(rows, failure))


def array_checks(scope: Scope, old: dict[str, Field], new: dict[str, Field]) -> Iterable[Check]:
    for name in sorted(new):
        field, before = new[name], old.get(name)
        if field == before or field.removed or field.type != "list" or is_list_table(field):
            continue
        if before and (before.type == "state" or is_list_table(before)):
            continue
        yield from _checks(scope, name, before, field)

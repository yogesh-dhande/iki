"""Scalar SQL values after generated field migrations, used for checks and grouping."""

from __future__ import annotations

import datetime as dt
import math
import struct
import sys
from collections.abc import Callable, Mapping
from typing import Any

from django.core.management.base import CommandError
from psycopg import sql

from iki_server.plugins.makemigrations import linear_conversion
from iki_spec.model import Field, stored_in_list_table
from iki_spec.units import UnitError

# The physical kinds shared by generated history columns. A whole number can also
# widen to float8 (PluginAutodetector._check_history_type, ADR 0033).
_KINDS = {
    "string": "text",
    "text": "text",
    "enum": "text",
    "state": "text",
    "int": "bigint",
    "float": "float8",
    "quantity": "float8",
    "bool": "boolean",
    "date": "date",
    "datetime": "timestamptz",
    "ref": "uuid",
    "file": "uuid",
    "person": "uuid",
    "json": "jsonb",
}


def sql_type(field: Field) -> sql.SQL:
    """PostgreSQL scalar storage for a field or an array item."""
    return sql.SQL(_KINDS[field.type])


def fill_default(old: Field | None, new: Field) -> Any:  # noqa: ANN401 - a manifest value
    """The value used for a missing cell by AddField or a nullable-to-required change."""
    if old is not None and (old.required or old.type == "state" or not new.required):
        return None
    if new.type == "state":
        return new.initial
    return new.default if new.has_default else None


def typed_default(field: Field, value: object) -> sql.Composable:
    """A scalar or primitive array item's literal in its target column type."""
    # Compiled uploads carry JSON-shaped defaults. Reject malformed ones before a
    # SQL literal is adapted or cast, so an invalid candidate yields a client error.
    kind = _KINDS[field.type]
    valid = False
    if kind == "text":
        valid = isinstance(value, str) and "\x00" not in value
    elif kind == "bigint":
        valid = type(value) is int and -(2**63) <= value < 2**63
    elif kind == "boolean":
        valid = isinstance(value, bool)
    elif kind == "float8":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and -sys.float_info.max <= value <= sys.float_info.max
        )
    elif kind in ("date", "timestamptz") and isinstance(value, str):
        try:
            if kind == "date":
                value = dt.date.fromisoformat(value)
                valid = True
            else:
                value = dt.datetime.fromisoformat(value)
                valid = value.tzinfo is not None
        except ValueError:
            pass
    if not valid:
        raise ValueError(f"Invalid default for {field.name} ({field.type}).")
    return sql.SQL("({})::{}").format(sql.Literal(value), sql_type(field))


def _last_magnitude(test: Callable[[float], bool]) -> float:
    """Largest binary64 magnitude satisfying a monotone bound, including zero.

    IEEE positive encodings are in increasing numeric order. Searching those bits
    avoids rounding the boundary (max_float / scale can itself round up). Costs at
    most 63 arithmetic operations per bound, independent of the number of records.
    """
    low = 0
    high = int.from_bytes(struct.pack(">d", sys.float_info.max), "big")
    while low < high:
        mid = (low + high + 1) // 2
        value = struct.unpack(">d", mid.to_bytes(8, "big"))[0]
        if test(value):
            low = mid
        else:
            high = mid - 1
    return float(struct.unpack(">d", low.to_bytes(8, "big"))[0])


def _converted(value: sql.Composable, scale: float, offset: float) -> sql.Composable:
    """Use ConvertUnit's float8 arithmetic only for operands PostgreSQL can evaluate."""
    result = sql.SQL("({} * {}::float8 + {}::float8)").format(
        value, sql.Literal(scale), sql.Literal(offset)
    )
    valid: list[sql.Composable] = []
    for sign, comparison in ((1, sql.SQL("<=")), (-1, sql.SQL(">="))):
        if not math.isfinite(sign * sys.float_info.max * scale + offset):
            magnitude = _last_magnitude(
                lambda v: math.isfinite(sign * v * scale + offset)  # noqa: B023 - evaluated now
            )
            valid.append(
                sql.SQL("{} {} {}::float8").format(value, comparison, sql.Literal(sign * magnitude))
            )

    # PostgreSQL also raises when multiplying two nonzero float8s rounds to zero.
    if scale and math.ulp(0.0) * scale == 0.0:
        tiny = _last_magnitude(lambda v: v * scale == 0)
        valid.append(
            sql.SQL("({} = 0 OR abs({}) > {}::float8)").format(value, value, sql.Literal(tiny))
        )
    if valid:
        return sql.SQL("(CASE WHEN {} THEN {} END)").format(sql.SQL(" AND ").join(valid), result)
    return result


def _move_states(
    value: sql.Composable, source: sql.Composable, moves: Mapping[str, str]
) -> sql.Composable:
    if not moves:
        return value
    cases = sql.SQL(" ").join(
        sql.SQL("WHEN {} THEN {}").format(sql.Literal(old), sql.Literal(new))
        for old, new in sorted(moves.items())
    )
    return sql.SQL("(CASE {} {} ELSE {} END)").format(source, cases, value)


def project(
    name: str,
    old: Mapping[str, Field],
    new: Mapping[str, Field],
    column: Callable[[str], sql.Composable],
) -> sql.Composable:
    """The candidate scalar, or typed NULL when the value cannot migrate.

    Only deployed field names go to `column` for physical reads. Retired state
    declarations in `new` describe moves into a different live state's column.
    """
    field = new[name]
    previous = old.get(name)
    kind = _KINDS[field.type]
    target = sql_type(field)
    missing = sql.SQL("NULL::{}").format(target)
    value: sql.Composable = missing
    raw: sql.Composable = missing
    if previous is not None:
        # Normalized lists are read as [] when empty. Their value is present
        # on every record but has no scalar column or scalar conversion.
        raw = sql.SQL("TRUE") if stored_in_list_table(previous) else column(name)
        source = _KINDS.get(previous.type)
        if source == kind and (previous.type == "state") == (field.type == "state"):
            value = raw
        elif source == "bigint" and kind == "float8":
            value = sql.SQL("({})::float8").format(raw)

    default = fill_default(previous, field)
    if default is not None:
        filled = typed_default(field, default)
        value = (
            sql.SQL("(CASE WHEN {} IS NULL THEN {} ELSE {} END)").format(raw, filled, value)
            if previous is not None
            else filled
        )

    if previous and field.type == previous.type == "quantity" and previous.unit != field.unit:
        assert previous.unit and field.unit  # noqa: S101 - quantities have validated units
        try:
            scale, offset = linear_conversion(previous.unit, field.unit, name)
        except (CommandError, UnitError, OverflowError):
            return missing
        return _converted(value, scale, offset)

    if field.type == "state":
        live = set(field.live_states)
        moves = {
            n: s.moved_to
            for n, s in field.states.items()
            if s.removed and s.moved_to in live and s.moved_to
        }
        value = _move_states(value, value, moves)
        for retired, declaration in sorted(new.items()):
            move = declaration.moved_to
            if declaration.removed and move and move.field == name and retired in old:
                moves = {s: target for s, target in move.states if target in live}
                value = _move_states(value, column(retired), moves)
    return value

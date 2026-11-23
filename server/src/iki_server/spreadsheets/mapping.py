"""Mapping a sheet's columns to a type's fields (ADR 0178): which fields a column can set,
the mapping Iki suggests from the headers, checking one, and saved mappings, matched to a
sheet by its headers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from iki_server.entities import registry, requests
from iki_server.entities.values import shape
from iki_server.spreadsheets import cells
from iki_server.spreadsheets.cells import Column
from iki_spec import units
from iki_spec.model import Field

__all__ = [
    "Target",
    "check",
    "columns_from_json",
    "from_saved",
    "headers_of",
    "key",
    "matches",
    "saved_columns",
    "suggest",
    "targets",
]

_OPTIONS = (
    "index",
    "field",
    "unit",
    "separator",
    "dates",
    "time_zone",
    "decimal",
    "serial_dates",
    "values",
)


@dataclass(frozen=True)
class Target:
    """A field a column can set, or can't and why."""

    field: Field
    why_not: str | None

    @property
    def name(self) -> str:
        return self.field.name


def targets(type_name: str) -> list[Target]:
    """The type's fields, in its order, each with why a column can't set it, if it can't."""
    form = shape(type_name)
    out = []
    for name, f in form.fields().items():
        service = requests.is_service_field(type_name, name)
        out.append(Target(f, cells.not_mappable(f, service)))
    return out


def key(text: str) -> str:
    """A header or a field's name as they're compared: ``Conc. (mg/mL)`` → ``conc``."""
    without_unit = re.sub(r"[(\[][^()\[\]]*[)\]]\s*$", "", text)
    return re.sub(r"[^a-z0-9]+", "", without_unit.casefold())


def headers_of(sample: Sequence[Sequence[str]], header_row: int, width: int) -> list[str]:
    """The sheet's headers, one a column: what its header row says, or the column's letter
    (``Column C``) where it says nothing or it has none."""
    row = list(sample[header_row - 1]) if 0 < header_row <= len(sample) else []
    out = []
    for index in range(width):
        text = row[index].strip() if index < len(row) else ""
        out.append(text or f"Column {letter(index)}")
    return out


def letter(index: int) -> str:
    """A column's letters, as a spreadsheet names it: 0 → ``A``, 27 → ``AB``."""
    out = ""
    n = index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        out = chr(65 + rest) + out
    return out


def suggest(type_name: str, headers: Sequence[str]) -> list[Column]:
    """A column for each header: the field whose name or label it is, without its unit or
    punctuation, each field once; with the unit the header names, for a quantity."""
    wanted: dict[str, Target] = {}
    for target in targets(type_name):
        if target.why_not is not None:
            continue
        for name in (target.field.name, target.field.label or ""):
            if name:
                wanted.setdefault(key(name), target)
    taken: set[str] = set()
    out = []
    for index, header in enumerate(headers):
        found = wanted.get(key(header)) or _abbreviated(wanted, key(header))
        if found is None or found.name in taken:
            out.append(Column(index))
            continue
        taken.add(found.name)
        out.append(Column(index, found.name, unit=_header_unit(found.field, header)))
    return out


def _abbreviated(wanted: Mapping[str, Target], header: str) -> Target | None:
    """The one field a header abbreviates, as ``Conc.`` does ``Concentration``: at least
    four letters its name or label starts with, and no other's."""
    if len(header) < 4:
        return None
    found = {t.name: t for k, t in wanted.items() if k.startswith(header)}
    return next(iter(found.values())) if len(found) == 1 else None


def _header_unit(f: Field, header: str) -> str | None:
    if f.type != "quantity":
        return None
    named = cells.unit_in(header)
    if named is None or named == f.unit or cells.unit_problem(f, named) is not None:
        return None
    return named


def columns_from_json(data: Iterable[Mapping[str, Any]]) -> list[Column]:
    """Columns as an import keeps them."""
    return [
        Column(
            index=int(c["index"]),
            field=c.get("field"),
            unit=c.get("unit"),
            separator=c.get("separator", cells.SEPARATOR),
            dates=c.get("dates"),
            time_zone=c.get("time_zone"),
            decimal=c.get("decimal"),
            serial_dates=c.get("serial_dates"),
            values=dict(c.get("values") or {}),
        )
        for c in data
    ]


def check(type_name: str, given: Sequence[Mapping[str, Any]], width: int) -> dict[str, list[str]]:
    """What's wrong with columns as given, by where (``columns/2/unit``): each column once,
    in the sheet; each field once, one a column can set; options that fit it."""
    errors: dict[str, list[str]] = {}
    fields = {t.name: t for t in targets(type_name)}
    seen_columns: set[int] = set()
    seen_fields: dict[str, int] = {}
    for at, column in enumerate(given):
        where = f"columns/{at}"
        extra = sorted(set(column) - set(_OPTIONS))
        if extra:
            errors[f"{where}/{extra[0]}"] = [f"isn't an option: give {', '.join(_OPTIONS)}."]
            continue
        index = column.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < width:
            errors[f"{where}/index"] = [f"must be a column of the sheet, from 0 to {width - 1}."]
            continue
        if index in seen_columns:
            errors[f"{where}/index"] = [f"Column {letter(index)} is mapped twice."]
            continue
        seen_columns.add(index)
        name = column.get("field")
        if name is None:
            continue
        target = fields.get(name) if isinstance(name, str) else None
        if target is None:
            errors[f"{where}/field"] = [f"{type_name} has no field `{name}`."]
            continue
        if target.why_not is not None:
            errors[f"{where}/field"] = [target.why_not]
            continue
        if name in seen_fields:
            errors[f"{where}/field"] = [
                f"Column {letter(seen_fields[name])} sets {target.field.label or name} already."
            ]
            continue
        seen_fields[name] = index
        _check_options(target.field, column, where, errors)
    return errors


def _check_options(
    f: Field, column: Mapping[str, Any], where: str, errors: dict[str, list[str]]
) -> None:
    item = f.items if f.type == "list" and f.items is not None else f
    unit = column.get("unit")
    if unit is not None:
        if item.type != "quantity":
            errors[f"{where}/unit"] = ["Only a quantity's cells have a unit."]
        elif not isinstance(unit, str) or not unit.strip():
            errors[f"{where}/unit"] = ["must be a UCUM unit, such as mg/mL."]
        else:
            problem = cells.unit_problem(item, unit)
            if problem is not None:
                errors[f"{where}/unit"] = [f"Its cells can't be {item.unit}: {problem}"]
    separator = column.get("separator")
    if separator is not None and (
        f.type != "list" or not isinstance(separator, str) or len(separator) != 1
    ):
        errors[f"{where}/separator"] = ["A list's items are separated by one character."]
    dates = column.get("dates")
    if dates is not None and (
        item.type not in ("date", "datetime") or dates not in cells.DATE_ORDERS
    ):
        errors[f"{where}/dates"] = [f"must be one of {', '.join(cells.DATE_ORDERS)}, for dates."]
    serial = column.get("serial_dates")
    if serial is not None and (
        item.type not in ("date", "datetime") or not isinstance(serial, bool)
    ):
        errors[f"{where}/serial_dates"] = ["must be true or false, for dates."]
    zone = column.get("time_zone")
    if zone is not None and (
        item.type != "datetime" or not isinstance(zone, str) or not cells.is_time_zone(zone)
    ):
        errors[f"{where}/time_zone"] = ["must be a time zone, such as Europe/London."]
    decimal = column.get("decimal")
    if decimal is not None and (
        item.type not in ("int", "float", "quantity") or decimal not in cells.DECIMALS
    ):
        errors[f"{where}/decimal"] = ["must be point or comma, for numbers."]
    values = column.get("values")
    if values is not None:
        allowed = (
            list(item.values)
            if item.type == "enum"
            else ["true", "false"]
            if item.type == "bool"
            else []
        )
        if not allowed or not isinstance(values, dict) or len(values) > 200:
            errors[f"{where}/values"] = [
                "Only an enumeration's or a truth value's cells have choices."
            ]
        else:
            wrong = [
                str(v)
                for k, v in values.items()
                if not isinstance(k, str) or not k.strip() or len(k) > 200 or v not in allowed
            ]
            if wrong:
                errors[f"{where}/values"] = [
                    f"“{wrong[0]}” isn't one of its choices: {', '.join(allowed)}."
                ]


def unit_choices(f: Field) -> tuple[str, ...]:
    """The units a quantity's column may be in that a form offers: its own, then those of
    its kind."""
    assert f.unit is not None  # noqa: S101 - quantities have units
    return units.common_alternatives(f.unit)


# --- Saved mappings ---------------------------------------------------------------------------


def saved_columns(
    columns: Sequence[Column], headers: Sequence[str], *, keep_index: bool = False
) -> list[dict[str, Any]]:
    """An import's mapped columns by header for reuse. For a particular committed sheet,
    also keep the column index so identical headers can be distinguished."""
    out = []
    for column in columns:
        if column.field is None or column.index >= len(headers):
            continue
        kept = column.as_json()
        if not keep_index:
            del kept["index"]
        out.append({"header": headers[column.index], **kept})
    return out


def from_saved(saved: Sequence[Mapping[str, Any]], headers: Sequence[str]) -> list[Column]:
    """A saved mapping's columns for a sheet with these headers: each by its header,
    compared as :func:`key` compares them; the sheet's other columns are left unmapped."""
    by_key: dict[str, list[int]] = {}
    for index, header in enumerate(headers):
        by_key.setdefault(key(header), []).append(index)
    placed: dict[int, Column] = {}
    for kept in saved:
        free = [i for i in by_key.get(key(str(kept.get("header", ""))), []) if i not in placed]
        if not free:
            continue
        options = {k: v for k, v in kept.items() if k != "header"}
        placed[free[0]] = columns_from_json([{"index": free[0], **options}])[0]
    return [placed.get(i, Column(i)) for i in range(len(headers))]


def matches(saved_headers: Sequence[str], headers: Sequence[str]) -> float:
    """How well a saved mapping's headers match a sheet's: the share of its headers the
    sheet has, 1 for all of them."""
    if not saved_headers:
        return 0.0
    have = {key(h) for h in headers}
    return sum(1 for h in saved_headers if key(h) in have) / len(saved_headers)


def is_type(name: str) -> bool:
    found = registry.types().get(name)
    return found is not None and not found.abstract and name != "iki/core.Entity"

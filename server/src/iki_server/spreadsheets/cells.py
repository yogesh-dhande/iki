"""A sheet's cells as a type's values (ADR 0178): each column mapped to a field, how its
cells read, and what's wrong with a cell that can't be read so.

A cell becomes the JSON a create gives (``entities.prepare`` then checks it, as it
checks any create): text, a number, a truth value, a date in ISO 8601, a quantity with
its unit (``{"value": 12.5, "unit": "ug/mL"}``, which Iki converts to the field's unit),
a list, or a reference, found by the HRID or the name it gives. A problem is a
``ValueError`` saying what's wrong with the cell, for the person who'll fix it in the
workbook.

Text that reads differently by where the sheet was written is read only as the person
says it was written, never guessed (:class:`Locale`): ``1,234`` is a thousand and more
with a decimal point, and one and a bit with a decimal comma; ``03/04/2026`` is the 3rd
of April read day first, the 4th of March read month first. Until they've said, a cell
that could be either is :class:`Ambiguous`, and one that can only be one is read so
(``1,5`` has a decimal comma; ``31/12/2026`` is day first). Cells the workbook holds as
numbers or dates are never ambiguous: Excel stored them as such. A plain number in a date
column is an Excel serial date (``46023`` is 1 January 2026) only when they say so.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import re
import uuid
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iki_server.spreadsheets.workbooks import Cell, text_of
from iki_spec import units
from iki_spec.model import Field

__all__ = [
    "MAPPABLE",
    "Ambiguous",
    "Column",
    "Locale",
    "Reference",
    "convert",
    "header_unit_label",
    "items",
    "not_mappable",
    "ucum",
    "unit_in",
    "unit_problem",
]

#: The field types a column can be mapped to.
MAPPABLE = frozenset(
    {
        "string",
        "text",
        "int",
        "float",
        "quantity",
        "bool",
        "date",
        "datetime",
        "enum",
        "ref",
        "person",
        "list",
    }
)

#: How a list's items are told apart in a cell, unless the column says otherwise.
SEPARATOR = ";"

_TRUE = frozenset({"true", "yes", "y", "1", "x", "✓"})
_FALSE = frozenset({"false", "no", "n", "0"})
_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_GROUPED = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_COMMA_GROUPED = re.compile(r"[-+]?\d{1,3}(?:\.\d{3})+(?:,\d+)?")
_QUANTITY = re.compile(r"\s*(?P<number>[-+]?[\d.,]+(?:[eE][-+]?\d+)?)\s*(?P<unit>\S.*?)?\s*")
_NUMERIC_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})[\s/.-]+([A-Za-z]{3,9})\.?[\s/.,-]+(\d{4})")
_MONTH_DAY_YEAR = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")

#: How labs write units that UCUM writes otherwise, in headers and cells.
_SPELLINGS = {
    "°C": "Cel",
    "℃": "Cel",
    "degC": "Cel",
    "°F": "[degF]",
    "degF": "[degF]",
    "M": "mol/L",
    "mM": "mmol/L",
    "uM": "umol/L",
    "nM": "nmol/L",
    "pM": "pmol/L",
    "Da": "u",
    "kDa": "ku",
    "bp": "{bp}",
    "kb": "k{bp}",
    "rpm": "{rpm}",
}


class Ambiguous(ValueError):  # noqa: N818 - reads as "the cell is ambiguous"
    """A cell that reads two ways, by where the sheet was written: the person says which."""


#: Day 0 of Excel's serial dates (as Excel reckons them, from 1 March 1900).
EXCEL_EPOCH = dt.date(1899, 12, 30)
DECIMALS = ("point", "comma")
DATE_ORDERS = ("day_first", "month_first")


@dataclass(frozen=True)
class Locale:
    """How the sheet's text writes numbers and dates, as the person says it does: None
    until they've said, when cells that read two ways are :class:`Ambiguous`."""

    #: A number's decimal mark: ``point`` (1,234.5) or ``comma`` (1.234,5).
    decimal: str | None = None
    #: Dates written as numbers: ``day_first`` (31/12/2026) or ``month_first``.
    dates: str | None = None
    #: Plain numbers in date columns are Excel serial dates.
    serial_dates: bool = False

    @classmethod
    def of(cls, data: Mapping[str, Any] | None) -> Locale:
        data = data or {}
        return cls(data.get("decimal"), data.get("dates"), bool(data.get("serial_dates")))

    def as_json(self) -> dict[str, Any]:
        return {"decimal": self.decimal, "dates": self.dates, "serial_dates": self.serial_dates}


@dataclass(frozen=True)
class Column:
    """One of the sheet's columns, as it's mapped: which field its cells set (none, to
    ignore it), and how they read."""

    #: Its place in the sheet, from 0 for column A.
    index: int
    field: str | None = None
    #: A quantity's cells' unit, in UCUM, when they give none themselves: the field's,
    #: unless the column says otherwise.
    unit: str | None = None
    #: A list's items are separated so in a cell.
    separator: str = SEPARATOR
    #: How a date such as 03/04/2026 reads, ``day_first`` or ``month_first``, when it
    #: isn't as the sheet's :class:`Locale` says: None for the sheet's.
    dates: str | None = None
    #: A date and time's time zone, when its cells give none: an IANA name.
    time_zone: str | None = None
    #: A number's decimal mark, ``point`` (12.5) or ``comma`` (12,5), when it isn't as
    #: the sheet's: None for the sheet's.
    decimal: str | None = None
    #: Plain numbers are Excel serial dates, when it isn't as the sheet's says.
    serial_dates: bool | None = None
    #: What a cell says, for each choice it means: an enumeration's values (or a truth
    #: value's, as ``true`` and ``false``) the sheet spells its own way.
    values: Mapping[str, str] = dataclasses.field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"index": self.index, "field": self.field}
        for name, default in (
            ("unit", None),
            ("separator", SEPARATOR),
            ("dates", None),
            ("time_zone", None),
            ("decimal", None),
            ("serial_dates", None),
        ):
            value = getattr(self, name)
            if value != default:
                out[name] = value
        if self.values:
            out["values"] = dict(self.values)
        return out

    def under(self, locale: Locale) -> Column:
        """The column, reading as the sheet's locale says where it doesn't say otherwise."""
        return dataclasses.replace(
            self,
            decimal=self.decimal or locale.decimal,
            dates=self.dates or locale.dates,
            serial_dates=locale.serial_dates if self.serial_dates is None else self.serial_dates,
        )


@dataclass(frozen=True)
class Reference:
    """A cell that names a record, found once a chunk's are known: by its ID, its HRID or
    its name."""

    text: str


def not_mappable(f: Field, service: bool = False) -> str | None:
    """Why a column can't be mapped to ``f``, or None if it can."""
    if f.removed:
        return "It's been removed from the type."
    if f.computed:
        return "It's computed: only its plugin's actions set it."
    if f.type == "state":
        return "It's a state: records start in their first, and move on as people work."
    if service:
        return "It's the service's: set once a request is submitted."
    if f.type == "file":
        return "It holds a file: add files to records once they're imported."
    if f.type == "json" or f.is_rows:
        return "It holds structured data that a cell can't."
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        if f.items.type not in MAPPABLE or f.items.type == "list":
            return "It's a list of what a cell can't hold."
        return None
    if f.type not in MAPPABLE:
        return "A cell can't hold it."
    return None


def header_unit_label(text: str) -> str | None:
    """The bracketed input unit on a quantity column, including an unfamiliar spelling."""
    found = re.search(r"[(\[]\s*([^()\[\]]+?)\s*[)\]]\s*$", text)
    return ucum(found.group(1)) if found else None


def unit_in(text: str) -> str | None:
    """The UCUM unit a header names, in brackets, as ``Concentration (ug/mL)`` or
    ``Tm [°C]`` do, if it names one."""
    code = header_unit_label(text)
    if code is None:
        return None
    try:
        units.parse(code)
    except units.UnitError:
        return None
    return code


def unit_problem(f: Field, unit: str) -> str | None:
    """Why a quantity's cells in ``unit`` can't be ``f``'s, or None if they convert."""
    assert f.unit is not None  # noqa: S101 - quantities have units
    try:
        units.convert(1.0, ucum(unit), f.unit)
    except units.UnitError as error:
        hint = f" {error.hint}" if error.hint else ""
        return f"{error.message}{hint}"
    return None


def items(value: str, separator: str = SEPARATOR) -> list[str]:
    """A cell's list items, apart by ``separator``. An item that holds the separator, or
    starts with a double quote, is in double quotes, a quote in it doubled, as Iki's CSV
    download writes it (ADR 0175): ``"Tris; pH 8"; EDTA`` is two items. A quote elsewhere
    is part of its item, as in ``5" tube``."""
    if '"' not in value or separator == '"':
        return [part.strip() for part in value.split(separator)]
    try:
        rows = list(
            csv.reader(io.StringIO(value, newline=""), delimiter=separator, skipinitialspace=True)
        )
    except csv.Error:
        return [part.strip() for part in value.split(separator)]
    return [part.strip() for row in rows for part in row]


def convert(f: Field, column: Column, value: Cell) -> Any:  # noqa: ANN401 - JSON
    """The cell's value as a create gives it to ``f``, or None for an empty cell. A
    reference is a :class:`Reference` (a list's, a list of them), found later. Raises
    ``ValueError`` with what's wrong with it."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        if isinstance(value, str):
            parts = items(value, column.separator or SEPARATOR)
        else:
            parts = [text_of(value)]
        out = []
        for number, part in enumerate((p for p in parts if p), start=1):
            try:
                out.append(_one(f.items, column, part))
            except ValueError as error:
                raise ValueError(f"Its item {number}, “{part}”, {error}") from None
        return out
    return _one(f, column, value)


def _one(f: Field, column: Column, value: Cell) -> Any:  # noqa: ANN401 - JSON, by type
    kind = f.type
    if kind in ("string", "text"):
        return text_of(value).strip() if kind == "string" else text_of(value)
    if kind == "enum":
        return _choice(f, column, text_of(value).strip())
    if kind == "bool":
        return _truth(column, value)
    if kind == "int":
        number = _number(column, value)
        if not float(number).is_integer():
            raise ValueError("isn't a whole number.")
        return int(number)
    if kind == "float":
        return float(_number(column, value))
    if kind == "quantity":
        return _quantity(f, column, value)
    if kind == "date":
        return _date(column, value).isoformat()
    if kind == "datetime":
        return _datetime(column, value).isoformat()
    if kind == "ref":
        text = text_of(value).strip()
        return Reference(text)
    if kind == "person":
        return text_of(value).strip()
    raise ValueError("can't be read from a cell.")


def _choice(f: Field, column: Column, text: str) -> str:
    mapped = _lookup(column.values, text)
    if mapped is not None:
        return mapped
    for choice in f.values:
        if choice.casefold() == text.casefold() or _plain(choice) == _plain(text):
            return choice
    shown = ", ".join(f.values[:12]) + (", …" if len(f.values) > 12 else "")
    raise ValueError(
        f"isn't one of its choices ({shown}): say which it means in the column's choices."
    )


def _truth(column: Column, value: Cell) -> bool:
    if isinstance(value, bool):
        return value
    text = text_of(value).strip()
    mapped = _lookup(column.values, text)
    if mapped in ("true", "false"):
        return mapped == "true"
    lowered = text.casefold()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError("isn't yes or no (true, false, yes, no, y, n, 1 or 0).")


def _number(column: Column, value: Cell) -> float | int:
    if isinstance(value, bool):
        raise ValueError("is a truth value, not a number.")
    if isinstance(value, int | float):
        return value
    if not isinstance(value, str):
        raise ValueError("isn't a number.")
    return _parse_number(column, value.strip())


def _parse_number(column: Column, text: str) -> float | int:
    cleaned = text.replace("\u2212", "-").replace(" ", "").replace("\u202f", "").replace("\xa0", "")
    if column.decimal is None:
        readings = {r for r in (_as_point(cleaned), _as_comma(cleaned)) if r is not None}
        if len(readings) > 1:
            point, comma = _as_point(cleaned), _as_comma(cleaned)
            raise Ambiguous(
                f"reads as {shown(point)} with a decimal point, and {shown(comma)} with a "
                "decimal comma: say which the sheet's numbers have."
            )
        if readings:
            return readings.pop()
        raise ValueError("isn't a number.")
    number = _as_comma(cleaned) if column.decimal == "comma" else _as_point(cleaned)
    if number is None:
        other = _as_point(cleaned) if column.decimal == "comma" else _as_comma(cleaned)
        hint = (
            f" With a decimal {'point' if column.decimal == 'comma' else 'comma'} it would be "
            f"{shown(other)}: check how the sheet's numbers are written."
            if other is not None
            else ""
        )
        raise ValueError(f"isn't a number with a decimal {column.decimal}.{hint}")
    return number


def _as_point(text: str) -> float | int | None:
    """``text`` as a number with a decimal point (and commas between thousands)."""
    if _GROUPED.fullmatch(text):
        text = text.replace(",", "")
    return _plain_number(text)


def _as_comma(text: str) -> float | int | None:
    """``text`` as a number with a decimal comma (and points between thousands)."""
    if _COMMA_GROUPED.fullmatch(text):
        text = text.replace(".", "")
    elif "." in text and "," in text:
        return None
    return _plain_number(text.replace(",", "."))


def _plain_number(text: str) -> float | int | None:
    if not _NUMBER.fullmatch(text):
        return None
    return int(text) if re.fullmatch(r"[-+]?\d+", text) else float(text)


#: Between a number's thousands, as SI writes them (ISO 80000-1): a space, no-break, which
#: reads one way whichever mark a reader's decimals have. A comma or a point there would be
#: the very ambiguity these readings show.
THOUSANDS = "\xa0"


def shown(number: float | int | None) -> str:
    """A number as the import says it: a decimal point, thousands apart by a space."""
    if number is None:
        return "nothing"
    if isinstance(number, int) or number.is_integer():
        text = f"{int(number):,}"
    else:
        text = f"{number:,.10g}" if abs(number) >= 1000 else f"{number:.10g}"
    return text.replace(",", THOUSANDS)


def _quantity(f: Field, column: Column, value: Cell) -> dict[str, Any]:
    assert f.unit is not None  # noqa: S101 - quantities have units
    unit = ucum(column.unit) if column.unit else f.unit
    if isinstance(value, str):
        found = _QUANTITY.fullmatch(value)
        if found is not None and found.group("unit"):
            unit = ucum(found.group("unit"))
            number = _parse_number(column, found.group("number"))
        else:
            number = _number(column, value)
    else:
        number = _number(column, value)
    problem = unit_problem(f, unit)
    if problem is not None:
        raise ValueError(f"is in a unit Iki can't convert to {f.unit}: {problem}")
    return {"value": number, "unit": unit}


def _date(column: Column, value: Cell) -> dt.date:
    if isinstance(value, dt.datetime):
        if value.time() != dt.time():
            raise ValueError("is a date and time, and the field holds a date.")
        return value.date()
    if isinstance(value, dt.date):
        return value
    serial = _serial(column, value)
    if serial is not None:
        if serial != int(serial):
            raise ValueError("is a date and time, and the field holds a date.")
        return EXCEL_EPOCH + dt.timedelta(days=int(serial))
    if not isinstance(value, str):
        raise ValueError("isn't a date.")
    text = value.strip()
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        pass
    named = _named_date(text)
    if named is not None:
        return named
    numeric = _NUMERIC_DATE.fullmatch(text)
    if numeric is not None:
        a, b, year = (int(g) for g in numeric.groups())
        order = column.dates
        if order is None:
            if a == b or (a > 12) != (b > 12):
                order = "day_first" if a > 12 or a == b else "month_first"
            else:
                raise Ambiguous(
                    f"reads as {_day(a, b, year)} day first, and {_day(b, a, year)} month first:"
                    " say which the sheet's dates are."
                )
        day, month = (a, b) if order == "day_first" else (b, a)
        try:
            return dt.date(year, month, day)
        except ValueError:
            said = "day first" if order == "day_first" else "month first"
            raise ValueError(f"isn't a date, read {said}.") from None
    raise ValueError("isn't a date: write it as 2026-03-01, 1 Mar 2026 or 01/03/2026.")


def _day(day: int, month: int, year: int) -> str:
    try:
        return dt.date(year, month, day).strftime("%-d %B %Y")
    except ValueError:
        return "no date"


def _serial(column: Column, value: Cell) -> float | None:
    """A plain number in a date column, as an Excel serial date if the person said its
    dates are (and None if it isn't a number). Raises ``ValueError`` if they didn't."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d{1,6}(?:\.\d+)?", text):
            return None
        number = float(text)
    elif isinstance(value, int | float):
        number = float(value)
    else:
        return None
    if not column.serial_dates:
        example = (EXCEL_EPOCH + dt.timedelta(days=int(number))).strftime("%-d %B %Y")
        raise ValueError(
            f"is a number: if the sheet's dates are Excel serial dates ({int(number)} is "
            f"{example}), say so."
            if 1 <= number <= 2_958_465
            else "is a number, not a date."
        )
    if not 1 <= number <= 2_958_465:  # 1 January 1900 to 31 December 9999
        raise ValueError("isn't an Excel serial date: they're from 1 to 2958465.")
    return number


def _named_date(text: str) -> dt.date | None:
    for pattern, order in ((_DAY_MONTH_YEAR, "dmy"), (_MONTH_DAY_YEAR, "mdy")):
        found = pattern.fullmatch(text)
        if found is None:
            continue
        first, second, year = found.groups()
        day, name = (first, second) if order == "dmy" else (second, first)
        month = _MONTHS.get(name.casefold())
        if month is None:
            return None
        try:
            return dt.date(int(year), month, int(day))
        except ValueError:
            return None
    return None


def _datetime(column: Column, value: Cell) -> dt.datetime:
    serial = _serial(column, value) if not isinstance(value, dt.date) else None
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime.combine(value, dt.time())
    elif serial is not None:
        moment = dt.datetime.combine(EXCEL_EPOCH, dt.time()) + dt.timedelta(days=serial)
        moment = moment.replace(microsecond=0)
    elif isinstance(value, str):
        try:
            moment = dt.datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError("isn't a date and time: write it as 2026-03-01T09:30.") from None
    else:
        raise ValueError("isn't a date and time.")
    if moment.tzinfo is None:
        if not column.time_zone:
            raise ValueError(
                "has no time zone: give the column's, or write it in the cell (+01:00)."
            )
        moment = moment.replace(tzinfo=zoneinfo.ZoneInfo(column.time_zone))
    return moment


def is_time_zone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def is_id(text: str) -> bool:
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


def ucum(text: str) -> str:
    cleaned = text.strip().replace("µ", "u").replace("μ", "u")
    return _SPELLINGS.get(cleaned, cleaned)


def _lookup(values: Mapping[str, str], text: str) -> str | None:
    for given, meaning in values.items():
        if given.strip().casefold() == text.casefold():
            return meaning
    return None


def _plain(text: str) -> str:
    return re.sub(r"[\s_-]+", "", text.casefold())

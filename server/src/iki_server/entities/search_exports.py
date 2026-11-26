"""A search's results as a table (ADR 0175): the entity grid's columns, and its values as
text, as the web app's grid shows them (``valueText`` in ``web/app/src/entities.ts``).

A value is the reader's: a reference by its HRID, or "An entity you can’t read"; a person
by their name; a state by its label. Where a spreadsheet needs something other than what
the grid shows, the table says it as data: numbers in full (``11.981``, not ``11.98``),
with a quantity's unit in its column's heading, as the grid has it; dates and times in ISO
8601, times in UTC. Each row ends with the record's revision and UUID, so it's known which
record, as of when, it shows.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["Column", "NoColumn", "columns", "heading", "text"]

#: A row: a record as :func:`text` reads it, its values and what they refer to, as the API
#: gives them (``values``, ``refs``, ``files``, ``people``, ``request``), and its own
#: columns (``hrid``, ``type``, ``title``, ``project``, ``updated_at``, ``revision``, ``id``).
type Row = Mapping[str, Any]


class NoColumn(ValueError):  # noqa: N818 - reads as "no such column"
    """A column asked for that the table hasn't got: which one, and why."""

    def __init__(self, index: int, detail: str) -> None:
        super().__init__(detail)
        self.index = index


@dataclass(frozen=True)
class Column:
    """A column: what a search's export names it by, its heading, and its cells."""

    key: str
    heading: str
    cell: Callable[[Row], str]


def _when(value: Any) -> str:  # noqa: ANN401 - a datetime, or one as JSON
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return "" if value is None else str(value)


#: Every entity's own columns: (key, heading, cell).
_OWN: dict[str, tuple[str, Callable[[Row], str]]] = {
    "hrid": ("ID", lambda r: str(r["hrid"])),
    "type": ("Type", lambda r: str(r["type_label"])),
    "title": ("Title", lambda r: "" if r["title"] == r["hrid"] else str(r["title"])),
    # A request's sides (ADR 0132): the project that asked, and the service it was sent to.
    "asked_by": ("Asked by", lambda r: _side(r, "asking_project")),
    "sent_to": ("Sent to", lambda r: _side(r, "service") or "Not sent yet"),
    "project": ("Project", lambda r: str(r["project"] or "")),
    "updated_at": ("Last changed", lambda r: _when(r["updated_at"])),
    "revision": ("Revision", lambda r: str(r["revision"])),
    "id": ("UUID", lambda r: str(r["id"])),
}
#: What every row ends with: when it last changed, its revision and UUID.
_LAST = ("updated_at", "revision", "id")


def _side(row: Row, side: str) -> str:
    found = (row.get("request") or {}).get(side)
    return str(found["name"]) if found else ""


def columns(
    entity_type: Mapping[str, Any] | None,
    wanted: Sequence[str] = (),
    *,
    projects: bool = False,
) -> list[Column]:
    """The columns of a search's export: ``wanted`` (by key) in that order, or else the
    grid's for the type (``EntityTypeOut``, or None for any entity), with the owning
    project where the search spans ``projects``. Raises :class:`NoColumn`."""
    fields = {f["name"]: f for f in entity_type["fields"]} if entity_type else {}
    request = bool(entity_type and entity_type.get("request"))
    if not wanted:
        wanted = _grid(entity_type, request, projects=projects)
    out: list[Column] = []
    for index, key in enumerate(wanted):
        if key in _LAST:
            continue
        if key in fields:
            f = fields[key]
            out.append(Column(key, heading(f), _field_cell(f)))
        elif key in _OWN and (key not in ("asked_by", "sent_to") or request or not fields):
            said, cell = _OWN[key]
            out.append(Column(key, said, cell))
        else:
            where = entity_type["name"] if entity_type else "every entity"
            raise NoColumn(index, f"{where} has no column `{key}`.")
    out.extend(Column(key, *_OWN[key]) for key in _LAST)
    # Two fields with the same label (an extension's beside the type's) say which is which.
    seen = [c.heading for c in out]
    return [
        Column(c.key, f"{c.heading} [{c.key}]", c.cell) if seen.count(c.heading) > 1 else c
        for c in out
    ]


def _grid(entity_type: Mapping[str, Any] | None, request: bool, *, projects: bool) -> list[str]:
    """The grid's columns: its ID; its type if it has subtypes; a request's sides; its
    states, then its other fields; or without a type, each one's type and title."""
    where = ["project"] if projects else []
    if entity_type is None:
        return ["hrid", "type", "title", *where]
    fields = entity_type["fields"]
    states = [f["name"] for f in fields if f["type"] == "state"]
    rest = [f["name"] for f in fields if f["type"] != "state"]
    return [
        "hrid",
        *(["type"] if entity_type.get("subtypes") else []),
        *(["asked_by", "sent_to"] if request else []),
        *states,
        *rest,
        *where,
    ]


def heading(f: Mapping[str, Any]) -> str:
    """A field's column heading: its label, a quantity's with its unit."""
    label = str(f["label"])
    return f"{label} ({_unit(f)})" if f["type"] == "quantity" else label


def _unit(f: Mapping[str, Any]) -> str:
    if f.get("display_scale") is not None and f.get("display_unit"):
        return str(f.get("display_unit_symbol") or f["display_unit"])
    return str(f.get("unit_symbol") or f.get("unit") or "")


def _field_cell(f: Mapping[str, Any]) -> Callable[[Row], str]:
    name = f["name"]
    return lambda row: text(f, row["values"].get(name), row)


def text(f: Mapping[str, Any], value: Any, row: Row) -> str:  # noqa: ANN401 - JSON
    """A value as the table has it: as the grid shows it, numbers and times as data."""
    if value is None:
        return ""
    if isinstance(value, list) and f.get("items") == "row":
        # A list of rows (ADR 0137): each row's values, rows apart by semicolons.
        cells = (f.get("row") or {}).get("fields") or []
        return "; ".join(
            " · ".join(t for c in cells if (t := text(c, item.get(c["name"]), row)))
            for item in value
        )
    if isinstance(value, list):
        item = {**f, "type": f.get("items") or "string"}
        return ", ".join(text(item, v, row) for v in value)
    kind = f["type"]
    if kind == "bool":
        return "Yes" if value is True else "No"
    if kind in ("int", "float"):
        return _number(value)
    if kind == "quantity":
        return _number(_shown(f, value))
    if kind == "ref":
        found = row["refs"].get(str(value))
        return str(found["hrid"]) if found else "An entity you can’t read"
    if kind == "file":
        found = row["files"].get(str(value))
        return str(found["name"]) if found else "A file you can’t read"
    if kind == "person":
        found = row["people"].get(str(value))
        if not found:
            return "Someone you can’t see"
        return str(found["name"]) if found["active"] else f"{found['name']} (deactivated)"
    if kind == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if kind == "state":
        return _state(f, value, row)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _shown(f: Mapping[str, Any], value: float) -> float:
    """A quantity in the unit its column says: its display unit, as the grid converts."""
    scale = f.get("display_scale")
    if scale is None or not f.get("display_unit"):
        return value
    converted = value * scale + (f.get("display_offset") or 0)
    # As the grid's `clean`: twelve significant digits, so 11981 u is 11.981 kDa, not
    # 11.981000000000002.
    return float(f"{converted:.12g}")


def _number(value: Any) -> str:  # noqa: ANN401 - a number, as JSON
    """A number in full, as a spreadsheet reads it back: 37, -20, 11.981, 1e-09."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _state(f: Mapping[str, Any], value: str, row: Row) -> str:
    """A state's label (ADR 0125); a request's service state before it's sent says so
    (ADR 0132), as the grid does."""
    sides = row.get("request")
    if sides and not sides["submitted"] and f["name"] in sides["service_fields"]:
        return "Not sent"
    return next((str(s["label"]) for s in f.get("states") or [] if s["name"] == value), value)

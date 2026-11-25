"""Editing a type's records as a sheet (ADR 0228), and a type's import template.

The web app's records table edits its cells with these, and so can scripts, the CLI, the
SDK and agents:

1. ``POST /projects/{id}/sheet-edits/check`` with each changed row's cells as text, as a
   person typed or pasted them, and how the sheet writes numbers and dates. Each row says
   what saving it would do, each cell how it reads (``values``, ``shown``), and every
   problem, by its cell. At most 200 rows a call: check a larger paste in parts.
2. ``POST /projects/{id}/sheet-edits`` with what was checked and reviewed: each row's
   ``values``, the revision of each record that was read, each new record's exact place,
   and a reason. Every change is made, or none: a record that changed since its revision
   is refused (412, ``stale``), and a problem with any row refuses them all (422).

``GET /entity-types/{name}/import-template.csv`` is a CSV file of a type's importable
fields, each heading with its unit, for a collaborator or vendor to fill in and import.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.utils.http import content_disposition_header
from ninja import Field, Router, Schema

from iki_server.accounts.models import User
from iki_server.api import csvs
from iki_server.api.common import caller, project
from iki_server.api.entities import (
    _CSV_OUT,
    _NOT_IN_A_NAME,
    AmountIn,
    _amount,
    _table_rows,
    _type_out,
)
from iki_server.api.problems import Problem
from iki_server.entities import access, registry, search_exports
from iki_server.entities.values import Invalid
from iki_server.spreadsheets import cells, mapping, sheets
from iki_server.spreadsheets.cells import Locale, is_time_zone

router = Router(tags=["sheets"])

Kind = Literal["edited", "unchanged", "created", "registered", "added", "blank", "failed"]


class SheetEditLocaleIn(Schema):
    """How the sheet's text writes numbers and dates, as the person says (ADR 0178). Until
    it's said, a cell that reads two ways (1,234; 03/04/2026) is a problem, never a guess."""

    decimal: Literal["point", "comma"] | None = Field(
        None, description="point: 1,234.5; comma: 1.234,5. Null until you know."
    )
    dates: Literal["day_first", "month_first"] | None = Field(
        None, description="day_first: 31/12/2026; month_first: 12/31/2026. Null until you know."
    )
    serial_dates: bool = Field(
        False, description="Plain numbers in date columns are Excel serial dates (46023)."
    )
    time_zone: str | None = Field(
        None,
        max_length=64,
        description="The IANA time zone of a date and time that gives none, such as the "
        "person's own: Europe/London. Without one, such a cell is a problem.",
    )


class SheetEditCheckRowIn(Schema):
    key: str = Field(min_length=1, max_length=64, description="The row, as the caller names it.")
    entity: UUID | None = Field(None, description="The record it is; none for a new one.")
    revision: int | None = Field(
        None, ge=1, description="The record's revision the cells were read from."
    )
    cells: dict[str, str | None] = Field(
        default_factory=dict,
        description="Each changed cell's text, by its field: null or empty to empty it.",
    )
    location: str | None = Field(
        None,
        max_length=300,
        description="A new record's place in storage: a container by its ID, code or name, "
        "and its position, such as `BOX-000002 A1`; just the container for its next free one.",
    )
    amount: str | None = Field(
        None,
        max_length=100,
        description="How much a new record starts with, in its type's amount unit or its "
        "own: `500`, `0.5 mL`.",
    )


class SheetEditCheckIn(Schema):
    type: str = Field(description="The type of the sheet's records, such as acme/protein.Sample.")
    locale: SheetEditLocaleIn | None = None
    rows: list[SheetEditCheckRowIn] = Field(max_length=sheets.MAX_CHECK_ROWS)


class SheetEditProblemOut(Schema):
    at: str = Field(
        description="Where: `values/<field>` for a cell, `location`, `amount`, or `` for the row."
    )
    detail: str
    kind: Literal["invalid", "ambiguous", "stale"] = Field(
        description="`ambiguous`: the cell reads two ways, by how the sheet writes numbers "
        "or dates; `stale`: the record changed since its revision was read."
    )


class SheetEditPlaceIn(Schema):
    container: UUID = Field(description="The container it goes in.")
    position: str | None = Field(
        None, max_length=20, description="Its position, such as A1; none for the next free one."
    )


class SheetEditPlaceOut(Schema):
    container: UUID
    hrid: str
    title: str
    position: str | None
    shown: str = Field(description="As the sheet says it: `BOX-000002 A1`.")


class SheetEditRowOut(Schema):
    key: str
    kind: Kind = Field(
        description="What saving it does: `edited`; `unchanged`; `created`, `registered` "
        "(a new registry entry) or `added` (a record of an entry already registered); "
        "`blank`, a new row with nothing in it; or `failed`, with its problems."
    )
    entity: UUID | None = Field(description="The record it changes; none for a new one.")
    hrid: str | None
    revision: int | None = Field(description="The record's revision the check read.")
    values: dict[str, Any] = Field(
        description="Each cell as it reads, as an edit gives it: send these to save."
    )
    shown: dict[str, str] = Field(
        description="Each cell as the record would have it, as the grid and its CSV say it."
    )
    location: SheetEditPlaceOut | None = None
    amount: AmountIn | None = None
    amount_shown: str | None = None
    problems: list[SheetEditProblemOut]
    held: list[str] = Field(
        description="Rules that hold it for review once saved (ADR 0127): saving needs "
        "`hold_reason`."
    )


class SheetEditCheckOut(Schema):
    rows: list[SheetEditRowOut]


class SheetEditSaveRowIn(Schema):
    key: str = Field(min_length=1, max_length=64)
    entity: UUID | None = Field(None, description="The record it changes; none for a new one.")
    revision: int | None = Field(
        None,
        ge=1,
        description="The revision of the record that was read and reviewed: required for a "
        "record. If it's changed since, nothing is saved.",
    )
    values: dict[str, Any] = Field(
        default_factory=dict, description="Each field's value, as the check read it."
    )
    location: SheetEditPlaceIn | None = None
    amount: AmountIn | None = None


class SheetEditSaveIn(Schema):
    type: str
    reason: str = Field(
        min_length=1, max_length=2000, description="Why, for each revision and the audit trail."
    )
    hold_reason: str | None = Field(
        None,
        max_length=2000,
        description="Why to save rows that break rules holding records for review (ADR 0127).",
    )
    locale: SheetEditLocaleIn | None = Field(
        None, description="How the cells were read, for the audit trail."
    )
    rows: list[SheetEditSaveRowIn] = Field(max_length=sheets.MAX_ROWS)


class SheetEditSavedRowOut(Schema):
    key: str
    kind: Kind
    entity: UUID | None
    hrid: str | None
    revision: int | None


class SheetEditSaveOut(Schema):
    rows: list[SheetEditSavedRowOut]
    edited: int
    created: int


def _locale(given: SheetEditLocaleIn | None) -> tuple[Locale, str | None]:
    if given is None:
        return Locale(), None
    if given.time_zone is not None and not is_time_zone(given.time_zone):
        raise Problem(
            422,
            "Some fields are invalid.",
            code="validation_failed",
            errors=[
                {"pointer": "#/locale/time_zone", "detail": "must be a time zone: Europe/London."}
            ],
        )
    return Locale(given.decimal, given.dates, given.serial_dates), given.time_zone


def _sheet(me: User, project_id: UUID, type_name: str) -> Any:  # noqa: ANN401 - a Project
    place = project(me, project_id, access.edit_action(type_name))
    try:
        sheets.sheet_type(type_name)
    except Invalid as error:
        raise Problem(
            422,
            " ".join(error.errors["type"]),
            errors=[{"pointer": "#/type", "detail": " ".join(error.errors["type"])}],
        ) from None
    return place


def _pointer(index: int, at: str) -> str:
    return f"#/rows/{index}/{at}" if at else f"#/rows/{index}"


def _problem_out(p: sheets.Problem) -> dict[str, Any]:
    return {"at": p.at, "detail": p.detail, "kind": p.kind}


def cell_text(f: Mapping[str, Any], value: Any, row: Mapping[str, Any]) -> str:  # noqa: ANN401
    """A value as a sheet's cell has it, to be typed back and read the same: as the grid's
    CSV writes it (ADR 0175), but a list's items apart by ``;``, as a cell's list is read,
    and a person by their email, or their ID when it can't be seen."""
    if value is None:
        return ""
    if isinstance(value, list) and f.get("items") != "row":
        item = {**f, "type": f.get("items") or "string"}
        return f"{cells.SEPARATOR} ".join(cell_text(item, v, row) for v in value)
    if f["type"] == "person":
        found = row["people"].get(str(value))
        return str(found["email"] or value) if found else str(value)
    return search_exports.text(f, value, row)


def _describe(me: User, type_name: str, rows: list[sheets.Row]) -> Any:  # noqa: ANN401
    """Reads each outcome, while its changes are there: each cell it read, as the grid's
    CSV says it (ADR 0175), and where a new record goes and how much it starts with."""
    from iki_server.amounts import services as amounts

    fields = {f["name"]: f for f in _type_out(registry.types()[type_name])["fields"]}

    def describe(outcomes: list[sheets.Outcome]) -> None:
        done = [o for o in outcomes if o.entity is not None and not o.problems]
        if not done:
            return
        read = {r["id"]: r for r in _table_rows(me, [o.entity for o in done if o.entity])}
        held = amounts.amounts_of([o.entity for o in done if o.entity])
        boxes = {o.container.pk: o.container for o in done if o.container is not None}
        titles = {r["id"]: str(r["title"]) for r in _table_rows(me, list(boxes.values()))}
        for row, outcome in zip(rows, outcomes, strict=True):
            entity = outcome.entity
            if entity is None or outcome.problems:
                continue
            table = read[entity.pk]
            outcome.shown["values"] = {
                name: cell_text(fields[name], table["values"].get(name), table)
                for name in row.values
                if name in fields
            }
            if outcome.container is not None:
                outcome.shown["location"] = {
                    "container": outcome.container.pk,
                    "hrid": outcome.container.hrid,
                    "title": titles.get(outcome.container.pk, outcome.container.hrid),
                    "position": outcome.position,
                    "shown": " ".join(
                        t for t in (outcome.container.hrid, outcome.position or "") if t
                    ),
                }
            if row.amount is not None and (kept := held.get(entity.pk)) is not None:
                # In the column's unit, which its heading says, as a quantity's cell is.
                said = _amount(entity, kept, table["values"])
                display = said["display"] if said else None
                outcome.shown["amount"] = (
                    display.rsplit(" ", 1)[0] if display else amounts.shown(kept.value)
                )

    return describe


@router.post(
    "/projects/{project_id}/sheet-edits/check",
    response=SheetEditCheckOut,
    summary="Check a sheet's changes",
)
def check_sheet(
    request: HttpRequest, project_id: UUID, payload: SheetEditCheckIn
) -> dict[str, Any]:
    """Reads each row's cells as the type's fields read a spreadsheet's (ADR 0178), and
    finds what saving them would do by making the changes, then undoing them: so a cell
    is held to every rule an edit is. Nothing is saved, and no HRID is spent. Needs Editor
    in the project, and in each record's own."""
    me = caller(request)
    place = _sheet(me, project_id, payload.type)
    locale, zone = _locale(payload.locale)
    given = [
        sheets.Given(r.key, r.entity, r.revision, r.cells, r.location, r.amount)
        for r in payload.rows
    ]
    try:
        rows = sheets.read(me, place, payload.type, given, locale, zone)
        outcomes = sheets.check(
            me, place, payload.type, rows, describe=_describe(me, payload.type, rows)
        )
    except Invalid as error:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": f"#/{k}", "detail": " ".join(v)} for k, v in error.errors.items()],
        ) from None
    out = []
    for row, outcome in zip(rows, outcomes, strict=True):
        record = outcome.entity
        shown = outcome.shown
        out.append(
            {
                "key": row.key,
                "kind": "failed" if outcome.problems else outcome.kind,
                "entity": row.entity,
                "hrid": record.hrid if record is not None and row.entity is not None else None,
                "revision": row.revision,
                "values": _json(row.values),
                "shown": shown.get("values", {}),
                "location": shown.get("location"),
                "amount": row.amount,
                "amount_shown": shown.get("amount"),
                "problems": [_problem_out(p) for p in outcome.problems],
                "held": outcome.held,
            }
        )
    return {"rows": out}


def _json(values: dict[str, Any]) -> dict[str, Any]:
    """Values as JSON: a cell's read value is JSON already, but for its references' IDs."""
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in values.items()}


@router.post(
    "/projects/{project_id}/sheet-edits",
    response=SheetEditSaveOut,
    summary="Save a sheet's changes, all or none",
)
def save_sheet(request: HttpRequest, project_id: UUID, payload: SheetEditSaveIn) -> dict[str, Any]:
    """Makes the changes reviewed (ADR 0228, 0207): each record's, from the revision read,
    as a revision with the reason; each new record, with its starting amount and place.
    All of them are made, or none. If a record changed since its revision, nothing is
    (412, ``stale``, its rows in ``rows``); if any row has a problem, nothing is (422,
    each problem at ``#/rows/<n>/values/<field>``, ``…/location``, ``…/amount`` or the row).
    The audit trail records them as one change, ``records.sheet_saved``."""
    me = caller(request)
    place = _sheet(me, project_id, payload.type)
    locale, _ = _locale(payload.locale)
    missing = [
        {"pointer": f"#/rows/{i}/revision", "detail": "is required: the revision you read."}
        for i, r in enumerate(payload.rows)
        if r.entity is not None and r.revision is None
    ]
    if missing:
        raise Problem(422, "Some fields are invalid.", code="validation_failed", errors=missing)
    rows = [
        sheets.Row(
            r.key,
            r.entity,
            r.revision,
            dict(r.values),
            sheets.Place(r.location.container, r.location.position) if r.location else None,
            r.amount.model_dump(mode="json") if r.amount else None,
        )
        for r in payload.rows
    ]
    try:
        outcomes = sheets.save(
            me,
            place,
            payload.type,
            rows,
            reason=payload.reason,
            hold_reason=payload.hold_reason or "",
            locale=locale,
        )
    except sheets.Stale as stale:
        raise Problem(
            412,
            str(stale),
            code="stale",
            extensions={
                "rows": [
                    {"key": key, "entity": str(e.pk), "hrid": e.hrid, "revision": e.revision}
                    for key, e in stale.rows
                ]
            },
        ) from None
    except sheets.Failed as failed:
        index = {r.key: i for i, r in enumerate(payload.rows)}
        raise Problem(
            422,
            str(failed),
            code="sheet_problems",
            errors=[
                {"pointer": _pointer(index.get(o.key, 0), p.at), "detail": p.detail, "kind": p.kind}
                for o in failed.outcomes
                for p in o.problems
            ],
        ) from None
    except Invalid as error:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": f"#/{k}", "detail": " ".join(v)} for k, v in error.errors.items()],
        ) from None
    out = [
        {
            "key": o.key,
            "kind": o.kind,
            "entity": o.entity.pk if o.entity is not None else None,
            "hrid": o.entity.hrid if o.entity is not None else None,
            "revision": o.entity.revision if o.entity is not None else None,
        }
        for o in outcomes
    ]
    return {
        "rows": out,
        "edited": sum(1 for o in outcomes if o.kind == "edited"),
        "created": sum(1 for o in outcomes if o.kind in ("created", "registered", "added")),
    }


@router.get(
    "/entity-types/{qualified:name}/import-template.csv",
    summary="A type's import template",
    openapi_extra=_CSV_OUT,
)
def import_template(request: HttpRequest, name: str) -> HttpResponse:
    """A CSV file with a heading for each field of the type a spreadsheet import can set
    (ADR 0178), in the type's order, a quantity's with its unit: `Concentration (mg/mL)`.
    The import maps each heading back to its field. No heading starts as a formula would
    (ADR 0175), and the file starts with a byte order mark, so a spreadsheet opens its
    units' symbols as they are."""
    caller(request)
    found = registry.types().get(name)
    if found is None or not mapping.is_type(name):
        raise Problem(404, f"There’s no entity type {name} to import.")
    fields = {f["name"]: f for f in _type_out(found)["fields"]}
    headings = [
        search_exports.heading(fields[t.name])
        for t in mapping.targets(name)
        if t.why_not is None and t.name in fields
    ]
    label = " ".join(_NOT_IN_A_NAME.sub("_", found.label_plural or f"{found.label}s").split())
    response = HttpResponse(
        "﻿" + "".join(csvs.lines([headings])), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = content_disposition_header(
        True, f"{label} import template.csv"
    )
    response["Cache-Control"] = "no-store"
    return response

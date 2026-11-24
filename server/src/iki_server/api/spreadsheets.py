"""Importing a spreadsheet into a project's records (ADR 0178): a CSV file or an XLSX
workbook added to the project, its sheet and rows chosen, its columns mapped to a type's
fields, every row checked, then written by a run of ``iki/core.import_spreadsheet``.

The web app's importer uses these, and so can scripts, the CLI and the SDK:

1. Upload the workbook to the project (``POST /uploads``), then
   ``POST /projects/{id}/spreadsheet-imports`` with its file and the type. Iki reads it.
2. ``GET /spreadsheet-imports/{id}`` until its ``state`` is ``mapping``: it lists the
   sheets, the chosen sheet's first rows, and a mapping Iki suggests. ``PATCH`` it to
   choose another sheet, header row or rows, or to map the columns.
3. ``POST …/preview``, then ``GET`` it until it's ``previewed``: its ``counts`` say what
   importing does, and ``GET …/rows`` lists each row's problems and duplicates.
4. ``POST …/commit``. Its ``run`` writes the records, all of them or none.

Every read of an import carries an ``ETag``, its ``revision``: one more with each change to
what it imports. ``PATCH``, ``…/preview``, ``…/commit`` and ``…/mappings`` must send it
back as ``If-Match`` (428 without it). If it's moved on since, from another tab or a
script, the request is refused (412, ``stale``) and changes nothing: read it again, and
choose from what it is now.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from ninja import Field, HeaderEx, P, Query, Router, Schema
from ninja.responses import Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project
from iki_server.api.entities import _expected
from iki_server.api.files import _storage
from iki_server.api.pagination import PageParams, page, page_by
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef
from iki_server.entities import registry
from iki_server.entities.values import Invalid, shape
from iki_server.org.permissions import Action, can
from iki_server.org.services import Refused
from iki_server.spreadsheets import mapping, services
from iki_server.spreadsheets.cells import SEPARATOR, Column, Locale
from iki_server.spreadsheets.models import (
    ImportState,
    PreviewRow,
    RowOutcome,
    SavedMapping,
    SpreadsheetImport,
)
from iki_spec.model import Field as SpecField

router = Router(tags=["spreadsheets"])

State = Literal[
    "reading",
    "mapping",
    "previewing",
    "previewed",
    "committing",
    "importing",
    "imported",
    "failed",
    "cancelled",
    "discarded",
]
Outcome = Literal["create", "register", "add", "reuse", "same", "problem"]


class SpreadsheetImportIn(Schema):
    file: UUID = Field(description="The workbook: a file added to the project.")
    type: str = Field(description="The entity type its rows become, such as acme/dna.Plasmid.")


class SheetColumnIn(Schema):
    index: int = Field(ge=0, description="The column, from 0 for A.")
    field: str | None = Field(None, description="The field its cells set; null to leave it out.")
    unit: str | None = Field(
        None,
        description="A quantity's cells' UCUM unit when they don't say one (12.5, not "
        "12.5 ug/mL): the field's, if none. Iki converts them to the field's.",
    )
    separator: str | None = Field(None, description=f"A list's items' separator: `{SEPARATOR}`.")
    dates: Literal["day_first", "month_first"] | None = Field(
        None, description="How 03/04/2026 reads, if not as the sheet's locale says."
    )
    serial_dates: bool | None = Field(
        None, description="Its plain numbers are Excel serial dates, if not as the locale says."
    )
    time_zone: str | None = Field(
        None, description="A date and time's, when its cells give none: an IANA name."
    )
    decimal: Literal["point", "comma"] | None = Field(
        None, description="A number's decimal mark, if not as the sheet's locale says."
    )
    values: dict[str, str] | None = Field(
        None,
        description="What a cell says, for each choice it means: an enumeration's, or for a "
        "truth value, true or false.",
    )


class SheetLocaleIn(Schema):
    """How the sheet's text writes numbers and dates. Until it's said, a cell that reads
    two ways (1,234; 03/04/2026) is a problem, never a guess."""

    decimal: Literal["point", "comma"] | None = Field(
        None, description="point: 1,234.5; comma: 1.234,5. Null until you know."
    )
    dates: Literal["day_first", "month_first"] | None = Field(
        None, description="day_first: 31/12/2026; month_first: 12/31/2026. Null until you know."
    )
    serial_dates: bool = Field(
        False, description="Plain numbers in date columns are Excel serial dates (46023)."
    )


class SheetLocaleOut(Schema):
    decimal: str | None
    dates: str | None
    serial_dates: bool


class CellReadingOut(Schema):
    row: int
    cell: str = Field(description="As the sheet shows it.")
    stored: bool = Field(
        description="The workbook holds it as a number or a date, so it reads one way only."
    )
    reads_as: str | None
    ambiguous: bool
    problem: str | None


class ColumnReadingOut(Schema):
    column: int
    field: str
    kind: Literal["numbers", "dates"]
    cells: list[CellReadingOut]


class SpreadsheetImportChange(Schema):
    sheet: str | None = None
    header_row: int | None = Field(None, ge=0, description="From 1; 0 when there's none.")
    first_row: int | None = Field(None, ge=1, description="The first row imported, from 1.")
    last_row: int | None = Field(None, ge=0, description="The last row imported; 0 for the last.")
    skip: list[list[int]] | None = Field(
        None, max_length=1000, description="Rows left out besides, as [from, to] ranges."
    )
    columns: list[SheetColumnIn] | None = Field(
        None, max_length=1000, description="How each column maps; those not given are left out."
    )
    mapping: UUID | None = Field(None, description="A saved mapping to map the columns by.")
    locale: SheetLocaleIn | None = None


class SpreadsheetCommitIn(Schema):
    skip_problems: bool = Field(
        False, description="Leave out the rows with problems, and import the rest."
    )
    reason: str = Field("", max_length=2000, description="Why, for its run and the audit trail.")


class SavedMappingIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    replace: UUID | None = Field(None, description="A saved mapping to change, rather than add.")
    target_revision: int | None = Field(
        None, ge=1, strict=True, description="The reviewed saved template revision, with replace."
    )


class WorkbookRef(Schema):
    id: UUID
    name: str
    size: int
    media_type: str


class SheetOut(Schema):
    name: str
    rows: int = Field(description="Up to its last row with anything in it.")
    columns: int
    hidden: bool


class SheetColumnOut(Schema):
    index: int
    letter: str
    header: str
    field: str | None
    unit: str | None
    separator: str
    dates: str | None
    time_zone: str | None
    decimal: str | None
    serial_dates: bool | None
    values: dict[str, str]
    examples: list[str] = Field(description="Its first few cells under the header.")


class FieldTargetOut(Schema):
    """A field of the type a column may set, or why it can't."""

    name: str
    label: str
    type: str
    item_type: str | None = Field(description="A list's items' type.")
    unit: str | None
    units: list[str] = Field(description="Units its quantity's cells may be in, for a form.")
    choices: list[str]
    to: list[str] = Field(description="A reference's types: cells name one by HRID or name.")
    required: bool
    identity: bool = Field(description="Part of what makes a record the same as another.")
    unique: bool
    why_not: str | None = Field(description="Why a column can't set it, if it can't.")


class SavedMappingOut(Schema):
    id: UUID
    revision: int
    name: str
    type: str
    headers: list[str]
    columns: list[dict[str, Any]]
    header_row: int
    locale: SheetLocaleOut
    created_by: PersonRef
    updated_by: PersonRef
    updated_at: datetime

    @staticmethod
    def resolve_locale(saved: SavedMapping) -> dict[str, Any]:
        return Locale.of(saved.locale).as_json()


class SavedMappingMatch(Schema):
    id: UUID
    name: str
    match: float = Field(description="The share of its headers the sheet has.")


class AvailableMapping(SavedMappingMatch):
    revision: int


class AppliedMappingOut(Schema):
    template: SavedMappingMatch | None
    header_row: int
    locale: SheetLocaleOut
    columns: list[dict[str, Any]] = Field(
        description="Mapped columns, including the column index, header and how each was read."
    )


class SpreadsheetPreviewOut(Schema):
    phase: str
    progress: float
    message: str
    counts: dict[str, int] = Field(
        description="Rows by what importing does: create, register (a new registry entry), "
        "add (a new record of an entry registered already), reuse (the project's record of "
        "it, unchanged), same (as an earlier row), problem; blank and left_out rows; and held "
        "(written, then held for review by a plugin's rule)."
    )


class ImportRunRef(Schema):
    id: UUID
    hrid: str
    status: str
    progress: float | None
    message: str


class SheetProblemOut(Schema):
    row: int | None
    column: int | None
    field: str | None
    detail: str
    kind: Literal["ambiguous"] | None = Field(
        None, description="ambiguous: it reads two ways; say how the sheet's text is written."
    )


class SpreadsheetImportOut(Schema):
    id: UUID
    revision: int = Field(
        description="Advances for each change to what it imports and each new preview. "
        "Send it as If-Match to change, preview, commit or save its mapping."
    )
    project: UUID = Field(alias="project_id")
    type: str
    file: WorkbookRef
    state: State
    error: str
    kind: str = Field(description="xlsx or csv, once it's read.")
    encoding: str = Field(description="A CSV file's, as Iki read it.")
    separator: str = Field(description="A CSV file's.")
    requested_by: PersonRef
    created_at: datetime
    updated_at: datetime
    sheets: list[SheetOut]
    sheet: str
    header_row: int
    first_row: int
    last_row: int | None
    skip: list[list[int]]
    locale: SheetLocaleOut
    readings: list[ColumnReadingOut] = Field(
        description="How the mapped columns' numbers and dates read, on the sheet's own cells."
    )
    sample: list[list[str]] = Field(description="The sheet's first rows, as text.")
    columns: list[SheetColumnOut]
    fields: list[FieldTargetOut]
    mapping: SavedMappingMatch | None = Field(
        description="The saved mapping it was mapped by, pinned on committed imports."
    )
    applied_mapping: AppliedMappingOut | None = Field(
        None, description="The exact mapping confirmed at commit, kept on the import."
    )
    mappings: list[AvailableMapping] = Field(
        description="The project's saved mappings for the type, best matched first."
    )
    warnings: list[str]
    preview: SpreadsheetPreviewOut | None
    skip_problems: bool
    run: ImportRunRef | None
    run_problems: list[SheetProblemOut] = Field(
        description="If its run didn't write it: why, by row and column."
    )
    can: dict[str, bool]


class PreviewRowOut(Schema):
    row: int
    outcome: Outcome
    match: str = Field(description="The HRID it is, or will be a record of.")
    match_id: UUID | None
    same_as: int | None
    problems: list[SheetProblemOut]
    held: list[dict[str, str]]


class PreviewRowParams(PageParams):
    outcome: list[Outcome] | None = Field(None, description="Only rows with these outcomes.")


# --- Helpers -----------------------------------------------------------------------------------


def _find(me: User, import_id: UUID) -> SpreadsheetImport:
    found = (
        services.visible(me)
        .select_related("file__blob", "requested_by", "project", "run")
        .filter(pk=import_id)
        .first()
    )
    if found is None:
        raise Problem(404, "No such import, or it isn't yours to see.")
    return found


def _writable(me: User, import_id: UUID) -> SpreadsheetImport:
    found = _find(me, import_id)
    if found.requested_by_id != me.pk:
        raise Problem(403, "It's someone else's import.")
    if not can(me, Action.PROJECT_EDIT, found.project):
        raise Problem(403, f"You can't add records to {found.project.name}.")
    return found


def _etag(imported: SpreadsheetImport) -> str:
    return f'"{imported.revision}"'


def _one(me: User, imported: SpreadsheetImport, response: HttpResponse) -> dict[str, Any]:
    response["ETag"] = _etag(imported)
    return _out(me, imported)


def _invalid(error: Invalid) -> Problem:
    return Problem(
        422,
        "Some fields are invalid.",
        errors=[
            {"pointer": f"#/{name}", "detail": " ".join(messages)}
            for name, messages in error.errors.items()
        ],
    )


def _call(work: Any) -> Any:  # noqa: ANN401 - whatever it returns
    try:
        return work()
    except services.Stale as stale:
        raise Problem(
            412,
            f"It changed since you read it, in another tab or session: it’s at revision "
            f"{stale.current} now, and nothing was changed. Read it again, then choose from "
            "what it is now.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    except Refused as error:
        if error.field:
            raise Problem(
                422,
                "Some fields are invalid.",
                errors=[{"pointer": f"#/{error.field}", "detail": str(error)}],
            ) from None
        raise
    except PermissionDenied as error:
        raise Problem(403, str(error) or None) from None


def _target(target: mapping.Target, identity: set[str]) -> dict[str, Any]:
    f = target.field
    item: SpecField = f.items if f.type == "list" and f.items is not None else f
    return {
        "name": f.name,
        "label": f.label or f.name.replace("_", " ").capitalize(),
        "type": f.type,
        "item_type": item.type if item is not f else None,
        "unit": item.unit,
        "units": list(mapping.unit_choices(item)) if item.type == "quantity" else [],
        "choices": list(item.values),
        "to": sorted(item.to) if item.type == "ref" else [],
        "required": f.required,
        "identity": f.name in identity,
        "unique": f.unique,
        "why_not": target.why_not,
    }


def _out(me: User, imported: SpreadsheetImport) -> dict[str, Any]:
    sheet = services.sheet_of(imported)
    sample: list[list[str]] = sheet.get("sample", [])
    headers = services.headers_of(imported)
    exists = imported.type in registry.types()
    targets = mapping.targets(imported.type) if exists else []
    entity_type = registry.types().get(imported.type)
    owner = registry.identity_owner(entity_type) if entity_type else None
    identity = set(owner.identity) if owner else set()
    by_index = {c.index: c for c in mapping.columns_from_json(imported.columns)}
    columns = []
    start = max(imported.first_row, 1)
    for index, header in enumerate(headers):
        column = by_index.get(index) or Column(index)
        columns.append(
            {
                **column.as_json(),
                "letter": mapping.letter(index),
                "header": header,
                "unit": column.unit,
                "separator": column.separator,
                "dates": column.dates,
                "time_zone": column.time_zone,
                "decimal": column.decimal,
                "serial_dates": column.serial_dates,
                "values": dict(column.values),
                "examples": [
                    row[index]
                    for row in sample[start - 1 : start + 4]
                    if index < len(row) and row[index]
                ][:3],
            }
        )
    mapped = {c.field for c in by_index.values() if c.field}
    warnings = [
        f"{t['label']} is required, and no column is mapped to it."
        for t in (_target(x, identity) for x in targets)
        if t["required"]
        and t["why_not"] is None
        and t["name"] not in mapped
        and not _has_default(imported.type, t["name"])
    ]
    saved = list(SavedMapping.objects.filter(project=imported.project_id, type=imported.type))
    fields = {t.name: t.field for t in targets}
    scored = sorted(
        ((mapping.from_saved(s.columns, headers, fields).score, s) for s in saved),
        key=lambda m: (-m[0], m[1].name),
    )
    suggested = [
        {"id": s.pk, "revision": s.revision, "name": s.name, "match": score} for score, s in scored
    ]
    state = services.state_of(imported)
    pinned = imported.applied_mapping
    if pinned is not None or imported.state in (ImportState.COMMITTING, ImportState.COMMITTED):
        # A legacy record's current template can no longer prove what it used.
        shown_mapping = pinned["template"] if pinned is not None else None
    else:
        shown_mapping = imported.mapping_source
    mine = imported.requested_by_id == me.pk and can(me, Action.PROJECT_EDIT, imported.project)
    run = imported.run
    run_out = None
    if run is not None:
        job = run.jobs.order_by("-attempt").first()
        run_out = {
            "id": run.pk,
            "hrid": run.hrid,
            "status": run.status,
            "progress": job.progress if job else None,
            "message": job.progress_message if job else "",
        }
    previewing = imported.round > 0 and imported.state in (
        ImportState.PREVIEWING,
        ImportState.PREVIEWED,
        ImportState.COMMITTING,
        ImportState.COMMITTED,
    )
    return {
        "id": imported.pk,
        "revision": imported.revision,
        "project_id": imported.project_id,
        "type": imported.type,
        "file": {
            "id": imported.file.pk,
            "name": imported.file.name,
            "size": imported.file.blob.size,
            "media_type": imported.file.media_type,
        },
        "state": state,
        "error": imported.error,
        "kind": imported.kind,
        "encoding": imported.encoding,
        "separator": imported.separator,
        "requested_by": imported.requested_by,
        "created_at": imported.created_at,
        "updated_at": imported.updated_at,
        "sheets": [
            {k: s[k] for k in ("name", "rows", "columns", "hidden")} for s in imported.sheets
        ],
        "sheet": imported.sheet,
        "header_row": imported.header_row,
        "first_row": imported.first_row,
        "last_row": imported.last_row,
        "skip": imported.skip,
        "locale": Locale.of(imported.locale).as_json(),
        "readings": services.readings(imported),
        "sample": sample,
        "columns": columns,
        "fields": [_target(t, identity) for t in targets],
        "mapping": shown_mapping,
        "applied_mapping": pinned,
        "mappings": suggested,
        "warnings": warnings,
        "preview": {
            "phase": imported.phase,
            "progress": imported.progress,
            "message": imported.progress_message,
            "counts": imported.counts,
        }
        if previewing
        else None,
        "skip_problems": imported.skip_problems,
        "run": run_out,
        "run_problems": services.run_problems(imported) if state == "failed" else [],
        "can": {
            "change": mine and state in ("mapping", "previewing", "previewed"),
            "preview": mine and state in ("mapping", "previewing", "previewed"),
            "commit": mine and state == "previewed",
            "discard": mine
            and state in ("reading", "mapping", "previewing", "previewed", "failed"),
        },
    }


def _has_default(type_name: str, name: str) -> bool:
    f = shape(type_name).fields().get(name)
    return f is not None and f.has_default


# --- Imports -----------------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/spreadsheet-imports",
    response={201: SpreadsheetImportOut},
    summary="Import a spreadsheet",
)
def start_import(
    request: HttpRequest, project_id: UUID, payload: SpreadsheetImportIn, response: HttpResponse
) -> Status[Any]:
    """Starts importing a workbook the project has (an XLSX workbook, or a CSV file) as
    records of a type. Iki reads it, then suggests how its columns map. Needs an Editor."""
    me = caller(request)
    target = project(me, project_id, Action.PROJECT_EDIT)
    _storage()
    made = _call(lambda: services.start(me, target, payload.file, payload.type))
    return Status(201, _one(me, _find(me, made.pk), response))


@router.get(
    "/projects/{project_id}/spreadsheet-imports",
    response=Page[SpreadsheetImportOut],
    summary="The project's spreadsheet imports",
)
def list_imports(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    """Yours, and those committed, newest first."""
    me = caller(request)
    target = project(me, project_id, Action.PROJECT_READ)
    found = (
        services.visible(me)
        .filter(project=target)
        .exclude(state=ImportState.DISCARDED)
        .select_related("file__blob", "requested_by", "project", "run")
    )
    out = page(found, params, newest_first=True)
    out["items"] = [_out(me, i) for i in out["items"]]
    return out


@router.get(
    "/spreadsheet-imports/{import_id}",
    response=SpreadsheetImportOut,
    summary="A spreadsheet import",
)
def get_import(request: HttpRequest, import_id: UUID, response: HttpResponse) -> dict[str, Any]:
    """Its ``ETag`` is its revision: send it back as If-Match to change it."""
    me = caller(request)
    return _one(me, _find(me, import_id), response)


@router.patch(
    "/spreadsheet-imports/{import_id}",
    response=SpreadsheetImportOut,
    summary="Choose what a spreadsheet import imports",
)
def change_import(
    request: HttpRequest,
    import_id: UUID,
    payload: SpreadsheetImportChange,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Its sheet, header row and rows, and how its columns map (or a saved mapping to map
    them by), from the revision you read (If-Match). What's given is checked together, on
    the sheet it chooses, and saved all at once or not at all. Choosing another sheet
    takes its rows afresh (its header row as Iki finds it, every row after it), but for
    those given with it. Choosing another sheet or header row maps the columns afresh,
    unless the columns are given too. A preview starts again."""
    me = caller(request)
    found = _writable(me, import_id)
    expected = _expected(if_match)
    given = payload.dict(exclude_unset=True)
    choice = services.Choice(
        sheet=given.get("sheet"),
        header_row=given.get("header_row"),
        first_row=given.get("first_row"),
        last_row=given.get("last_row"),
        skip=given.get("skip"),
        columns=[{k: v for k, v in c.items() if v is not None} for c in given["columns"]]
        if given.get("columns") is not None
        else None,
        mapping=given.get("mapping"),
        locale=given.get("locale"),
    )
    with transaction.atomic():
        changed = _call(lambda: services.change(me, found, choice, expected_revision=expected))
        return _one(me, changed, response)


@router.post(
    "/spreadsheet-imports/{import_id}/preview",
    response={202: SpreadsheetImportOut},
    summary="Check a spreadsheet import's rows",
)
def preview_import(
    request: HttpRequest,
    import_id: UUID,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> Status[Any]:
    """Checks every row as importing would write it, a chunk at a time: what's wrong with
    each, by its column, and the records each would duplicate. Send the revision you read
    (If-Match). Each preview starts a new revision. Poll until that revision is previewed."""
    me = caller(request)
    found = _writable(me, import_id)
    expected = _expected(if_match)
    with transaction.atomic():
        started = _call(lambda: services.preview(me, found, expected_revision=expected))
        return Status(202, _one(me, started, response))


@router.get(
    "/spreadsheet-imports/{import_id}/rows",
    response=Page[PreviewRowOut],
    summary="What a preview found, row by row",
)
def import_rows(
    request: HttpRequest,
    import_id: UUID,
    params: Query[PreviewRowParams],
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Read its rows in sheet order. Send the preview's If-Match on each page to read
    that revision; a superseded preview returns 412. ``outcome`` filters its rows."""
    me = caller(request)
    found = _find(me, import_id)
    expected = _expected(if_match) if if_match is not None else None
    if expected is not None:
        _call(lambda: services.check_revision(found, expected))
    rows = services.outcome_rows(found, params.outcome)
    out = page_by(rows, params, by=("row",))
    out["items"] = [_row_out(r) for r in out["items"]]
    if expected is not None:
        # The preview worker skips locked drafts. Recheck the monotone revision
        # after reading this round rather than blocking its worker to read a page.
        found.refresh_from_db(fields=["revision"])
        _call(lambda: services.check_revision(found, expected))
    return out


def _row_out(row: PreviewRow) -> dict[str, Any]:
    return {
        "row": row.row,
        "outcome": row.outcome,
        "match": row.match_hrid,
        "match_id": row.match_id if row.outcome in (RowOutcome.REUSE, RowOutcome.ADD) else None,
        "same_as": row.same_as,
        "problems": [{"row": row.row, **p} for p in row.problems or []],
        "held": row.held or [],
    }


@router.post(
    "/spreadsheet-imports/{import_id}/commit",
    response={202: SpreadsheetImportOut},
    summary="Import a previewed spreadsheet",
)
def commit_import(
    request: HttpRequest,
    import_id: UUID,
    payload: SpreadsheetCommitIn,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> Status[Any]:
    """Writes its rows as records, all of them or none, as a run of
    ``iki/core.import_spreadsheet``, which checks them again as they are now. Send the
    revision whose preview you read (If-Match). Rows with problems stop it, unless
    ``skip_problems`` leaves them out."""
    me = caller(request)
    found = _writable(me, import_id)
    expected = _expected(if_match)
    _storage()
    _call(
        lambda: services.commit(
            me,
            found,
            expected_revision=expected,
            skip_problems=payload.skip_problems,
            reason=payload.reason,
        )
    )
    return Status(202, _one(me, _find(me, import_id), response))


@router.delete(
    "/spreadsheet-imports/{import_id}", response={204: None}, summary="Discard a spreadsheet import"
)
def discard_import(request: HttpRequest, import_id: UUID) -> Status[None]:
    """Before it's committed. The workbook stays in the project."""
    me = caller(request)
    found = _writable(me, import_id)
    _call(lambda: services.discard(me, found))
    return Status(204, None)


# --- Saved mappings ----------------------------------------------------------------------------


@router.post(
    "/spreadsheet-imports/{import_id}/mappings",
    response={201: SavedMappingOut},
    summary="Save a spreadsheet import's mapping",
)
def save_mapping(
    request: HttpRequest,
    import_id: UUID,
    payload: SavedMappingIn,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> Status[Any]:
    """Keeps how its columns map, by their headers, for the project's next sheet of the same
    kind: one from the same instrument or vendor maps itself. Send the revision you read
    (If-Match): its mapping is what's kept. To replace a template send its ID and reviewed
    target_revision. A stale template returns 409; a stale import returns 412."""
    me = caller(request)
    found = _writable(me, import_id)
    expected = _expected(if_match)
    try:
        saved = _call(
            lambda: services.save_mapping(
                me,
                found,
                payload.name,
                expected_revision=expected,
                replace=payload.replace,
                target_revision=payload.target_revision,
            )
        )
    except SavedMapping.DoesNotExist:
        raise Problem(404, "No such saved mapping for this type in the project.") from None
    return Status(201, saved)


@router.get(
    "/projects/{project_id}/spreadsheet-mappings",
    response=list[SavedMappingOut],
    summary="The project's saved spreadsheet mappings",
)
def list_mappings(
    request: HttpRequest, project_id: UUID, type: str | None = None
) -> list[SavedMapping]:
    me = caller(request)
    target = project(me, project_id, Action.PROJECT_READ)
    found = SavedMapping.objects.filter(project=target).select_related("created_by", "updated_by")
    if type:
        found = found.filter(type=type)
    return list(found.order_by("type", "name"))


@router.delete(
    "/spreadsheet-mappings/{mapping_id}", response={204: None}, summary="Delete a saved mapping"
)
def delete_mapping(request: HttpRequest, mapping_id: UUID) -> Status[None]:
    me = caller(request)
    saved = SavedMapping.objects.select_related("project").filter(pk=mapping_id).first()
    if saved is None or not can(me, Action.PROJECT_READ, saved.project):
        raise Problem(404, "No such saved mapping.")
    project(me, saved.project_id, Action.PROJECT_EDIT)
    try:
        _call(lambda: services.delete_mapping(me, saved))
    except SavedMapping.DoesNotExist:
        raise Problem(404, "No such saved mapping.") from None
    return Status(204, None)

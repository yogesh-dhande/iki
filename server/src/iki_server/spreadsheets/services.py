"""Importing a spreadsheet into a project's records (ADR 0178).

1. :func:`start` takes a workbook the person added to the project (ADR 0036), and the
   type its rows become. A task reads it: its sheets, and each one's first rows.
2. :func:`change` chooses its sheet, header row and rows, and maps its columns to the
   type's fields (:mod:`mapping`). Iki suggests a mapping from the headers, or the
   project's saved mapping whose headers the sheet has.
3. :func:`preview` checks every row as it would be written, a chunk at a time and
   resumably, as a bulk import checks its lines (ADR 0056): each cell read as its field
   (:mod:`cells`), each row as a create of the type (``entities.prepare``, and the
   plugins' rules), then what each would be: a new record, a new entry in the registry,
   a record of an entry registered already, the project's record it is already, or the
   same as an earlier row (ADR 0115). What's wrong with a row says its column.
4. :func:`commit` writes the rows as one run of Iki's core action
   ``iki/core.import_spreadsheet`` (:func:`imports.launch`): the bulk import of ADR
   0056 checks them again, as they are then, and writes them all at once or none, with
   HRIDs in row order. Each record's origin names the workbook, its sheet and its row.

An import is its requester's until it's committed. Each change to what it imports starts
its preview afresh (a new round); its rows are kept until it's committed or discarded.

Each change to what it imports is a new revision of it. A change, a preview, a commit and
saving its mapping each say the revision they were made from, and are refused
(:class:`Stale`) if it's moved on since: a change made in another tab, or one of the
person's own sent before it but arriving after, never goes unseen.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import structlog
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.db.models import Q, QuerySet
from django.db.models.functions import Lower
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.api.problems import Problem
from iki_server.audit import log as audit
from iki_server.entities import access, lists, registry, unique
from iki_server.entities import services as entities
from iki_server.entities.identity import hash_of
from iki_server.entities.models import UniqueValue
from iki_server.entities.values import Invalid, referable, shape
from iki_server.files import listing, scan_state, storage
from iki_server.files import services as files
from iki_server.files.models import File
from iki_server.org import permissions
from iki_server.org.models import Project, registry_of
from iki_server.org.services import Refused
from iki_server.runs import imports
from iki_server.runs.models import SERVER_TIER, ActionRun, RunStatus
from iki_server.spreadsheets import cells, mapping
from iki_server.spreadsheets.cells import Column, Locale, Reference
from iki_server.spreadsheets.models import (
    ImportState,
    PreviewPhase,
    PreviewRow,
    RowOutcome,
    SavedMapping,
    SpreadsheetImport,
)
from iki_server.spreadsheets.workbooks import (
    SAMPLE_COLUMNS,
    Unreadable,
    Workbook,
    from_json,
    open_workbook,
    text_of,
)
from iki_spec import units
from iki_spec.model import Action as SpecAction
from iki_spec.model import Creates, Field

__all__ = [
    "ACTION",
    "Stale",
    "action_for",
    "change",
    "commit",
    "discard",
    "preview",
    "save_mapping",
    "start",
    "state_of",
]

logger = structlog.get_logger("iki.spreadsheets")

#: Iki's core action whose runs write imports (ADR 0178).
ACTION = "iki/core.import_spreadsheet"
#: How long a preview's task works before it hands on to the next.
STEP_FOR = 30.0
#: Rows checked per transaction.
CHUNK_ROWS = 2_000
#: Rows whose references are looked up at once, as the sheet is read.
BLOCK_ROWS = 2_000
#: The most problems a preview lists for a row.
MAX_ROW_PROBLEMS = 20
#: Steps that fail unexpectedly, in a row, before a preview gives up.
MAX_FAILURES = 5
#: A preview that hasn't moved for this long gets another step (its runner died).
STALLED_AFTER = timedelta(minutes=10)
#: An import nobody has touched for this long, uncommitted, is discarded.
ABANDONED_AFTER = timedelta(days=14)

#: The sources origins name, by the workbook's kind (ADR 0052).
SOURCES = {"xlsx": "XLSX workbook", "csv": "CSV file"}
WRITTEN = (RowOutcome.CREATE, RowOutcome.REGISTER, RowOutcome.ADD)


class Stale(Exception):  # noqa: N818 - reads as "the request was stale"
    """The import changed since the revision a request was made from."""

    def __init__(self, current: int) -> None:
        super().__init__(f"The import changed since: it's at revision {current} now.")
        self.current = current


class StaleTemplate(Refused):
    code = "stale_template"

    def __init__(self, saved: SavedMapping) -> None:
        super().__init__(
            f"The saved template “{saved.name}” changed to revision {saved.revision}. "
            "Review the current template before replacing it."
        )
        self.extensions = {
            "target": {"id": str(saved.pk), "name": saved.name, "revision": saved.revision}
        }


def check_revision(imported: SpreadsheetImport, expected_revision: int) -> None:
    if imported.revision != expected_revision:
        raise Stale(imported.revision)


def _locked(imported: SpreadsheetImport, expected_revision: int) -> SpreadsheetImport:
    """The import, locked for the transaction, if it's still at ``expected_revision``."""
    locked = SpreadsheetImport.objects.select_for_update().get(pk=imported.pk)
    check_revision(locked, expected_revision)
    return locked


# --- The core action ---------------------------------------------------------------------------


def action_for(type_name: str) -> SpecAction:
    """``iki/core.import_spreadsheet``, creating ``type_name``'s records: what a run of it
    keeps as its action (ADR 0042), its params what was imported and how."""
    return SpecAction(
        plugin="iki/core",
        name="import_spreadsheet",
        tier=SERVER_TIER,
        environment="server",
        command=(),
        label="Import a spreadsheet",
        description="Writes a spreadsheet's rows as records, as their columns were mapped.",
        params={
            "workbook": Field("workbook", "file", label="Workbook", required=True),
            "sheet": Field("sheet", "string", label="Sheet"),
            "header_row": Field("header_row", "int", label="Header row"),
            "first_row": Field("first_row", "int", label="First row"),
            "last_row": Field("last_row", "int", label="Last row"),
            "skip": Field("skip", "json", label="Rows left out"),
            "locale": Field("locale", "json", label="How its numbers and dates were read"),
            "columns": Field("columns", "json", label="Columns"),
            "skip_problems": Field("skip_problems", "bool", label="Rows with problems left out"),
        },
        creates=(Creates(type=type_name, cardinality="many"),),
        timeout=7 * 24 * 3600,
    )


# --- Starting and choosing ---------------------------------------------------------------------


def _may_write(user: User, project: Project, type_name: str) -> None:
    if project.is_archived:
        raise Refused(f"{project.name} is archived: restore it to import into it.")
    if not permissions.can(user, access.edit_action(type_name), project):
        raise PermissionDenied(f"You can't add records to {project.name}.")


def start(user: User, project: Project, file_id: uuid.UUID, type_name: str) -> SpreadsheetImport:
    """An import of the file, a workbook in ``project``, into ``type_name``'s records. A
    task reads it. Raises :class:`Refused` naming the ``field`` that's wrong, or
    :class:`PermissionDenied`."""
    from iki_server.spreadsheets import tasks

    _may_write(user, project, type_name)
    if not mapping.is_type(type_name):
        raise Refused(f"There's no entity type {type_name} to import into.", field="type")
    why = entities.frozen(shape(type_name))
    if why:
        raise Refused(why, field="type")
    # One of the project's Files (ADR 0177), as its picker lists them: added to it, or held
    # by its records. A file it holds stays where it is; the import names it.
    added, held = listing.branches(user, project)
    file = (
        files.readable(user)
        .filter(pk=file_id)
        .filter(Q(pk__in=added.values("pk")) | Q(pk__in=held.values("pk")))
        .select_related("blob")
        .first()
    )
    if file is None:
        raise Refused(
            f"Give one of {project.name}’s files that you can read: a workbook, or a CSV file.",
            field="file",
        )
    scan_state.require_usable(file.blob)
    with transaction.atomic():
        made = SpreadsheetImport.objects.create(
            project=project,
            type=type_name,
            file=file,
            requested_by=user,
            agent=audit.current_agent(),
        )
        tasks.read_workbook.enqueue(str(made.pk))
    return made


def read(import_id: str) -> str:
    """Reads the workbook: its sheets, each one's size and first rows, and what to import
    to start with (its first visible sheet, its headers, every row after them, columns
    mapped by their headers, or by a saved mapping they all match)."""
    found = SpreadsheetImport.objects.select_related("file__blob").filter(pk=import_id).first()
    if found is None or found.state != ImportState.READING:
        return "done"
    try:
        with _workbook(found.file) as book:
            sheets = book.describe()
            found.kind, found.encoding, found.separator = book.kind, book.encoding, book.separator
            chosen = next((s for s in sheets if not s.hidden and s.rows), None) or sheets[0]
            header_row = _header_of(chosen.sample)
            headers = book.row_text(chosen.name, header_row)
    except (Unreadable, Problem) as error:
        _fail(found, str(error))
        return "unreadable"
    with transaction.atomic():
        locked = SpreadsheetImport.objects.select_for_update().get(pk=found.pk)
        if locked.state != ImportState.READING:
            return "done"
        locked.kind, locked.encoding, locked.separator = found.kind, found.encoding, found.separator
        locked.sheets = [
            {
                "name": s.name,
                "rows": s.rows,
                "columns": s.columns,
                "hidden": s.hidden,
                "sample": s.sample,
                "cells": s.cells,
            }
            for s in sheets
        ]
        locked.sheet = chosen.name
        locked.header_row, locked.headers = header_row, headers
        locked.first_row = locked.header_row + 1
        locked.last_row = None
        locked.columns = [c.as_json() for c in _suggested(locked)]
        locked.state = ImportState.MAPPING
        # Iki's reading is what it imports to start with: a revision of its own.
        locked.revision += 1
        locked.updated_at = timezone.now()
        locked.save()
    return "read"


def _header_of(sample: Sequence[Sequence[str]]) -> int:
    """A sheet's header row, as Iki first takes it: its first row with anything in it."""
    return next((i for i, row in enumerate(sample, 1) if any(c.strip() for c in row)), 1)


def _suggested(imported: SpreadsheetImport) -> list[Column]:
    """The columns to start with: a saved mapping of the project's whose every header the
    sheet has (the best, if several), else those the headers name."""
    headers = headers_of(imported)
    fields = shape(imported.type).fields()
    saved_mappings = SavedMapping.objects.filter(project=imported.project_id, type=imported.type)
    for saved in sorted(saved_mappings, key=lambda s: len(s.columns), reverse=True):
        match = mapping.from_saved(saved.columns, headers, fields)
        if match.score == 1.0:
            _remember_mapping(imported, saved, match.score)
            imported.locale = Locale.of(saved.locale).as_json()
            return match.columns
    return mapping.suggest(imported.type, headers)


def _remember_mapping(imported: SpreadsheetImport, saved: SavedMapping, score: float) -> None:
    imported.mapping_source = {"id": str(saved.pk), "name": saved.name, "match": score}


def sheet_of(imported: SpreadsheetImport) -> dict[str, Any]:
    return next((s for s in imported.sheets if s["name"] == imported.sheet), {})


def headers_of(imported: SpreadsheetImport) -> list[str]:
    """Its columns' headers, one a column of its sheet, as its header row says them."""
    row = imported.headers if imported.header_row else []
    return mapping.headers_of(row, int(sheet_of(imported).get("columns") or 0))


def _header_text(imported: SpreadsheetImport, sheet_name: str, header_row: int) -> list[str]:
    """A sheet's row's text, every column of it, to be its header row: from its sample if
    the sample holds all of it, else read from the workbook. Raises :class:`Refused` if
    the workbook can't be read now."""
    if header_row < 1:
        return []
    sheet: dict[str, Any] = next((s for s in imported.sheets if s["name"] == sheet_name), {})
    sample: list[list[str]] = sheet.get("sample", [])
    if header_row <= len(sample) and int(sheet.get("columns") or 0) <= SAMPLE_COLUMNS:
        return list(sample[header_row - 1])
    try:
        with _workbook(imported.file) as opened:
            return opened.row_text(sheet_name, header_row)
    except Unreadable as error:
        raise Refused(f"Iki couldn't read its row {header_row}: {error}") from None


def _chosen_header(imported: SpreadsheetImport, choice: Choice) -> tuple[str, int]:
    """The sheet and header row ``choice`` would leave it with, as :func:`change` finds
    them: a header row given, else another sheet's first row with anything in it."""
    sheet_name = imported.sheet
    header_row = imported.header_row
    names = [s["name"] for s in imported.sheets]
    if choice.sheet is not None and choice.sheet != imported.sheet and choice.sheet in names:
        sheet_name = choice.sheet
        found = next(s for s in imported.sheets if s["name"] == sheet_name)
        header_row = _header_of(found.get("sample", []))
    if choice.header_row is not None:
        header_row = choice.header_row
    return sheet_name, header_row


@dataclass(frozen=True)
class Choice:
    """What an import imports, as :func:`change` takes it: None leaves each as it is."""

    sheet: str | None = None
    header_row: int | None = None
    first_row: int | None = None
    #: 0 for the sheet's last.
    last_row: int | None = None
    skip: list[list[int]] | None = None
    columns: list[dict[str, Any]] | None = None
    #: A saved mapping to map the columns by (and to read the sheet's text as it says).
    mapping: uuid.UUID | None = None
    #: How the sheet's text writes numbers and dates: {decimal, dates, serial_dates}.
    locale: dict[str, Any] | None = None


def change(
    user: User, imported: SpreadsheetImport, choice: Choice, *, expected_revision: int
) -> SpreadsheetImport:
    """Chooses what it imports, from the revision the person saw: a new revision.

    Everything given is checked together, against the sheet it chooses: a new sheet and a
    header row on it are one change. Choosing another sheet takes its rows afresh too (its
    header row as Iki finds it, every row after it, none left out), but for those given
    with it: the old sheet's rows mean nothing on another. A new sheet or header row maps
    its columns afresh, unless ``columns`` are given too. Its preview, if it had one,
    starts again. Raises :class:`Stale` if it's moved on from ``expected_revision``,
    :class:`Invalid` with what's wrong, by where, or :class:`Refused`. Nothing is
    changed unless all of it is."""
    _may_write(user, imported.project, imported.type)
    # A header row the sample doesn't hold all of is read from the workbook, before its
    # lock is taken: reading far into a large sheet takes a while.
    wanted = _chosen_header(imported, choice)
    height = next((int(s.get("rows") or 0) for s in imported.sheets if s["name"] == wanted[0]), 0)
    read: tuple[tuple[str, int], list[str]] | None = None
    if wanted != (imported.sheet, imported.header_row) and 0 <= wanted[1] <= max(height, 1):
        read = (wanted, _header_text(imported, *wanted))
    with transaction.atomic():
        imported = _locked(imported, expected_revision)
        if imported.state not in (
            ImportState.MAPPING,
            ImportState.PREVIEWED,
            ImportState.PREVIEWING,
        ):
            raise Refused(_why_not_changed(imported))
        errors: dict[str, list[str]] = {}
        names = [s["name"] for s in imported.sheets]
        remap = False
        if choice.sheet is not None and choice.sheet != imported.sheet:
            if choice.sheet not in names:
                errors["sheet"] = [f"isn't one of its sheets: {', '.join(names)}."]
            else:
                imported.sheet = choice.sheet
                remap = True
                # Its rows, afresh: those given with it are set below, on this sheet.
                imported.header_row = _header_of(sheet_of(imported).get("sample", []))
                imported.last_row = None
                imported.skip = []
        sheet = sheet_of(imported)
        rows = int(sheet.get("rows") or 0)
        if choice.header_row is not None:
            if not 0 <= choice.header_row <= max(rows, 1):
                errors["header_row"] = [f"must be a row of the sheet, from 1 to {rows}, or 0."]
            elif choice.header_row != imported.header_row:
                imported.header_row = choice.header_row
                remap = True
        if remap and not errors:
            chosen = (imported.sheet, imported.header_row)
            imported.headers = (
                read[1]
                if read is not None and read[0] == chosen
                else _header_text(imported, *chosen)
            )
        if choice.first_row is not None:
            imported.first_row = choice.first_row
        elif remap:
            imported.first_row = imported.header_row + 1
        if choice.last_row is not None:
            imported.last_row = choice.last_row or None
        if choice.skip is not None:
            imported.skip = choice.skip
        _check_rows(imported, rows, errors)
        if remap and choice.mapping is None:
            imported.mapping_source = None
        if choice.mapping is not None:
            saved = SavedMapping.objects.filter(
                pk=choice.mapping, project=imported.project_id, type=imported.type
            ).first()
            if saved is None:
                errors["mapping"] = ["isn't a mapping saved for this type in the project."]
            else:
                match = mapping.from_saved(
                    saved.columns, headers_of(imported), shape(imported.type).fields()
                )
                if match.conflicts:
                    errors["mapping"] = match.conflicts
                else:
                    _remember_mapping(imported, saved, match.score)
                    imported.locale = Locale.of(saved.locale).as_json()
                    imported.columns = [c.as_json() for c in match.columns]
        elif choice.columns is not None:
            width = int(sheet.get("columns") or 0)
            wrong = mapping.check(imported.type, choice.columns, width)
            errors.update(wrong)
            if not wrong:
                imported.columns = [c.as_json() for c in mapping.columns_from_json(choice.columns)]
        elif remap:
            imported.columns = [
                c.as_json() for c in mapping.suggest(imported.type, headers_of(imported))
            ]
        if choice.locale is not None:
            wrong = _locale_errors(choice.locale)
            errors.update(wrong)
            if not wrong:
                imported.locale = Locale.of({**imported.locale, **choice.locale}).as_json()
        if errors:
            raise Invalid(errors)
        _reset(imported)
        imported.revision += 1
        imported.save()
    return imported


def _locale_errors(given: Mapping[str, Any]) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for name in sorted(set(given) - {"decimal", "dates", "serial_dates"}):
        errors[f"locale/{name}"] = ["isn't an option: give decimal, dates or serial_dates."]
    if given.get("decimal") not in (None, *cells.DECIMALS):
        errors["locale/decimal"] = ["must be point or comma, or null until you know."]
    if given.get("dates") not in (None, *cells.DATE_ORDERS):
        errors["locale/dates"] = ["must be day_first or month_first, or null until you know."]
    if not isinstance(given.get("serial_dates", False), bool):
        errors["locale/serial_dates"] = ["must be true or false."]
    return errors


#: The most cells :func:`readings` shows a column.
READINGS = 6
#: Of them, the most the workbook stored as numbers or dates, which read one way only.
STORED_READINGS = 2


def readings(imported: SpreadsheetImport) -> list[dict[str, Any]]:
    """How the sheet's numbers and dates read, as its locale says: for each mapped column
    of them, a few of its first cells whose text reads by where it was written, each as it
    reads (or why it can't, or that it reads two ways), so the person can check them."""
    sheet = sheet_of(imported)
    typed = sheet.get("cells") or []
    if not typed or imported.type not in registry.types():
        return []
    fields = shape(imported.type).fields()
    locale = Locale.of(imported.locale)
    out = []
    for column in mapping.columns_from_json(imported.columns):
        f = fields.get(column.field or "")
        if f is None:
            continue
        item = f.items if f.type == "list" and f.items is not None else f
        kind = "dates" if item.type in ("date", "datetime") else "numbers"
        if item.type not in ("int", "float", "quantity", "date", "datetime"):
            continue
        under = column.under(locale)
        # The cells whose text reads by how the sheet writes it come first: they're what the
        # person checks. Then a couple the workbook stored, which read one way only.
        shown: list[dict[str, Any]] = []
        stored: list[dict[str, Any]] = []
        for number, row in enumerate(typed, start=1):
            if number < imported.first_row or (imported.last_row and number > imported.last_row):
                continue
            raw = from_json(row[column.index]) if column.index < len(row) else None
            if raw is None or raw == "":
                continue
            entry: dict[str, Any] = {
                "row": number,
                "cell": text_of(raw),
                "stored": not isinstance(raw, str),
            }
            try:
                value = cells.convert(f, under, raw)
            except cells.Ambiguous as error:
                entry |= {"reads_as": None, "ambiguous": True, "problem": str(error)}
            except ValueError as error:
                entry |= {"reads_as": None, "ambiguous": False, "problem": str(error)}
            else:
                entry |= {"reads_as": _reading(value, item), "ambiguous": False, "problem": None}
            (stored if entry["stored"] else shown).append(entry)
            if len(shown) >= READINGS:
                break
        picked = sorted(
            shown + stored[: max(0, min(STORED_READINGS, READINGS - len(shown)))],
            key=lambda e: int(e["row"]),
        )
        if picked:
            out.append({"column": column.index, "field": f.name, "kind": kind, "cells": picked})
    return out


def _reading(value: Any, f: Field) -> str:  # noqa: ANN401 - JSON
    """A cell's value as it reads, for the person to check: a quantity in its own unit,
    and in the field's when that's another, as it would be kept."""
    if isinstance(value, list):
        return "; ".join(_reading(v, f) for v in value)
    if isinstance(value, dict) and "value" in value:
        said = f"{cells.shown(value['value'])} {value['unit']}"
        if f.unit and value["unit"] != f.unit:
            with contextlib.suppress(units.UnitError):
                kept = units.convert(float(value["value"]), value["unit"], f.unit)
                said += f" = {cells.shown(kept)} {f.unit}"
        return said
    if isinstance(value, int | float) and not isinstance(value, bool):
        return cells.shown(value)
    return str(value)


def _why_not_changed(imported: SpreadsheetImport) -> str:
    if imported.state == ImportState.READING:
        return "Iki is still reading the workbook."
    if imported.state in (ImportState.COMMITTING, ImportState.COMMITTED):
        return "It's been imported: start another import to import it again."
    if imported.state == ImportState.FAILED:
        return imported.error or "It failed."
    return "It was discarded."


def _check_rows(imported: SpreadsheetImport, rows: int, errors: dict[str, list[str]]) -> None:
    if imported.first_row < 1 or imported.first_row <= imported.header_row:
        errors["first_row"] = ["must be a row after the header row."]
    if imported.last_row is not None and imported.last_row < imported.first_row:
        errors["last_row"] = ["must be the first row or after it."]
    skip = imported.skip
    if not isinstance(skip, list) or len(skip) > 1000:
        errors["skip"] = ["must be at most 1,000 ranges of rows, [from, to]."]
        return
    for index, given in enumerate(skip):
        if (
            not isinstance(given, list)
            or len(given) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in given)
            or given[0] > given[1]
        ):
            errors[f"skip/{index}"] = ["must be a range of rows, [from, to], from row 1."]


def _reset(imported: SpreadsheetImport) -> None:
    """Back to choosing: its preview's rows go (a batch at a time, by a task), and its
    rows as read."""
    from iki_server.spreadsheets import tasks

    if imported.round:
        tasks.clear_rows.enqueue(str(imported.pk), imported.round)
    imported.state = ImportState.MAPPING
    imported.phase = ""
    imported.progress = 0.0
    imported.progress_message = ""
    imported.counts = {}
    imported.offset = imported.lines = imported.size = 0
    imported.updated_at = timezone.now()


def save_mapping(
    user: User,
    imported: SpreadsheetImport,
    name: str,
    *,
    expected_revision: int,
    replace: uuid.UUID | None = None,
    target_revision: int | None = None,
) -> SavedMapping:
    """Keeps the import's mapping in its project, by its headers, as ``name``: or as
    ``replace``, changing it. The mapping kept is the one at ``expected_revision``, the
    one the person saw. Raises :class:`Stale` or :class:`Refused`."""
    _may_write(user, imported.project, imported.type)
    if (replace is None) != (target_revision is None):
        raise Invalid({"target_revision": ["Give a template ID and its revision together."]})
    name = name.strip()
    if not name or len(name) > 100:
        raise Refused("Give it a name, of at most 100 characters.", field="name")
    with transaction.atomic():
        imported = _locked(imported, expected_revision)
        headers = headers_of(imported)
        columns = mapping.saved_columns(mapping.columns_from_json(imported.columns), headers)
        if not columns:
            raise Refused("Map a column to a field first.", field="columns")
        kept = [str(c["header"]) for c in columns]
        if replace is None:
            saved = SavedMapping(
                project_id=imported.project_id, type=imported.type, created_by=user
            )
            what = "spreadsheet_mapping.saved"
        else:
            saved = (
                SavedMapping.objects.select_for_update(of=("self",))
                .select_related("created_by")
                .get(pk=replace, project_id=imported.project_id, type=imported.type)
            )
            if saved.revision != target_revision:
                raise StaleTemplate(saved)
            saved.revision += 1
            what = "spreadsheet_mapping.changed"
        saved.name, saved.columns, saved.headers = name, columns, kept
        saved.header_row, saved.locale = imported.header_row, imported.locale
        saved.updated_by, saved.updated_at = user, timezone.now()
        try:
            saved.save()
        except IntegrityError as error:
            diag = getattr(error.__cause__, "diag", None)
            if getattr(diag, "constraint_name", None) != "iki_spreadsheet_mapping_name":
                raise
            raise Refused(
                f"The project has a mapping called {name} already.", field="name"
            ) from error
        if imported.applied_mapping is None and imported.state not in (
            ImportState.COMMITTING,
            ImportState.COMMITTED,
        ):
            previous = imported.mapping_source
            _remember_mapping(imported, saved, 1.0)
            if previous != imported.mapping_source:
                imported.revision += 1
                imported.save(update_fields=["mapping_source", "revision"])
        audit.record(
            what,
            actor=user,
            target=saved,
            project=imported.project,
            data={"type": saved.type, "name": saved.name, "columns": len(columns)},
        )
    return saved


def delete_mapping(user: User, saved: SavedMapping) -> None:
    _may_write(user, saved.project, saved.type)
    with transaction.atomic():
        saved = SavedMapping.objects.select_for_update().get(pk=saved.pk)
        audit.record(
            "spreadsheet_mapping.deleted",
            actor=user,
            target=saved,
            project=saved.project,
            data={"type": saved.type, "name": saved.name},
        )
        saved.delete()


# --- Previewing --------------------------------------------------------------------------------


def preview(
    user: User, imported: SpreadsheetImport, *, expected_revision: int
) -> SpreadsheetImport:
    """Checks every row it imports, as it would be written, a chunk at a time (a task's
    work): what it imports at ``expected_revision``, as the person saw it. Raises
    :class:`Stale` if it's moved on since, or :class:`Refused` while it can't be."""
    from iki_server.spreadsheets import tasks

    _may_write(user, imported.project, imported.type)
    with transaction.atomic():
        imported = _locked(imported, expected_revision)
        if imported.state not in (
            ImportState.MAPPING,
            ImportState.PREVIEWED,
            ImportState.PREVIEWING,
        ):
            raise Refused(_why_not_changed(imported))
        scan_state.require_usable(imported.file.blob)
        if not any(c.get("field") for c in imported.columns):
            raise Refused("Map a column to a field first.", field="columns")
        _reset(imported)
        imported.round += 1
        imported.revision += 1
        imported.state = ImportState.PREVIEWING
        imported.phase = PreviewPhase.READING
        imported.progress_message = "Reading its rows."
        imported.schema = imports._schema()
        imported.failures = 0
        imported.save()
        tasks.preview_import.enqueue(str(imported.pk), imported.round)
    return imported


def step(import_id: str, round_: int) -> str:
    """Works on a preview for a while, then hands on to another task if it isn't done.
    Only one works on a preview at a time."""
    from iki_server.spreadsheets import tasks
    from iki_server.tasks.backend import Retry

    try:
        done = _advance(uuid.UUID(import_id), round_)
    except Exception as error:
        if _stumbled(uuid.UUID(import_id), round_, error):
            return "gave up"
        raise Retry(f"The preview failed for now: {error}") from error
    if done:
        return "ended"
    with transaction.atomic():
        tasks.preview_import.enqueue(import_id, round_)
    return "continues"


def _stumbled(import_id: uuid.UUID, round_: int, error: Exception) -> bool:
    logger.exception("spreadsheet_preview_failed", import_id=str(import_id))
    with transaction.atomic():
        found = SpreadsheetImport.objects.select_for_update().filter(pk=import_id).first()
        if found is None or found.round != round_ or found.state != ImportState.PREVIEWING:
            return True
        found.failures += 1
        found.updated_at = timezone.now()
        found.save(update_fields=["failures", "updated_at"])
        if found.failures < MAX_FAILURES:
            return False
        found.state = ImportState.MAPPING
        found.phase = ""
        found.error = f"Iki couldn't check its rows: {str(error)[:300]}"
        found.save(update_fields=["state", "phase", "error"])
        return True


def recover() -> int:
    """Gives previews and commits whose runner went away another step. The task runner's
    housekeeping calls it. Returns how many it moved on."""
    from iki_server.spreadsheets import tasks

    moved = 0
    with transaction.atomic():
        stalled = SpreadsheetImport.objects.select_for_update(skip_locked=True).filter(
            state__in=[ImportState.READING, ImportState.PREVIEWING, ImportState.COMMITTING],
            updated_at__lt=timezone.now() - STALLED_AFTER,
        )[:100]
        for found in stalled:
            found.updated_at = timezone.now()
            found.save(update_fields=["updated_at"])
            if found.state == ImportState.READING:
                tasks.read_workbook.enqueue(str(found.pk))
            elif found.state == ImportState.PREVIEWING:
                tasks.preview_import.enqueue(str(found.pk), found.round)
            else:
                tasks.commit_import.enqueue(str(found.pk))
            moved += 1
    return moved


def expire() -> int:
    """Discards imports nobody committed and nobody has touched for a fortnight, so their
    rows go, and clears committed imports' rows a fortnight on. Maintenance runs it.
    Returns how many it discarded."""
    cutoff = timezone.now() - ABANDONED_AFTER
    stale = list(_abandoned(cutoff).values_list("pk", flat=True)[:500])
    # A committed import's rows say what each became, and where its run failed, if it
    # did: kept a fortnight after it was committed.
    done = SpreadsheetImport.objects.filter(
        state=ImportState.COMMITTED,
        rows_cleared=False,
        committed_at__lt=timezone.now() - ABANDONED_AFTER,
    )[:500]
    from iki_server.spreadsheets import tasks

    for kept in done:
        with transaction.atomic():
            tasks.clear_rows.enqueue(str(kept.pk), kept.round)
            SpreadsheetImport.objects.filter(pk=kept.pk).update(rows_cleared=True)
    count = 0
    for pk in stale:
        with transaction.atomic():
            # Abandoned still, under its lock: another session may have changed, previewed
            # or committed it since it was listed, and then it's theirs, not maintenance's.
            found = _abandoned(cutoff).select_for_update(skip_locked=True).filter(pk=pk).first()
            if found is not None:
                _discarded(found)
                count += 1
    return count


def _abandoned(cutoff: datetime) -> QuerySet[SpreadsheetImport]:
    """Imports nobody committed, and nobody has touched since ``cutoff``."""
    return SpreadsheetImport.objects.filter(
        state__in=[
            ImportState.READING,
            ImportState.MAPPING,
            ImportState.PREVIEWED,
            ImportState.FAILED,
        ],
        updated_at__lt=cutoff,
    )


def _advance(import_id: uuid.UUID, round_: int) -> bool:
    deadline = time.monotonic() + STEP_FOR
    while True:
        with transaction.atomic():
            found = (
                SpreadsheetImport.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("project", "requested_by", "file__blob")
                .filter(pk=import_id)
                .first()
            )
            if found is None or found.round != round_ or found.state != ImportState.PREVIEWING:
                return True
            if found.phase == PreviewPhase.READING:
                _read_rows(found)
            elif found.phase == PreviewPhase.CHECKING:
                _check_chunk(found)
            elif found.phase == PreviewPhase.RESOLVING:
                _resolve(found)
            else:
                return True
            found.failures = 0
            found.updated_at = timezone.now()
            found.save()
            if found.state != ImportState.PREVIEWING:
                return True
        if time.monotonic() > deadline:
            return False


@contextlib.contextmanager
def _workbook(file: File) -> Iterator[Workbook]:
    """The file, downloaded to a temporary file, opened as a workbook."""
    scan_state.require_usable(file.blob)
    with tempfile.TemporaryDirectory(prefix="iki-sheet-") as directory:
        path = Path(directory) / "workbook"
        body = storage.open_object(file.blob.storage_uri, file.blob.object_version)
        size = 0
        try:
            with path.open("wb") as out:
                while block := body.read(1024 * 1024):
                    size += len(block)
                    if size > files_limit():
                        raise Unreadable(
                            f"It's over {files_limit() // (1024 * 1024)} MiB: split it into "
                            "smaller workbooks."
                        )
                    out.write(block)
        finally:
            body.close()
        book = open_workbook(path, file.name)
        try:
            yield book
        finally:
            book.close()


def files_limit() -> int:
    from iki_server.spreadsheets.workbooks import MAX_SIZE

    return MAX_SIZE


# The rows as read: one JSON object a line, in row order, each the create it would be,
# with its row and the problems its cells have. Kept in storage, not as a file: only the
# committed rows become one.


def _read_rows(imported: SpreadsheetImport) -> None:
    """Reads the sheet's rows, as the import's columns read them, into its rows to check."""
    form = shape(imported.type)
    fields = form.fields()
    locale = Locale.of(imported.locale)
    columns = [
        c.under(locale) for c in mapping.columns_from_json(imported.columns) if c.field in fields
    ]
    headers = headers_of(imported)
    skip = [(int(a), int(b)) for a, b in imported.skip]
    counts = {"blank": 0, "left_out": 0}
    origin = {
        "source": SOURCES.get(imported.kind, "Spreadsheet"),
        "file": str(imported.file_id),
    }
    sheet_name = imported.sheet if imported.kind == "xlsx" else ""
    resolver = Resolver(imported.requested_by, imported.project)
    digest = hashlib.sha256()
    try:
        with (
            _workbook(imported.file) as book,
            tempfile.NamedTemporaryFile("wb", prefix="iki-rows-", suffix=".ndjson") as out,
        ):
            block: list[_Row] = []
            lines = 0
            for number, row in enumerate(book.rows(imported.sheet), start=1):
                if number < imported.first_row:
                    continue
                if imported.last_row is not None and number > imported.last_row:
                    break
                if any(a <= number <= b for a, b in skip):
                    counts["left_out"] += 1
                    continue
                read = _read_row(number, row, columns, fields, headers)
                if read is None:
                    counts["blank"] += 1
                    continue
                block.append(read)
                if len(block) >= BLOCK_ROWS:
                    lines = _write_block(
                        out, digest, block, resolver, imported, origin, sheet_name, lines
                    )
                    block = []
            lines = _write_block(out, digest, block, resolver, imported, origin, sheet_name, lines)
            out.flush()
            size = Path(out.name).stat().st_size
            if size:
                storage.upload_file(
                    Path(out.name),
                    storage.spreadsheet_key(imported.pk, imported.round),
                    "application/x-ndjson",
                )
    except (Unreadable, Problem) as error:
        imported.state = ImportState.MAPPING
        imported.phase = ""
        imported.error = str(error)
        return
    imported.error = ""
    imported.size = size
    imported.offset = imported.lines = 0
    imported.counts = {**counts, "rows": lines}
    imported.phase = PreviewPhase.CHECKING if lines else PreviewPhase.RESOLVING
    imported.progress = 0.2
    imported.progress_message = f"Read {lines:,} rows: checking them."


@dataclass
class _Row:
    number: int
    values: dict[str, Any]
    problems: list[dict[str, Any]]


def _read_row(
    number: int,
    row: Sequence[Any],
    columns: Sequence[Column],
    fields: Mapping[str, Field],
    headers: Sequence[str],
) -> _Row | None:
    values: dict[str, Any] = {}
    problems: list[dict[str, Any]] = []
    blank = True
    for column in columns:
        assert column.field is not None  # noqa: S101 - only mapped ones
        cell = row[column.index] if column.index < len(row) else None
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            continue
        blank = False
        f = fields[column.field]
        try:
            value = cells.convert(f, column, cell)
        except ValueError as error:
            shown = text_of(cell)
            shown = shown if len(shown) <= 60 else shown[:57] + "…"
            problem = {
                "column": column.index,
                "field": column.field,
                "detail": f"“{shown}” {error}",
            }
            if isinstance(error, cells.Ambiguous):
                problem["kind"] = "ambiguous"
            problems.append(problem)
            continue
        if value is not None:
            values[column.field] = value
    if blank:
        return None
    return _Row(number, values, problems)


def _write_block(
    out: Any,  # noqa: ANN401 - a binary file
    digest: Any,  # noqa: ANN401 - a hash
    block: list[_Row],
    resolver: Resolver,
    imported: SpreadsheetImport,
    origin: dict[str, str],
    sheet_name: str,
    lines: int,
) -> int:
    """Finds the records the block's references name, then writes its rows."""
    if not block:
        return lines
    fields = shape(imported.type).fields()
    columns = {c.field: c.index for c in mapping.columns_from_json(imported.columns) if c.field}
    wanted: dict[str, set[str]] = {}
    for read in block:
        for name, value in read.values.items():
            for found in value if isinstance(value, list) else [value]:
                if isinstance(found, Reference):
                    wanted.setdefault(name, set()).add(found.text)
    named = {name: resolver.find(fields[name], texts) for name, texts in wanted.items()}
    for read in block:
        for name, value in list(read.values.items()):
            if name not in named:
                continue
            try:
                read.values[name] = resolved(value, named[name])
            except ValueError as error:
                del read.values[name]
                read.problems.append(
                    {"column": columns.get(name), "field": name, "detail": str(error)}
                )
        lines += 1
        where = f"{sheet_name}, row {read.number}" if sheet_name else f"row {read.number}"
        line = {
            "row": read.number,
            "type": imported.type,
            "values": read.values,
            "origin": {**origin, "id": where[:255]},
            "problems": read.problems,
        }
        data = json.dumps(line, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        out.write(data)
        digest.update(data)
    return lines


def resolved(value: Any, found: Mapping[str, str | uuid.UUID]) -> Any:  # noqa: ANN401 - JSON
    if isinstance(value, list):
        out = []
        for number, item in enumerate(value, start=1):
            try:
                out.append(resolved(item, found))
            except ValueError as error:
                raise ValueError(f"Its item {number}: {error}") from None
        return out
    if not isinstance(value, Reference):
        return value
    target = found.get(value.text)
    if isinstance(target, uuid.UUID):
        return str(target)
    raise ValueError(f"“{value.text}” {target or 'names no record here.'}")


class Resolver:
    """Finds the records a sheet's references name, in the import's project: by ID, by
    HRID, or by name, a block of rows' at once."""

    def __init__(self, user: User, project: Project) -> None:
        self.user = user
        self.project = project
        self._usable = access.shareable_into(user, project)

    def find(self, f: Field, texts: set[str]) -> dict[str, str | uuid.UUID]:
        """What each text names: a record's ID, or why it names none."""
        target = f.items if f.type == "list" and f.items is not None else f
        if len(target.to) == 1 and lists.is_list(target.to[0]):
            return self._options(target.to[0], texts)
        types = referable(target)
        out: dict[str, str | uuid.UUID] = {}
        ids = {t: uuid.UUID(t) for t in texts if cells.is_id(t)}
        if ids:
            found = set(
                self._usable.filter(pk__in=ids.values(), type__in=types).values_list(
                    "pk", flat=True
                )
            )
            for text, pk in ids.items():
                out[text] = pk if pk in found else "names no record you can use here."
        rest = {t for t in texts if t not in out}
        if not rest:
            return out
        here = self._usable.filter(type__in=types)
        by_hrid: dict[str, list[tuple[uuid.UUID, bool]]] = {}
        for pk, hrid, owner in here.filter(hrid__in={t.upper() for t in rest}).values_list(
            "pk", "hrid", "owner_project_id"
        ):
            by_hrid.setdefault(hrid, []).append((pk, owner == self.project.pk))
        for text in list(rest):
            records = by_hrid.get(text.upper())
            if records:
                # A record of an entry has the entry's HRID: this project's own, if it has one.
                out[text] = sorted(records, key=lambda r: not r[1])[0][0]
                rest.discard(text)
        if rest:
            self._by_name(target, types, rest, out)
        label = _one_of(target.to)
        for text in rest - out.keys():
            out[text] = f"is no {label} in {self.project.name}, by its ID, HRID or name."
        return out

    def _options(self, type_name: str, texts: set[str]) -> dict[str, str | uuid.UUID]:
        """A lab list's options (ADR 0270), in the project's registry: by its code, as
        exports give it; by its code or its label in any case; by HRID or by ID. A text
        that names more than one in another case names none, and says which it could be.
        An archived one is found, and refused when it's saved unless the record names it
        already."""
        every = lists.options(self.project.registry_id, type_name, archived=True)
        by_code = {o.code: o.entry.pk for o in every}
        # What each name, in any case, could be: the option, and whether by code or label.
        by_name: dict[str, dict[uuid.UUID, tuple[str, str]]] = {}
        for o in every:
            by_name.setdefault(o.label.casefold(), {}).setdefault(o.entry.pk, (o.code, "label"))
            by_name.setdefault(o.code.casefold(), {}).setdefault(o.entry.pk, (o.code, "code"))
        by_hrid = {o.entry.hrid: o.entry.pk for o in every}
        by_id = {str(o.entry.pk): o.entry.pk for o in every}
        label = registry.types()[type_name].label_plural
        out: dict[str, str | uuid.UUID] = {}
        for text in texts:
            named = by_name.get(text.casefold(), {})
            if text not in by_code and len(named) > 1:
                said = [f"`{code}` by its {how}" for code, how in sorted(named.values())]
                which = ", ".join(said[:-1]) + " and " + said[-1]
                out[text] = (
                    f"names more than one option of {label}: {which}. Give the one you mean "
                    "by its code, exactly."
                )
                continue
            found = (
                by_code.get(text)
                or next(iter(named), None)
                or by_hrid.get(text.upper())
                or by_id.get(text.lower())
            )
            out[text] = (
                found
                if found is not None
                else f"is no option of {label}: give its code or its label."
            )
        return out

    def _by_name(
        self, f: Field, types: set[str], texts: set[str], out: dict[str, str | uuid.UUID]
    ) -> None:
        """Records by their names, as the sheet spells them, in any case. Several of one
        entry are one (ADR 0115); of several, the project's own is meant, if it has one."""
        folded: dict[str, list[str]] = {}
        for text in texts:
            folded.setdefault(text.casefold(), []).append(text)
        matches: dict[str, dict[uuid.UUID, _Named]] = {}
        for type_name in f.to:
            if type_name not in registry.types() or "name" not in shape(type_name).fields():
                continue
            model = shape(type_name).model
            found = (
                model._default_manager.filter(pk__in=self._usable.values("pk"), type__in=types)
                .alias(folded_name=Lower("name"))
                .filter(folded_name__in=list(folded))
                .values_list("pk", "hrid", "name", "entry_id", "owner_project_id")
            )
            for pk, hrid, name, entry, owner in found:
                key = str(name).casefold()
                if key in folded:
                    matches.setdefault(key, {})[pk] = _Named(
                        pk, hrid, cast("uuid.UUID | None", entry) or pk, owner == self.project.pk
                    )
        for key, spelled in folded.items():
            records = list(matches.get(key, {}).values())
            if not records:
                continue
            meant: list[_Named] = records
            if len({r.entry for r in meant}) > 1:
                meant = [r for r in records if r.here]
            if meant and len({r.entry for r in meant}) == 1:
                chosen = sorted(meant, key=lambda r: (not r.here, r.hrid))[0].pk
                for text in spelled:
                    out[text] = chosen
                continue
            hrids = ", ".join(sorted(r.hrid for r in records)[:5])
            for text in spelled:
                out[text] = f"names {len(records)} records here ({hrids}): give its HRID instead."


@dataclass(frozen=True)
class _Named:
    pk: uuid.UUID
    hrid: str
    #: Its entry, or itself: records of one entry are one by name.
    entry: uuid.UUID
    #: The import's project's own.
    here: bool


def _one_of(targets: Sequence[str]) -> str:
    labels = []
    for t in targets:
        found = registry.types().get(t)
        labels.append((found.label or found.name).lower() if found else t)
    return " or ".join(labels) or "record"


def _check_chunk(imported: SpreadsheetImport) -> None:
    """Checks a chunk of its rows as creates of its type, as a bulk import checks a creates
    file's lines (ADR 0056), and keeps what each would be."""
    user, project = imported.requested_by, imported.project
    reader = imports.Reader(
        storage.uri(storage.spreadsheet_key(imported.pk, imported.round)),
        "",
        imported.size,
        imported.offset,
    )
    parsed: list[dict[str, Any]] = []
    number = imported.lines
    try:
        for raw in reader.lines():
            if raw is None:
                break
            number += 1
            parsed.append(json.loads(raw))
            if len(parsed) >= CHUNK_ROWS:
                break
    finally:
        reader.close()
    first_line = imported.lines + 1
    rows = _checked(user, project, imported, parsed, first_line)
    PreviewRow.objects.bulk_create(rows, batch_size=1000)
    imported.offset = reader.offset
    imported.lines = number
    done = imported.offset >= imported.size
    if done:
        imported.phase = PreviewPhase.RESOLVING
        imported.progress_message = "Finding duplicates."
    else:
        imported.progress_message = (
            f"Checked {imported.lines:,} of {imported.counts.get('rows', 0):,} rows."
        )
    imported.progress = 0.2 + 0.75 * (imported.offset / imported.size if imported.size else 1)


def _checked(
    user: User,
    project: Project,
    imported: SpreadsheetImport,
    parsed: list[dict[str, Any]],
    first_line: int,
) -> list[PreviewRow]:
    form = shape(imported.type)
    columns = {c.field: c.index for c in mapping.columns_from_json(imported.columns) if c.field}
    refs = imports.refs_for(user, project)
    imports.look_up_refs(refs, parsed)
    prepared: list[tuple[int, dict[str, Any]]] = []
    problems: dict[int, list[dict[str, Any]]] = {}
    for index, line in enumerate(parsed):
        found = list(line.get("problems") or [])
        failed = {p["field"] for p in found if p.get("field")}
        try:
            _, given, _ = entities.prepare(
                user, imported.type, project, line["values"], refs, by_run=True, check_rules=False
            )
        except Invalid as error:
            for name, messages in error.errors.items():
                field_name = name.split("/", 1)[0]
                if field_name in failed:
                    continue  # its cell says what's wrong with it
                found.append(_problem(form, columns, field_name, " ".join(messages)))
        else:
            prepared.append((index, given))
        problems[index] = found
    held: dict[int, list[dict[str, str]]] = {}
    for at, broken in imports.breaking([(imported.type, g) for _, g in prepared]).items():
        index = prepared[at][0]
        for b in broken:
            if b.held:
                held.setdefault(index, []).append({"rule": b.rule.label, "message": b.message})
            else:
                name = b.fields[0] if b.fields else ""
                problems[index].append(_problem(form, columns, name, b.message))
    given_of = dict(prepared)
    _taken(project, form, columns, given_of, problems)
    out = []
    for index, line in enumerate(parsed):
        maybe = given_of.get(index)
        found = problems[index][:MAX_ROW_PROBLEMS]
        digest = None
        uniques = None
        if maybe is not None:
            try:
                digest = hash_of(maybe, form.entity_type)
            except ValueError:
                digest = None
            uniques = _uniques(form, maybe) or None
        out.append(
            PreviewRow(
                spreadsheet_import_id=imported.pk,
                round=imported.round,
                row=int(line["row"]),
                line=first_line + index,
                outcome=RowOutcome.PROBLEM if found else RowOutcome.CREATE,
                identity_hash=digest,
                uniques=uniques,
                problems=found or None,
                held=held.get(index),
            )
        )
    return out


def _problem(form: Any, columns: Mapping[str, int], name: str, detail: str) -> dict[str, Any]:  # noqa: ANN401 - a Shape
    f = form.fields().get(name)
    label = (f.label or name.replace("_", " ").capitalize()) if f is not None else ""
    column = columns.get(name)
    if f is not None and column is None:
        detail = f"{label} {detail} Map a column to it, or give it in each row."
    elif f is not None:
        detail = f"{label} {detail}"
    return {"column": column, "field": name or None, "detail": detail}


def _uniques(form: Any, given: Mapping[str, Any]) -> dict[str, str]:  # noqa: ANN401 - a Shape
    out = {}
    for part in form.parts:
        for name, f in part.fields.items():
            if f.unique and not f.removed and given.get(name) is not None:
                out[unique.key(part.model, name)] = str(given[name])
    return out


def _taken(
    project: Project,
    form: Any,  # noqa: ANN401 - a Shape
    columns: Mapping[str, int],
    given_of: Mapping[int, dict[str, Any]],
    problems: dict[int, list[dict[str, Any]]],
) -> None:
    """Rows whose unique values a record in the project has, or (for a registry fact) an
    entry in its registry: a chunk's in a query a field (ADR 0082)."""
    for part in form.parts:
        for name, f in part.fields.items():
            if not f.unique or f.removed:
                continue
            wanted = {i: str(g[name]) for i, g in given_of.items() if g.get(name) is not None}
            if not wanted:
                continue
            field_key = unique.key(part.model, name)
            rows = UniqueValue.objects.filter(key=field_key, value__in=set(wanted.values()))
            rows = (
                rows.filter(registry_id=registry_of(project))
                if part.entry
                else rows.filter(project=project)
            )
            holders = dict(rows.values_list("value", "entity__hrid"))
            for index, value in wanted.items():
                if value in holders:
                    whose = holders[value] if not part.entry else "another entry"
                    problems[index].append(
                        _problem(
                            form, columns, name, f"“{value}” is {whose}’s already: no two share it."
                        )
                    )


def _resolve(imported: SpreadsheetImport) -> None:
    """What each row would be (ADR 0115): rows with an identity are found in the registry,
    and the project's record of each entry, and a row with an earlier one's identity is
    that one; rows with a unique value an earlier row has can't be written. Then the
    preview's counts."""
    params: dict[str, Any] = {
        "import": imported.pk,
        "round": imported.round,
        "registry": registry_of(imported.project),
        "project": imported.project_id,
        "type": imported.type,
    }
    rows = "iki_spreadsheet_row"
    mine = "spreadsheet_import_id = %(import)s AND round = %(round)s"
    mine_r = "r.spreadsheet_import_id = %(import)s AND r.round = %(round)s"
    form = shape(imported.type)
    columns = {c.field: c.index for c in mapping.columns_from_json(imported.columns) if c.field}
    fields_by_key = {
        unique.key(p.model, n): n for p in form.parts for n, f in p.fields.items() if f.unique
    }
    with connection.cursor() as cursor:
        # Two rows with one unique value: each record has its own (ADR 0082).
        cursor.execute(
            f"SELECT u.key, u.value, array_agg(r.row ORDER BY r.row) FROM {rows} r"  # noqa: S608 - ours; values are parameters
            " CROSS JOIN LATERAL jsonb_each_text(r.uniques) u"
            f" WHERE {mine_r}"
            " AND r.uniques IS NOT NULL AND r.outcome <> 'problem'"
            " GROUP BY u.key, u.value HAVING count(*) > 1 LIMIT 10000",
            params,
        )
        clashes: dict[int, list[dict[str, Any]]] = {}
        for field_key, value, numbers in cursor.fetchall():
            name = fields_by_key.get(field_key, "")
            for later in numbers[1:]:
                clashes.setdefault(later, []).append(
                    _problem(
                        form,
                        columns,
                        name,
                        f"“{value}” is row {numbers[0]}’s too: no two share it.",
                    )
                )
        _add_problems(cursor, imported, clashes)
        if form.has_entries:
            # Each row's entry in the registry, if its identity is registered: the project's
            # record of it, if it has one, or a new record of it.
            cursor.execute(
                f"UPDATE {rows} r SET"  # noqa: S608 - ours; values are parameters
                " outcome = CASE WHEN x.superseded_by_id IS NOT NULL OR x.type <> %(type)s"
                "  THEN 'problem' WHEN h.id IS NOT NULL THEN 'reuse' ELSE 'add' END,"
                " match_id = COALESCE(h.id, x.id), match_hrid = x.hrid,"
                " problems = CASE WHEN x.superseded_by_id IS NOT NULL THEN jsonb_build_array("
                "   jsonb_build_object('column', NULL, 'field', NULL, 'detail',"
                "   x.hrid || ' was superseded by ' || s.hrid || ': register that instead.'))"
                "  WHEN x.type <> %(type)s THEN jsonb_build_array(jsonb_build_object('column',"
                "   NULL, 'field', NULL, 'detail', 'It''s registered already, as a ' || x.type"
                "   || ' (' || x.hrid || '): import it as one.')) ELSE r.problems END"
                " FROM iki_entity x"
                " LEFT JOIN iki_entity s ON s.id = x.superseded_by_id"
                " LEFT JOIN LATERAL (SELECT e.id FROM iki_entity e WHERE e.entry_id = x.id"
                "  AND e.owner_project_id = %(project)s ORDER BY e.created_at, e.id LIMIT 1) h"
                "  ON true"
                f" WHERE {mine_r}"
                " AND r.outcome = 'create' AND r.identity_hash IS NOT NULL"
                " AND x.registry_id = %(registry)s AND x.identity_hash = r.identity_hash",
                params,
            )
            # A row with an earlier one's identity is the same record: the first is written.
            cursor.execute(
                f"UPDATE {rows} r SET outcome = 'same', same_as = f.row, match_id = NULL,"  # noqa: S608 - ours; values are parameters
                " match_hrid = f.match_hrid FROM ("
                f" SELECT DISTINCT ON (identity_hash) identity_hash, row, match_hrid FROM {rows}"
                f" WHERE {mine} AND identity_hash IS NOT NULL AND outcome <> 'problem'"
                " ORDER BY identity_hash, row) f"
                f" WHERE {mine_r}"
                " AND r.identity_hash = f.identity_hash AND r.row <> f.row"
                " AND r.outcome <> 'problem'",
                params,
            )
            cursor.execute(
                f"UPDATE {rows} SET outcome = 'register' WHERE {mine} AND outcome = 'create'",  # noqa: S608 - ours; values are parameters
                params,
            )
        cursor.execute(
            f"SELECT outcome, count(*), count(*) FILTER (WHERE held IS NOT NULL) FROM {rows}"  # noqa: S608 - ours; values are parameters
            f" WHERE {mine} GROUP BY outcome",
            params,
        )
        found = {outcome: (n, held) for outcome, n, held in cursor.fetchall()}
        cursor.execute(
            f"SELECT count(*) FROM {rows} WHERE {mine} AND outcome = 'problem'"  # noqa: S608 - ours; values are parameters
            """ AND problems @> '[{"kind": "ambiguous"}]'""",
            params,
        )
        (ambiguous,) = cursor.fetchone()
    counts = dict(imported.counts)
    for outcome in RowOutcome.values:
        counts[outcome] = found.get(outcome, (0, 0))[0]
    counts["held"] = sum(held for o, (_, held) in found.items() if o in WRITTEN)
    #: Rows with cells that read two ways, until the person says how the sheet's written.
    counts["ambiguous"] = ambiguous
    imported.counts = counts
    imported.phase = PreviewPhase.DONE
    imported.state = ImportState.PREVIEWED
    imported.progress = 1.0
    imported.progress_message = "Checked."


def _add_problems(
    cursor: Any,  # noqa: ANN401 - a cursor
    imported: SpreadsheetImport,
    found: Mapping[int, list[dict[str, Any]]],
) -> None:
    if not found:
        return
    numbers = list(found)
    cursor.execute(
        "UPDATE iki_spreadsheet_row r SET outcome = 'problem',"
        " problems = COALESCE(r.problems, '[]'::jsonb) || m.problems::jsonb"
        " FROM unnest(%(rows)s::bigint[], %(problems)s::text[]) AS m(row, problems)"
        " WHERE r.spreadsheet_import_id = %(import)s AND r.round = %(round)s AND r.row = m.row",
        {
            "rows": numbers,
            "problems": [json.dumps(found[n]) for n in numbers],
            "import": imported.pk,
            "round": imported.round,
        },
    )


def clear_rows(import_id: str, round_: int) -> str:
    """Deletes a round's rows, a batch at a time, and its rows as read. Enqueues itself
    again until they're gone."""
    from iki_server.spreadsheets import tasks

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM iki_spreadsheet_row WHERE (spreadsheet_import_id, round, row) IN ("
            " SELECT spreadsheet_import_id, round, row FROM iki_spreadsheet_row"
            " WHERE spreadsheet_import_id = %s AND round = %s LIMIT 50000)",
            [import_id, round_],
        )
        more = cursor.rowcount == 50000
        if more:
            tasks.clear_rows.enqueue(import_id, round_)
    if not more:
        with contextlib.suppress(Exception):
            storage.delete_for_good(storage.spreadsheet_key(import_id, round_))
    return "continues" if more else "cleared"


# --- Committing --------------------------------------------------------------------------------


def _applied_mapping(imported: SpreadsheetImport) -> dict[str, Any]:
    return {
        "template": imported.mapping_source,
        "header_row": imported.header_row,
        "locale": Locale.of(imported.locale).as_json(),
        "columns": mapping.saved_columns(
            mapping.columns_from_json(imported.columns), headers_of(imported), keep_index=True
        ),
    }


def commit(
    user: User,
    imported: SpreadsheetImport,
    *,
    expected_revision: int,
    skip_problems: bool = False,
    reason: str = "",
) -> SpreadsheetImport:
    """Imports what the preview checked, as a run (a task starts it): the preview of
    ``expected_revision``, the one the person saw. Rows with problems stop it, unless
    ``skip_problems`` leaves them out. Raises :class:`Stale` or :class:`Refused`."""
    from iki_server.spreadsheets import tasks

    _may_write(user, imported.project, imported.type)
    with transaction.atomic():
        imported = _locked(imported, expected_revision)
        if imported.state != ImportState.PREVIEWED:
            if imported.state in (ImportState.MAPPING, ImportState.PREVIEWING):
                raise Refused("Preview it first: Iki checks every row before it imports them.")
            raise Refused(_why_not_changed(imported))
        scan_state.require_usable(imported.file.blob)
        if imported.schema != imports._schema():
            raise Refused("A plugin changed since it was previewed: preview it again.")
        counts = imported.counts
        problems = int(counts.get(RowOutcome.PROBLEM, 0))
        if problems and not skip_problems:
            raise Refused(
                f"{problems:,} {'row has' if problems == 1 else 'rows have'} problems: fix "
                "them in the workbook and preview it again, or leave them out.",
                field="skip_problems",
            )
        if not sum(int(counts.get(o, 0)) for o in (*WRITTEN, RowOutcome.REUSE, RowOutcome.SAME)):
            raise Refused("It has no rows to import.")
        imported.state = ImportState.COMMITTING
        imported.applied_mapping = _applied_mapping(imported)
        imported.skip_problems = skip_problems and bool(problems)
        imported.reason = reason.strip()[:2000]
        imported.via = audit.current_via()
        imported.client = audit.current_oauth_client()
        imported.progress, imported.progress_message = 0.0, "Starting the import."
        imported.updated_at = timezone.now()
        imported.save()
        tasks.commit_import.enqueue(str(imported.pk))
    return imported


def _blocked_commit(imported: SpreadsheetImport, problem: Problem) -> str:
    from iki_server.spreadsheets import tasks

    active = SpreadsheetImport.objects.filter(pk=imported.pk, state=ImportState.COMMITTING)
    if problem.code == "file_scanning":
        with transaction.atomic():
            now = timezone.now()
            if active.update(progress_message=problem.detail, updated_at=now):
                tasks.commit_import.using(run_after=now + timedelta(seconds=30)).enqueue(
                    str(imported.pk)
                )
        return "scanning"
    active.update(state=ImportState.FAILED, error=problem.detail)
    return "refused"


def committing(import_id: str) -> str:
    """Writes the rows to import as the run's creates file, then starts its run."""
    found = (
        SpreadsheetImport.objects.select_related("project", "requested_by", "file")
        .filter(pk=import_id)
        .first()
    )
    if found is None or found.state != ImportState.COMMITTING:
        return "done"
    try:
        scan_state.require_usable(found.file.blob)
    except Problem as error:
        return _blocked_commit(found, error)
    if found.creates_id is None:
        file = _creates_file(found)
        with transaction.atomic():
            locked = SpreadsheetImport.objects.select_for_update().get(pk=found.pk)
            if locked.state != ImportState.COMMITTING or locked.creates_id is not None:
                return "done"
            locked.creates = file
            locked.save(update_fields=["creates"])
        found.creates = file
    with transaction.atomic():
        locked = (
            SpreadsheetImport.objects.select_for_update(of=("self",))
            .select_related("project", "requested_by", "creates")
            .get(pk=found.pk)
        )
        if locked.state != ImportState.COMMITTING or locked.creates is None:
            return "done"
        user, project = locked.requested_by, locked.project
        if (
            not user.is_active
            or not permissions.can(user, access.edit_action(locked.type), project)
            or project.is_archived
        ):
            locked.state = ImportState.FAILED
            locked.error = f"{user.name} can no longer add records to {project.name}."
            locked.save(update_fields=["state", "error"])
            return "refused"
        try:
            scan_state.require_usable(locked.creates.blob)
        except Problem as error:
            return _blocked_commit(locked, error)
        # A task queued before upgrades may not have a snapshot yet.
        pinned = locked.applied_mapping or _applied_mapping(locked)
        params = {
            "workbook": str(locked.file_id),
            "sheet": locked.sheet,
            "header_row": pinned["header_row"],
            "first_row": locked.first_row,
            "last_row": locked.last_row,
            "skip": locked.skip,
            "locale": pinned["locale"],
            "columns": [
                {key: value for key, value in column.items() if key != "index"}
                for column in pinned["columns"]
            ],
            "skip_problems": locked.skip_problems,
        }
        with audit.using(audit.Context(via=locked.via or "api", oauth_client=locked.client)):
            run = imports.launch(
                user,
                project,
                action_for(locked.type),
                params,
                locked.creates,
                reason=locked.reason,
                via=locked.via or "api",
                client=locked.client,
                agent=locked.agent,
            )
            locked.run = run
            locked.applied_mapping = pinned
            locked.state = ImportState.COMMITTED
            locked.committed_at = timezone.now()
            locked.updated_at = timezone.now()
            locked.save()
            audit.record(
                "spreadsheet.imported",
                actor=user,
                agent=locked.agent,
                target=run,
                project=project,
                data={
                    "file": str(locked.file_id),
                    "sheet": locked.sheet,
                    "type": locked.type,
                    "locale": Locale.of(locked.locale).as_json(),
                    "rows": int(locked.counts.get("rows", 0)),
                    "left_out": int(locked.counts.get(RowOutcome.PROBLEM, 0))
                    if locked.skip_problems
                    else 0,
                    "run": run.hrid,
                },
            )
    return "started"


def _creates_file(imported: SpreadsheetImport) -> File:
    """The rows to import, one create a line, as a bulk import reads them: each line where
    the preview's was, a row with problems left blank, so a line's number is its row's."""
    left_out = set(
        PreviewRow.objects.filter(
            spreadsheet_import_id=imported.pk, round=imported.round, outcome=RowOutcome.PROBLEM
        ).values_list("line", flat=True)
    )
    reader = imports.Reader(
        storage.uri(storage.spreadsheet_key(imported.pk, imported.round)), "", imported.size, 0
    )
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile("wb", prefix="iki-creates-", suffix=".ndjson") as out:
        number = 0
        try:
            for raw in reader.lines():
                if raw is None:
                    break
                number += 1
                if number in left_out:
                    data = b"\n"
                else:
                    line = json.loads(raw)
                    data = (
                        json.dumps(
                            {k: line[k] for k in ("type", "values", "origin")},
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                        + b"\n"
                    )
                out.write(data)
                digest.update(data)
        finally:
            reader.close()
        out.flush()
        stem = imported.file.name.rsplit(".", 1)[0]
        where = f"{stem}, {imported.sheet}" if imported.kind == "xlsx" else stem
        return files.add_written(
            Path(out.name),
            name=f"{where} (rows imported).ndjson",
            media_type="application/x-ndjson",
            project=imported.project,
            user=imported.requested_by,
            agent=imported.agent,
            sha256=digest.hexdigest(),
        )


def discard(user: User, imported: SpreadsheetImport) -> None:
    """Before it's committed: its rows go, and it's kept as discarded."""
    with transaction.atomic():
        locked = SpreadsheetImport.objects.select_for_update().get(pk=imported.pk)
        if locked.state in (ImportState.COMMITTING, ImportState.COMMITTED):
            raise Refused("It's been imported: its run's records are the project's now.")
        _discarded(locked)


def _discarded(imported: SpreadsheetImport) -> None:
    from iki_server.spreadsheets import tasks

    if imported.state == ImportState.DISCARDED:
        return
    if imported.round:
        tasks.clear_rows.enqueue(str(imported.pk), imported.round)
    imported.state = ImportState.DISCARDED
    imported.updated_at = timezone.now()
    imported.save(update_fields=["state", "updated_at"])


def _fail(imported: SpreadsheetImport, why: str) -> None:
    SpreadsheetImport.objects.filter(pk=imported.pk, state=ImportState.READING).update(
        state=ImportState.FAILED, error=why[:2000], updated_at=timezone.now()
    )


# --- Reading -----------------------------------------------------------------------------------


def visible(user: User) -> QuerySet[SpreadsheetImport]:
    """The imports ``user`` may see: their own and committed imports, in projects they read."""
    readable = access.readable_projects(user).values("pk")
    from django.db.models import Q

    return SpreadsheetImport.objects.filter(
        Q(requested_by=user) | Q(state=ImportState.COMMITTED), project__in=readable
    )


def state_of(imported: SpreadsheetImport) -> str:
    """Where it is, for people: its own state until it's committed, then its run's."""
    if imported.state != ImportState.COMMITTED or imported.run is None:
        return str(imported.state)
    run: ActionRun = imported.run
    if run.status == RunStatus.SUCCEEDED:
        return "imported"
    if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
        return "importing"
    if run.status == RunStatus.CANCELLED:
        return "cancelled"
    return "failed"


def run_problems(imported: SpreadsheetImport) -> list[dict[str, Any]]:
    """Why its run didn't write it, each problem at its row and column where it says one:
    the run checked the rows again, as they were then."""
    run = imported.run
    if run is None or not run.error:
        return []
    errors = run.error.get("errors") or []
    lines: dict[int, list[tuple[str, str]]] = {}
    general = []
    clashes = _clashes(imported)
    for error in errors:
        pointer = str(error.get("pointer", ""))
        parts = pointer.removeprefix("#/").split("/")
        if len(parts) >= 2 and parts[0] == "creates_file" and parts[1].isdigit():
            field_name = parts[3] if len(parts) >= 4 and parts[2] == "values" else ""
            lines.setdefault(int(parts[1]), []).append((field_name, str(error.get("detail", ""))))
        elif not (clashes and pointer == "#/creates_file"):
            # A unique value written since: :func:`_clashes` says at which rows.
            general.append(
                {"row": None, "column": None, "field": None, "detail": str(error.get("detail", ""))}
            )
    rows = dict(
        PreviewRow.objects.filter(
            spreadsheet_import_id=imported.pk, round=imported.round, line__in=list(lines)
        ).values_list("line", "row")
    )
    columns = {c.field: c.index for c in mapping.columns_from_json(imported.columns) if c.field}
    form = shape(imported.type) if imported.type in registry.types() else None
    out = []
    for line, found in sorted(lines.items()):
        for name, detail in found:
            problem = (
                _problem(form, columns, name, detail)
                if form is not None
                else {"column": None, "field": name or None, "detail": detail}
            )
            out.append({"row": rows.get(line), **problem})
    return [*general, *out, *clashes]


def _clashes(imported: SpreadsheetImport) -> list[dict[str, Any]]:
    """Rows whose unique values a record has now that none did when they were previewed:
    the import's run found them as it wrote, and failed (ADR 0082)."""
    if imported.type not in registry.types():
        return []
    form = shape(imported.type)
    columns = {c.field: c.index for c in mapping.columns_from_json(imported.columns) if c.field}
    out = []
    for part in form.parts:
        for name, f in part.fields.items():
            if not f.unique or f.removed:
                continue
            field_key = unique.key(part.model, name)
            found = (
                PreviewRow.objects.filter(
                    spreadsheet_import_id=imported.pk,
                    round=imported.round,
                    outcome__in=WRITTEN,
                    uniques__has_key=field_key,
                )
                .order_by("row")
                .values_list("row", "uniques")[:10_000]
            )
            wanted = {str(u[field_key]): row for row, u in found if u is not None}
            if not wanted:
                continue
            taken = UniqueValue.objects.filter(key=field_key, value__in=list(wanted))
            taken = (
                taken.filter(registry_id=registry_of(imported.project))
                if part.entry
                else taken.filter(project=imported.project_id)
            )
            for value, hrid in taken.values_list("value", "entity__hrid"):
                out.append(
                    {
                        "row": wanted[value],
                        **_problem(
                            form,
                            columns,
                            name,
                            f"“{value}” is {hrid}’s now, written since the preview: no two "
                            "share it.",
                        ),
                    }
                )
    return sorted(out, key=lambda p: p["row"])


def outcome_rows(
    imported: SpreadsheetImport, outcomes: Iterable[str] | None = None
) -> QuerySet[PreviewRow]:
    if imported.state == ImportState.MAPPING:
        # Clearing the rows of the previous preview is a deferred task.
        return PreviewRow.objects.none()
    found = PreviewRow.objects.filter(spreadsheet_import_id=imported.pk, round=imported.round)
    if outcomes:
        found = found.filter(outcome__in=list(outcomes))
    return found

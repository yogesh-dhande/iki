"""Read a plate reader export in a named format, then join its matrix to an Iki plate layout.

Each format names the subset of an export it accepts and a version, recorded on the preview
and the committed read (data/reader.md). Parsing a format yields one labelled matrix of
readings, its readout and the export's own header. The join by well coordinate is the same
for every format: a reading joins the assignment of its labelled row and numbered column.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

# ANSI/SLAS 4-2004 (R2012), sections 4.1 and 4.2, well positions in microplates.
DIMENSIONS = {96: (8, 12), 384: (16, 24)}
MAX_MIB = 1  # AssayPlate.reader_file's upload limit.
WELL = re.compile(r"([A-P])0?([1-9][0-9]?)", re.ASCII)
RAW = re.compile(r"Raw Data\s*\((\d{1,4})\)", re.ASCII | re.IGNORECASE)
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
MATRIX_HEADING = ["Normalized activity (%)"]
# The MARS run header's own labels, kept verbatim. A date's order depends on the
# instrument PC's locale, so it isn't interpreted.
MARS_HEADER = {
    "User": "user",
    "Test name": "test_name",
    "Test run no.": "test_run",
    "Date": "date",
    "Time": "time",
    "ID1": "id1",
    "ID2": "id2",
    "ID3": "id3",
}

Rows = list[tuple[int, list[str]]]


@dataclass(frozen=True)
class Readout:
    """What a matrix measures. Absorbance is never read as activity, nor the reverse."""

    kind: str  # A WellMeasurement readout.
    channel: str
    unit: str  # AU or %.


@dataclass(frozen=True)
class Parsed:
    readout: Readout
    matrix: Rows
    acquisition: dict[str, str]


@dataclass(frozen=True)
class Format:
    id: str
    version: int
    label: str
    parse: Callable[[Rows, int], Parsed]

    @property
    def described(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "label": self.label}


def _assignment(entry: object, rows: int, columns: int) -> dict[str, Any]:
    """Normalize one explicit well assignment. Retain the record ID and measured dose."""
    if not isinstance(entry, dict):
        raise ValueError("Give a well assignment as an object.")
    match = WELL.fullmatch(str(entry.get("well", "")).strip().upper())
    if not match or ord(match[1]) - ord("A") >= rows or int(match[2]) > columns:
        raise ValueError(f"Well {entry.get('well')!r} is outside this {rows * columns}-well plate.")
    well = f"{match[1]}{int(match[2])}"
    sample, control = entry.get("sample"), entry.get("control")
    if bool(sample) == bool(control):
        raise ValueError(f"{well} needs one sample ID or one named control.")
    assignment: dict[str, Any] = {"well": well}
    if sample:
        try:
            assignment["sample"] = str(UUID(str(sample)))
        except ValueError as exc:
            raise ValueError(f"{well} needs a sample record UUID.") from exc
    else:
        if not isinstance(control, str) or not control.strip():
            raise ValueError(f"{well} needs a control name.")
        assignment["control"] = control.strip()
    replicate = entry.get("replicate")
    if type(replicate) is not int or replicate < 1:
        raise ValueError(f"{well} needs a positive integer replicate number.")
    assignment["replicate"] = replicate
    dose, unit = entry.get("concentration"), entry.get("concentration_unit")
    if dose is not None or unit is not None:
        if type(dose) not in (int, float) or dose < 0 or not math.isfinite(dose):
            raise ValueError(f"{well} needs a finite, nonnegative concentration.")
        if unit not in ("nM", "uM", "mM", "M"):
            raise ValueError(f"{well} needs a concentration unit (nM, uM, mM or M).")
        assignment.update(concentration=dose, concentration_unit=unit)
    return assignment


def _layout(
    entries: object, rows: int, columns: int, issues: list[str]
) -> dict[str, dict[str, Any]]:
    if not isinstance(entries, list) or not entries:
        issues.append("Assign wells to samples or controls in the plate layout.")
        return {}
    if len(entries) > rows * columns:
        issues.append(f"The layout has more than {rows * columns} well assignments.")
        return {}
    assignments = {}
    for index, entry in enumerate(entries, 1):
        try:
            assignment = _assignment(entry, rows, columns)
        except (ValueError, OverflowError) as exc:
            issues.append(f"Layout row {index}: {exc}")
            continue
        well = assignment["well"]
        if well in assignments:
            issues.append(f"Layout row {index}: repeated assignment for {well}.")
        else:
            assignments[well] = assignment
    return assignments


def _csv_rows(content: bytes) -> Rows:
    if len(content) > MAX_MIB * 1024**2:
        raise ValueError(f"The endpoint CSV exceeds {MAX_MIB} MiB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("cp1252")  # The legacy Windows MARS export.
        except UnicodeDecodeError as exc:
            raise ValueError("Choose a UTF-8 or Windows-1252 CSV export.") from exc
    if "\0" in text:
        raise ValueError("Choose a UTF-8 or Windows-1252 CSV export.")
    parsed = []
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    for row in reader:
        if reader.line_num > 128 or len(row) > 64:
            raise ValueError("Export one 96- or 384-well endpoint matrix per file.")
        cells = [cell.strip() for cell in row]
        # MARS pads even metadata with empty cells, sometimes with a trailing delimiter.
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            parsed.append((reader.line_num, cells))
    return parsed


def _numbered(parsed: Rows, index: int, rows: int, columns: int) -> Rows:
    """The matrix below a heading at ``index``: numbered columns, then lettered rows."""
    headings = ["", *(str(column) for column in range(1, columns + 1))]
    if index + 1 >= len(parsed) or parsed[index + 1][1] != headings:
        raise ValueError(
            f"The matrix must label columns 1 through {columns} in order, "
            f"once each, for a {rows * columns}-well plate."
        )
    return parsed[index + 2 :]


def _mars(parsed: Rows, rows: int, columns: int) -> Parsed:
    """BMG LABTECH MARS MicroPlate View CSV: one raw absorbance endpoint, displayed in OD."""
    if parsed and parsed[0][1] == MATRIX_HEADING:
        raise ValueError(
            "This file is an Iki plate matrix of normalized activity. "
            "Choose that reader file format on the plate."
        )
    if not parsed or not parsed[0][1][0].startswith("User:"):
        raise ValueError("Choose a BMG MARS MicroPlate View CSV with its export header.")
    starts = [i for i, (_, row) in enumerate(parsed) if row[0].lower().startswith("raw data")]
    if len(starts) != 1:
        raise ValueError("Export exactly one Raw Data absorbance endpoint matrix per file.")
    index = starts[0]
    if not any(row == ["Absorbance"] for _, row in parsed[:index]):
        raise ValueError("Select the raw absorbance endpoint in OD in MARS and export again.")
    title = parsed[index][1]
    wavelength = RAW.fullmatch(title[0])
    if len(title) != 1 or not wavelength or int(wavelength[1]) == 0:
        raise ValueError("The Raw Data heading must give one absorbance wavelength in nm.")
    acquisition: dict[str, str] = {}
    for _, row in parsed[:index]:
        for cell in row:
            label, colon, value = cell.partition(":")
            if colon and (key := MARS_HEADER.get(label.strip())) and value.strip():
                acquisition.setdefault(key, value.strip()[:200])
    readout = Readout("absorbance", f"Absorbance {int(wavelength[1])} nm", "AU")
    return Parsed(readout, _numbered(parsed, index, rows, columns), acquisition)


def _iki_matrix(parsed: Rows, rows: int, columns: int) -> Parsed:
    """Iki's plate matrix: its readout heading alone on line one, then the labelled matrix."""
    if not parsed or parsed[0][1] != MATRIX_HEADING:
        if parsed and parsed[0][1][0].startswith("User:"):
            raise ValueError(
                "This file is a BMG MARS export. Choose that reader file format on the plate."
            )
        raise ValueError("An Iki plate matrix starts with `Normalized activity (%)` on line one.")
    readout = Readout("normalized_activity", "Normalized activity", "%")
    return Parsed(readout, _numbered(parsed, 0, rows, columns), {})


FORMATS = {
    "bmg_mars_absorbance": Format(
        "bmg_mars_absorbance", 1, "BMG LABTECH MARS absorbance endpoint (OD)", _mars
    ),
    "iki_plate_matrix": Format(
        "iki_plate_matrix", 1, "Iki plate matrix of normalized activity (%)", _iki_matrix
    ),
}


def _join(
    readout: Readout,
    matrix: Rows,
    rows: int,
    columns: int,
    assignments: dict[str, dict[str, Any]],
    issues: list[str],
) -> list[dict[str, Any]]:
    """Join each measured cell to its well's assignment, whatever format it came from."""
    readings: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    measured: set[str] = set()
    unassigned: list[str] = []
    for source_row, cells in matrix:
        letter = cells[0]
        if len(letter) != 1 or not ("A" <= letter < chr(ord("A") + rows)):
            issues.append(f"File row {source_row}: expected a plate row letter, found {letter!r}.")
            continue
        if letter in seen_rows:
            issues.append(f"File row {source_row}: repeated plate row {letter}.")
            continue
        seen_rows.add(letter)
        if len(cells) > columns + 1:
            issues.append(f"File row {source_row}: measurements extend past {letter}{columns}.")
            continue
        for column, cell in enumerate(cells[1:], 1):
            if not cell:
                continue
            well = f"{letter}{column}"
            measured.add(well)
            if not NUMBER.fullmatch(cell) or not math.isfinite(value := float(cell)):
                kind = "OD" if readout.unit == "AU" else "activity"
                issues.append(f"File row {source_row}, {well}: expected a finite {kind} reading.")
                continue
            if well not in assignments:
                unassigned.append(well)
                continue
            readings.append(
                {
                    **assignments[well],
                    "value": value,
                    "unit": readout.unit,
                    "readout": readout.kind,
                    "channel": readout.channel,
                    "source_row": source_row,
                }
            )
    if unassigned:
        issues.append("Measured wells without assignments: " + ", ".join(unassigned) + ".")
    missing = [well for well in assignments if well not in measured]
    if missing:
        issues.append("Assigned wells missing a reading: " + ", ".join(missing) + ".")
    if not measured:
        issues.append("The matrix contains no measurements.")
    return readings


def preview_reader(
    content: bytes,
    plate_size: int,
    layout: list[dict[str, Any]],
    reader_format: str = "bmg_mars_absorbance",
) -> dict[str, Any]:
    """Preview all readings and errors. The caller commits only a preview with no issues.

    `source_row` counts file lines from one. The join uses the exported row letter and
    numbered column, independent of layout order. Controls and numeric zero are retained.
    AU here labels dimensionless absorbance in the documented OD export profile
    (IUPAC, https://doi.org/10.1351/goldbook.A00028).
    """
    issues: list[str] = []
    readings: list[dict[str, Any]] = []
    chosen = FORMATS.get(reader_format)
    preview: dict[str, Any] = {
        "readings": readings,
        "issues": issues,
        "format": chosen.described if chosen else None,
        "acquisition": {},
        "channel": "",
        "unit": "",
    }
    if chosen is None:
        issues.append("Choose a supported reader file format.")
        return preview
    if plate_size not in DIMENSIONS:
        issues.append("Choose a 96- or 384-well layout.")
        return preview
    rows, columns = DIMENSIONS[plate_size]
    assignments = _layout(layout, rows, columns, issues)
    try:
        parsed = chosen.parse(_csv_rows(content), rows, columns)
    except (ValueError, csv.Error) as exc:
        issues.append(str(exc))
        return preview
    preview.update(
        channel=parsed.readout.channel, unit=parsed.readout.unit, acquisition=parsed.acquisition
    )
    readings.extend(_join(parsed.readout, parsed.matrix, rows, columns, assignments, issues))
    return preview

"""Join a BMG LABTECH MARS raw OD endpoint export to an Iki plate layout.

The accepted MicroPlate View export and its settings are documented in data/reader.md.
MARS writes row/column coordinates, not a rectangular table of records. Join the raw
matrix by well before asking the ordinary Iki run output writer to commit measurements.
"""

from __future__ import annotations

import csv
import io
import math
import re
from typing import Any
from uuid import UUID

# ANSI/SLAS 4-2004 (R2012), sections 4.1 and 4.2, well positions in microplates.
DIMENSIONS = {96: (8, 12), 384: (16, 24)}
MAX_BYTES = 5 * 1024 * 1024  # AssayPlate.reader_file's upload limit.
WELL = re.compile(r"([A-P])0?([1-9][0-9]?)", re.ASCII)
RAW = re.compile(r"Raw Data\s*\((\d{1,4})\)", re.ASCII | re.IGNORECASE)
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)


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


def _csv_rows(content: bytes) -> list[tuple[int, list[str]]]:
    if len(content) > MAX_BYTES:
        raise ValueError("The endpoint CSV exceeds 5 MiB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("cp1252")  # The legacy Windows MARS export.
        except UnicodeDecodeError as exc:
            raise ValueError("Choose a UTF-8 or Windows-1252 MARS CSV export.") from exc
    if "\0" in text:
        raise ValueError("Choose a UTF-8 or Windows-1252 MARS CSV export.")
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


def _matrix(content: bytes, rows: int, columns: int) -> tuple[str, list[tuple[int, list[str]]]]:
    parsed = _csv_rows(content)
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
    headings = ["", *(str(column) for column in range(1, columns + 1))]
    if index + 1 >= len(parsed) or parsed[index + 1][1] != headings:
        raise ValueError(
            f"The matrix must label columns 1 through {columns} in order, "
            f"once each, for a {rows * columns}-well plate."
        )
    return f"Absorbance {int(wavelength[1])} nm", parsed[index + 2 :]


def preview_reader(content: bytes, plate_size: int, layout: list[dict[str, Any]]) -> dict[str, Any]:
    """Preview all readings and errors. The caller commits only a preview with no issues.

    `source_row` counts file lines from one. The join uses the exported row letter and
    numbered column, independent of layout order. Controls and numeric zero are retained.
    AU here labels dimensionless absorbance in the documented OD export profile
    (IUPAC, https://doi.org/10.1351/goldbook.A00028).
    """
    issues: list[str] = []
    readings: list[dict[str, Any]] = []
    preview = {"readings": readings, "issues": issues, "channel": "", "unit": "AU"}
    if plate_size not in DIMENSIONS:
        issues.append("Choose a 96- or 384-well layout.")
        return preview
    rows, columns = DIMENSIONS[plate_size]
    assignments = _layout(layout, rows, columns, issues)
    try:
        channel, matrix = _matrix(content, rows, columns)
    except (ValueError, csv.Error) as exc:
        issues.append(str(exc))
        return preview
    preview["channel"] = channel
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
                issues.append(f"File row {source_row}, {well}: expected a finite OD reading.")
                continue
            if well not in assignments:
                unassigned.append(well)
                continue
            readings.append(
                {
                    **assignments[well],
                    "value": value,
                    "unit": "AU",
                    "channel": channel,
                    "source_row": source_row,
                }
            )
    if unassigned:
        issues.append("Measured wells without assignments: " + ", ".join(unassigned) + ".")
    missing = [well for well in assignments if well not in measured]
    if missing:
        issues.append("Assigned wells missing a reading: " + ", ".join(missing) + ".")
    if not measured:
        issues.append("The raw matrix contains no measurements.")
    return preview

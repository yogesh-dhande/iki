"""Read a QuantStudio or ViiA 7 results export into its qPCR run's fields.

QuantStudio Real-Time PCR Software and ViiA 7 Software export a run's results as
text (File → Export, Results): lines ``* Name = value`` describing the experiment
and the instrument, then sections, each a line such as ``[Results]`` over a
tab-separated table. The columns a table has depend on the experiment type and the
export's settings, so they're read by their headings, never by position.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

#: A 96- or 384-well block's wells, A1 to P24.
WELL = re.compile(r"^[A-P](?:[1-9]|1[0-9]|2[0-4])$")
#: A number as the software writes one: thousands may be grouped with commas
#: (``100,000,000.000``), and the decimal separator is a point.
NUMBER = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
#: The header's facts the record keeps, by the names the software gives them. ViiA 7
#: Software calls the user `Experiment User Name`; QuantStudio, `User Name`.
FACTS = {
    "experiment_name": ("Experiment Name",),
    "experiment_type": ("Experiment Type",),
    "instrument_type": ("Instrument Type",),
    "instrument_serial": ("Instrument Serial Number",),
    "block": ("Block Type",),
    "chemistry": ("Chemistry",),
    "passive_reference": ("Passive Reference",),
    "cycle_method": ("Quantification Cycle Method",),
    "run_ended": ("Experiment Run End Time",),
    "operator": ("User Name", "Experiment User Name"),
}
#: The field's length, as the type declares it: a longer value is refused, not cut.
LENGTHS = {
    "experiment_name": 255,
    "experiment_type": 100,
    "instrument_type": 100,
    "instrument_serial": 100,
    "block": 100,
    "chemistry": 50,
    "passive_reference": 50,
    "cycle_method": 20,
    "run_ended": 64,
    "operator": 200,
}
#: What the software writes where it has no value.
UNSTATED = {"", "NA", "N/A"}
#: The Cq column for each quantification cycle method. QuantStudio writes `CT`,
#: and `CRT` for relative threshold (Crt) analysis.
CYCLE_COLUMNS = {"ct": ("CT", "Ct", "Cт", "Cq"), "crt": ("CRT", "Crt", "Cq")}
#: A comparative Cт (ΔΔCт) analysis's settings, which the export lists under its table.
SETTINGS = {"endogenous_control": "Endogenous Control", "reference_sample": "Reference Sample"}
CALIBRATION = re.compile(r"^Calibration (.+?) is expired$")
MAX_ROWS = 1536


class ExportError(ValueError):
    """The file isn't a results export this can read, saying where and why."""


def read_export(source: bytes) -> dict[str, Any]:
    """The record's values from an export's bytes."""
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ExportError(
            f"The export isn't UTF-8 text (byte {error.start + 1}). Export the results "
            "from QuantStudio Real-Time PCR Software as text."
        ) from None
    lines = text.splitlines()
    header, sections = _split(lines)
    if "Results" not in sections:
        raise ExportError(
            "This isn't a QuantStudio results export: it has no [Results] section. "
            "In QuantStudio Real-Time PCR Software, export the run's Results as text."
        )
    values: dict[str, Any] = {}
    for field, names in FACTS.items():
        stated = next((header[n] for n in names if header.get(n) not in (None, *UNSTATED)), None)
        if stated is not None and len(stated) > LENGTHS[field]:
            raise ExportError(
                f"The export's {names[0]} is longer than {LENGTHS[field]} characters."
            )
        values[field] = stated
    values["expired_calibrations"] = sorted(
        match[1].strip()
        for name, stated in header.items()
        if (match := CALIBRATION.match(name)) and stated.lower() == "yes"
    )
    start, lines = sections["Results"]
    method = (values["cycle_method"] or "Ct").lower()
    if method not in CYCLE_COLUMNS:
        raise ExportError(f"The export's quantification cycle method, {method}, isn't Ct or Crt.")
    table, headings, settings = _results(start, lines)
    for field, name in SETTINGS.items():
        stated = settings.get(name)
        if stated is not None and len(stated) > 255:
            raise ExportError(f"The export's {name} is longer than 255 characters.")
        values[field] = None if stated in (None, *UNSTATED) else stated
    values["wells"], values["standard_curves"] = _wells(
        start, table, headings, CYCLE_COLUMNS[method]
    )
    return values


def _split(lines: list[str]) -> tuple[dict[str, str], dict[str, tuple[int, list[str]]]]:
    """The ``* name = value`` lines, and each section's first line number and lines."""
    header: dict[str, str] = {}
    sections: dict[str, tuple[int, list[str]]] = {}
    current: list[str] | None = None
    for number, line in enumerate(lines, 1):
        heading = re.fullmatch(r"\[([^\]]+)\]\s*", line)
        if heading:
            if heading[1] in sections:
                raise ExportError(f"Line {number}: the export has two [{heading[1]}] sections.")
            current = []
            sections[heading[1]] = (number + 1, current)
        elif current is not None:
            current.append(line)
        elif line.startswith("* "):
            name, equals, stated = line[2:].partition("=")
            if not equals:
                raise ExportError(f"Line {number} isn't a header line, * name = value.")
            header[" ".join(name.split())] = stated.strip()
        elif line.strip():
            raise ExportError(
                f"Line {number} is neither a header line nor in a section. Export the "
                "results as text from QuantStudio Real-Time PCR Software."
            )
    return header, sections


Rows = list[tuple[int, list[str]]]


def _results(start: int, lines: list[str]) -> tuple[Rows, list[str], dict[str, str]]:
    """The [Results] table's rows, its headings, and the analysis settings under it."""
    rows: Rows = []
    settings: dict[str, str] = {}
    headings: list[str] | None = None
    ended = False
    for number, line in enumerate(lines, start):
        if not line.strip():
            ended = headings is not None
            continue
        if ended:
            # A comparative Cт export lists its analysis settings under the table.
            name, equals, stated = line.partition("=")
            if not equals:
                raise ExportError(f"Line {number} isn't in the [Results] table or a setting.")
            settings[" ".join(name.split())] = stated.strip()
        elif headings is None:
            headings = next(csv.reader([line], delimiter="\t"))
        else:
            rows.append((number, next(csv.reader([line], delimiter="\t"))))
    if headings is None:
        raise ExportError("The [Results] section is empty: export the run once it's analysed.")
    return rows, headings, settings


def _wells(
    start: int, table: Rows, headings: list[str], cycle_columns: tuple[str, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # A heading can repeat (the results-only export ends with Target Name and Task
    # again, after the colour columns): the first is the result's.
    column: dict[str, int] = {}
    for i, heading in enumerate(headings):
        column.setdefault(heading.strip(), i)
    needed = ("Well Position", "Target Name", "Sample Name", "Task", "Reporter")
    missing = [name for name in needed if name not in column]
    cq_column = next((name for name in cycle_columns if name in column), None)
    if cq_column is None:
        missing.append(cycle_columns[0])
    if missing:
        raise ExportError(
            f"Line {start}: the [Results] table has no {', '.join(missing)} column. "
            "Export every result column."
        )

    wells: list[dict[str, Any]] = []
    curves: dict[str, dict[str, Any]] = {}
    seen: dict[tuple[str, str], int] = {}
    for number, cells in table:
        if len(cells) > len(headings):
            raise ExportError(f"Line {number} has more cells than the [Results] headings.")

        def cell(name: str, cells: list[str] = cells) -> str:
            at = column.get(name)
            return cells[at].strip() if at is not None and at < len(cells) else ""

        def number_in(name: str, number: int = number) -> float | None:
            return _number(cell(name), number, name)

        well = cell("Well Position").upper()
        if not WELL.match(well):
            raise ExportError(
                f"Line {number}: {well or 'an empty well'} isn't a well from A1 to P24."
            )
        target = cell("Target Name")
        if not target:
            raise ExportError(f"Line {number}: well {well} has no target.")
        key = (well, target)
        if key in seen:
            raise ExportError(
                f"Line {number}: well {well} has a second {target} result (line {seen[key]})."
            )
        seen[key] = number
        stated_cq = cell(cq_column)
        undetermined = stated_cq.lower() == "undetermined"
        wells.append(
            {
                "well": well,
                "sample": cell("Sample Name") or None,
                "target": target,
                "task": cell("Task") or None,
                "reporter": cell("Reporter") or None,
                "quencher": cell("Quencher") or None,
                "omitted": _flag(cell("Omit"), number, "Omit"),
                "cq": None if undetermined else _number(stated_cq, number, cq_column),
                "undetermined": undetermined,
                "threshold": number_in("Ct Threshold"),
                "quantity": number_in("Quantity"),
            }
        )
        if len(wells) > MAX_ROWS:
            raise ExportError(f"The export has more than {MAX_ROWS} well results.")
        curve = {
            "target": target,
            "slope": number_in("Slope"),
            "y_intercept": number_in("Y-Intercept"),
            "r_squared": number_in("R(superscript 2)"),
            "efficiency": number_in("Efficiency"),
        }
        stated_curve = any(curve[k] is not None for k in curve if k != "target")
        # The software repeats a target's curve on each of its wells.
        if stated_curve and curves.setdefault(target, curve) != curve:
            raise ExportError(
                f"Line {number}: {target}'s standard curve differs from its earlier wells'."
            )
    return wells, list(curves.values())


def _number(stated: str, line: int, column: str) -> float | None:
    if stated == "":
        return None
    if not NUMBER.match(stated):
        raise ExportError(f"Line {line}: {column} is {stated!r}, which isn't a number.")
    return float(stated.replace(",", ""))


def _flag(stated: str, line: int, column: str) -> bool:
    if stated.lower() in ("", "false"):
        return False
    if stated.lower() == "true":
        return True
    raise ExportError(f"Line {line}: {column} is {stated!r}, not true or false.")


def run_output(run: dict[str, Any]) -> dict[str, Any]:
    """The run's output, from its ``input.json``."""
    (record,) = run["inputs"]["run"]
    stated = record["values"]
    file_id = stated["results_file"]
    described = run["files"][file_id]
    source = Path(described["path"]).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != stated["source_sha256"] or digest != described["sha256"]:
        raise ExportError(
            "The results file isn't the export this run's record names: its SHA-256 is "
            f"{digest}, the record's {stated['source_sha256']}."
        )
    values = read_export(source)
    messages = []
    if values["expired_calibrations"]:
        messages.append(
            {
                "level": "warning",
                "entity": record["id"],
                "text": "The export says these calibrations had expired: "
                + ", ".join(values["expired_calibrations"])
                + ".",
            }
        )
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {"run": [{"entity": record["id"], "require_unchanged": True, "values": values}]},
        "metrics": {
            "wells": len(values["wells"]),
            "undetermined": sum(1 for w in values["wells"] if w["undetermined"]),
        },
        "messages": messages,
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        rendered = json.dumps(run_output(run), allow_nan=False, ensure_ascii=False)
    except (ValueError, OSError) as error:
        print(f"::error {error}", flush=True)
        sys.exit(1)
    Path(os.environ["IKI_OUTPUT"]).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()

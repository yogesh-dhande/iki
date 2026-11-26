"""Preview and commit a plate export, then fit a sample from the committed read."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from reader import FORMATS, preview_reader

PLUGIN = "iki/plate_assays"
SIZES = {"plate_96": 96, "plate_384": 384}
# SI prefixes, BIPM SI Brochure (9th ed.), Table 7, https://doi.org/10.59161/AUEZ1291.
MOLAR = {"M": 1.0, "mM": 1e-3, "uM": 1e-6, "nM": 1e-9}
# Each readout is its own typed field on a well measurement, so a reading keeps its unit.
READOUT_FIELDS = {"absorbance": "signal", "normalized_activity": "activity_percent"}
RESPONSE_UNITS = {"absorbance": "AU", "normalized_activity": "percent"}


def layout_of(values: dict[str, Any]) -> dict[str, Any]:
    """What a committed read's wells were joined to. Every later read of the plate keeps it."""
    return {
        "size": values["size"],
        "assignments": values.get("assignments") or [],
        "procedure": values.get("procedure"),
        "procedure_revision": values.get("procedure_revision"),
        "request": values.get("request"),
    }


def reader_output(run: dict[str, Any], *, commit: bool) -> dict[str, Any]:
    plate = run["inputs"]["plate"][0]
    values = plate["values"]
    file_id = values.get("reader_file")
    if not file_id:
        raise ValueError("Attach the plate reader's export file to the plate.")
    reader_format = values.get("reader_format")
    if reader_format not in FORMATS:
        raise ValueError("Choose the reader file format on the plate.")
    source = Path(run["files"][file_id]["path"]).read_bytes()
    assignments = values.get("assignments") or []
    size = values["size"]
    procedure = {key: values[key] for key in ("procedure", "procedure_revision") if values.get(key)}
    if procedure and (len(procedure) != 2 or not all(text.strip() for text in procedure.values())):
        raise ValueError("Give both the analytical procedure ID and its revision.")
    committed = values.get("committed_layout")
    if values.get("committed") and committed != layout_of(values):
        raise ValueError("A committed plate keeps its layout. Start a new plate to change it.")
    digest = hashlib.sha256(source).hexdigest()
    preview = preview_reader(source, SIZES[size], assignments, reader_format)
    reads = values.get("committed_reads") or []
    if any(read.get("sha256") == digest for read in reads):
        preview["issues"].append(
            "This reader file is already committed to this plate. Attach the next read's file."
        )
    # Bind the reviewed preview to the *specific* file, assignments and request. Even a
    # correction giving identical numerical results requires another preview.
    preview["source"] = {
        **procedure,
        "reader_file": file_id,
        "reader_format": reader_format,
        "sha256": digest,
        "size": size,
        "assignments": assignments,
        "request": values.get("request"),
    }
    updated: dict[str, Any] = {"entity": plate["id"], "require_unchanged": True}
    output: dict[str, Any] = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {"plate": [updated]},
    }
    if not commit:
        updated["values"] = {"preview": preview}
        return output
    if preview["issues"]:
        raise ValueError("Resolve the preview issues before committing: " + preview["issues"][0])
    if preview != values.get("preview"):
        raise ValueError("Preview this reader file and the current layout before committing.")
    # Each read of a plate is its own acquisition, one per reader file. The same export
    # can't be committed to the plate twice, including when two commits race.
    import_key = hashlib.sha256(f"{plate['id']}\n{digest}".encode()).hexdigest()
    updated["values"] = {
        "committed": True,
        "committed_layout": layout_of(values),
        "committed_reads": [*reads, {"reader_file": file_id, "sha256": digest}],
    }
    creates = []
    references = []
    source_fields = {
        "plate": plate["id"],
        "plate_revision": plate["revision"],
        "reader_file": file_id,
    }
    for reading in preview["readings"]:
        fields = dict(reading)
        fields.pop("unit")
        field = READOUT_FIELDS[fields["readout"]]
        fields[field] = fields.pop("value")
        key = fields["well"]
        creates.append(
            {
                "type": f"{PLUGIN}.WellMeasurement",
                "key": key,
                "values": {**source_fields, **fields},
            }
        )
        references.append({"$create": key})
    described = preview["format"]
    creates.append(
        {
            "type": f"{PLUGIN}.PlateRead",
            "values": {
                **source_fields,
                **procedure,
                "name": values["name"],
                "import_key": import_key,
                "reader_format": f"{described['id']}/{described['version']}",
                "acquisition": preview["acquisition"],
                "request": values.get("request"),
                "measurements": references,
            },
        }
    )
    output["creates"] = creates
    return output


def exclusions_by_well(text: str) -> dict[str, str]:
    """The operator supplies reasons. Their identity comes from the authenticated run."""
    excluded = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        left, _, right = line.partition(":")
        well, reason = left.strip().upper(), right.strip()
        if not well or not reason:
            raise ValueError(f"Exclusion line {number} needs a well and a reason, e.g. A1: bubble.")
        if well in excluded:
            raise ValueError(f"Well {well} has more than one exclusion reason.")
        excluded[well] = reason
    return excluded


Fit = Callable[[list[tuple[float, float]], str], dict[str, Any]]


def methods() -> dict[str, Fit]:
    """The fit methods this plugin ships, by name. Each result records the method's name
    and the version this release implements, such as 4PL-OLS/1, in `fit_version`."""
    import four_pl

    return {four_pl.METHOD.split("/")[0]: four_pl.fit_curve}


def series(run: dict[str, Any]) -> dict[str, Any]:
    """One sample's committed readings in one dose unit, with the operator's exclusions.

    Independent of any fit method: what a fit reads and what its result pins.
    """
    read = run["inputs"]["read"][0]
    sample_record = run["inputs"]["sample"][0]
    reference_id = sample_record.get("entry_id", sample_record["id"])
    read_values = read["values"]
    if not read_values.get("import_key"):
        raise ValueError("Choose a plate read created by the reader commit action.")
    measurements = read.get("included", {}).get("measurements", [])
    if [m["id"] for m in measurements] != read_values.get("measurements"):
        raise ValueError("The committed measurement records are incomplete.")
    selected = [m for m in measurements if m["values"].get("sample") == reference_id]
    if not selected:
        raise ValueError("The selected sample has no measurements in this plate read.")
    excluded = exclusions_by_well(run["params"].get("exclusions") or "")
    author = run["run"].get("requested_by")
    if excluded and not author:
        raise ValueError("Excluding readings requires an authenticated run requester.")
    unit = selected[0]["values"].get("concentration_unit")
    points = []
    included = []
    response_units = set()
    for measurement in selected:
        values = measurement["values"]
        well = values["well"]
        given_unit = values.get("concentration_unit")
        if given_unit not in MOLAR or values.get("concentration") is None or unit not in MOLAR:
            raise ValueError(f"{well} has no dose with units. Fit a sample with a dilution series.")
        if (
            values.get("plate") != read_values["plate"]
            or values.get("reader_file") != read_values["reader_file"]
        ):
            raise ValueError(f"{well} is not from this committed read.")
        dose = values["concentration"] * (MOLAR[given_unit] / MOLAR[unit])
        readout = values.get("readout")
        if readout not in READOUT_FIELDS or values.get(READOUT_FIELDS[readout]) is None:
            raise ValueError(f"{well} has no reading of its stated readout.")
        response = values[READOUT_FIELDS[readout]]
        response_units.add(RESPONSE_UNITS[readout])
        reason = excluded.pop(well, None)
        points.append(
            {
                "reading": measurement["id"],
                "reading_hrid": measurement["hrid"],
                "revision": measurement["revision"],
                "well": well,
                "replicate": values["replicate"],
                "dose": dose,
                "response": response,
                "exclusion_reason": reason,
                "excluded_by": author if reason else None,
            }
        )
        if reason is None:
            included.append((dose, response))
    if excluded:
        raise ValueError("Excluded wells outside this sample's series: " + ", ".join(excluded))
    channels = {m["values"]["channel"] for m in selected}
    if len(channels) != 1 or len(response_units) != 1:
        raise ValueError("Fit one detector channel at a time.")
    return {
        "points": points,
        "included": included,
        "values": {
            **{
                k: read_values[k] for k in ("procedure", "procedure_revision") if read_values.get(k)
            },
            "read": read["id"],
            "sample": sample_record["id"],
            "points": points,
            "channel": channels.pop(),
            "response_unit": response_units.pop(),
            "concentration_unit": unit,
            "measurements": [m["id"] for m in selected],
        },
    }


def result_type(run: dict[str, Any], flow: str) -> tuple[str, str | None]:
    """The result class for the review flow, and for a requested sample, the request.

    Which class a result is doesn't depend on its fit method.
    """
    result_types = {"research": "ResearchAssayResult", "regulated_qc": "QCAssayResult"}
    if flow not in result_types:
        raise ValueError("Choose research or regulated_qc in the assay review settings.")
    read = run["inputs"]["read"][0]
    sample = run["inputs"]["sample"][0]["id"]
    request = read["values"].get("request")
    if any(
        item["id"] == request and sample in item.get("covered_materials", [])
        for item in read.get("included", {}).get("request", [])
    ):
        return f"Requested{result_types[flow]}", request
    return result_types[flow], None


def fit_output(run: dict[str, Any]) -> dict[str, Any]:
    method = run["params"].get("method", "4PL-OLS")
    fit = methods().get(method)
    if fit is None:
        raise ValueError(f"Choose a fit method this plugin ships: {', '.join(methods())}.")
    flow = run.get("settings", {}).get("review_flow", "research")
    kind, request = result_type(run, flow)
    measured = series(run)
    fitted = fit(measured["included"], run["params"].get("endpoint_kind", "IC50"))
    result = {**fitted, **measured["values"], "review_flow": flow}
    if request:
        result["request"] = request
    read, sample = run["inputs"]["read"][0]["id"], run["inputs"]["sample"][0]["id"]
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {
                "type": f"{PLUGIN}.{kind}",
                "values": result,
                "derived_from": [read, sample],
            }
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        action = sys.argv[1]
        output = (
            fit_output(run) if action == "fit" else reader_output(run, commit=action == "commit")
        )
        rendered = json.dumps(output, allow_nan=False)
    except (ValueError, OSError) as error:
        print(f"::error {error}", flush=True)
        sys.exit(1)
    Path(os.environ["IKI_OUTPUT"]).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()

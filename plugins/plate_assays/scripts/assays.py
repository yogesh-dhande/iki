"""Preview and commit a plate export, then fit a sample from the committed read."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from reader import preview_reader

PLUGIN = "iki/plate_assays"
SIZES = {"plate_96": 96, "plate_384": 384}
# SI prefixes, BIPM SI Brochure (9th ed.), Table 7, https://doi.org/10.59161/AUEZ1291.
MOLAR = {"M": 1.0, "mM": 1e-3, "uM": 1e-6, "nM": 1e-9}


def reader_output(run: dict[str, Any], *, commit: bool) -> dict[str, Any]:
    plate = run["inputs"]["plate"][0]
    values = plate["values"]
    if values.get("committed"):
        raise ValueError("This plate's measurements have already been committed.")
    file_id = values.get("reader_file")
    if not file_id:
        raise ValueError("Attach the BMG MARS raw OD endpoint file to the plate.")
    source = Path(run["files"][file_id]["path"]).read_bytes()
    assignments = values.get("assignments") or []
    size = values["size"]
    procedure = {key: values[key] for key in ("procedure", "procedure_revision") if values.get(key)}
    if procedure and (len(procedure) != 2 or not all(text.strip() for text in procedure.values())):
        raise ValueError("Give both the analytical procedure ID and its revision.")
    preview = preview_reader(source, SIZES[size], assignments)
    # Bind the reviewed preview to the *specific* file, assignments and request. Even a
    # correction giving identical numerical results requires another preview.
    preview["source"] = {
        **procedure,
        "reader_file": file_id,
        "sha256": hashlib.sha256(source).hexdigest(),
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
    updated["values"] = {"committed": True}
    creates = []
    references = []
    source_fields = {
        "plate": plate["id"],
        "plate_revision": plate["revision"],
        "reader_file": file_id,
    }
    for reading in preview["readings"]:
        fields = dict(reading)
        fields["signal"] = fields.pop("value")
        del fields["unit"]  # Raw OD is stored in the typed dimensionless signal field.
        key = fields["well"]
        creates.append(
            {
                "type": f"{PLUGIN}.WellMeasurement",
                "key": key,
                "values": {**source_fields, **fields},
            }
        )
        references.append({"$create": key})
    creates.append(
        {
            "type": f"{PLUGIN}.PlateRead",
            "values": {
                **source_fields,
                **procedure,
                "name": values["name"],
                "import_key": plate["id"],
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


def fit_output(run: dict[str, Any]) -> dict[str, Any]:
    from four_pl import fit_curve

    read = run["inputs"]["read"][0]
    sample_record = run["inputs"]["sample"][0]
    sample = sample_record["id"]
    reference_id = sample_record.get("entry_id", sample)
    read_values = read["values"]
    if not read_values.get("import_key") or read_values["import_key"] != read_values["plate"]:
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
        response = values["signal"]
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
    if len(channels) != 1:
        raise ValueError("Fit one detector channel at a time.")
    fitted = fit_curve(included, run["params"].get("endpoint_kind", "IC50"))
    flow = run.get("settings", {}).get("review_flow", "research")
    result_types = {"research": "ResearchAssayResult", "regulated_qc": "QCAssayResult"}
    if flow not in result_types:
        raise ValueError("Choose research or regulated_qc in the assay review settings.")
    result_type = result_types[flow]
    result = {
        **fitted,
        **{k: read_values[k] for k in ("procedure", "procedure_revision") if read_values.get(k)},
        "read": read["id"],
        "sample": sample,
        "review_flow": flow,
        "points": points,
        "channel": channels.pop(),
        "response_unit": "AU",
        "concentration_unit": unit,
        "measurements": [m["id"] for m in selected],
    }
    request = read_values.get("request")
    if any(
        item["id"] == request and sample in item.get("covered_materials", [])
        for item in read.get("included", {}).get("request", [])
    ):
        result_type = f"Requested{result_type}"
        result["request"] = request
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {
                "type": f"{PLUGIN}.{result_type}",
                "values": result,
                "derived_from": [read["id"], sample],
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

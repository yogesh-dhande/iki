"""Build service, instrument-use, and calibration-impact effects from pinned inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SIGNED = frozenset({"signed_off", "released"})
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
PREFIX = "iki/equipment."
Record = dict[str, Any]


def timestamp(value: str) -> datetime:
    """Use UTC for comparisons and keep fractional seconds for the idempotence key."""
    if not RFC3339.fullmatch(value):
        raise ValueError("Give the measurement date and time with seconds and a time zone.")
    return datetime.fromisoformat(value).astimezone(UTC)


def key(*values: object) -> str:
    """Key exact evidence so repeating a trace or use can find its existing record."""
    return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()


def schedule(run: Record) -> list[Record]:
    (instrument,) = run["inputs"]["instrument"]
    if instrument["values"]["service"] == "retired":
        raise ValueError("A retired instrument cannot be scheduled for service.")
    return [
        {
            "type": PREFIX + "EquipmentService",
            "values": {
                "instrument": instrument["id"],
                "kind": run["params"]["kind"],
                "due_on": run["params"]["due_on"],
                "procedure": run["params"]["procedure"],
                "review_flow": run["settings"]["review_flow"],
            },
        }
    ]


def record_use(run: Record) -> list[Record]:
    inputs = run["inputs"]
    (instrument,) = inputs["instrument"]
    (result,) = inputs["result"]
    (calibration,) = inputs["calibration"]
    reading_time = timestamp(run["params"]["measurement_at"])
    criteria = calibration["values"]
    if (
        criteria["instrument"] != instrument["id"]
        or criteria["kind"] != "calibration"
        or criteria["outcome"] != "in_tolerance"
        or criteria["review"] not in SIGNED
    ):
        raise ValueError("Select a signed passing calibration for this instrument.")
    if not timestamp(criteria["performed_at"]) <= reading_time < timestamp(criteria["valid_until"]):
        raise ValueError("The selected calibration does not cover the measurement time.")
    if result["type"].startswith(PREFIX) or result["id"] == instrument["id"]:
        raise ValueError("Select the actual measured result.")
    at = reading_time.isoformat()
    return [
        {
            "type": PREFIX + "InstrumentUse",
            "derived_from": [instrument["id"], result["id"], calibration["id"]],
            "values": {
                "key": key(instrument["id"], result["id"], result["revision"], at),
                "instrument": instrument["id"],
                "result": result["id"],
                "result_record_id": result["id"],
                "result_revision": result["revision"],
                "calibration": calibration["id"],
                "calibration_revision": calibration["revision"],
                "measurement_at": at,
            },
        }
    ]


def trace(run: Record) -> list[Record]:
    inputs = run["inputs"]
    (instrument,) = inputs["instrument"]
    calibrations = inputs["calibrations"]
    uses = inputs.get("uses", [])
    existing = inputs.get("existing", [])
    records = [*calibrations, *uses, *existing]
    if any(record["values"]["instrument"] != instrument["id"] for record in records):
        raise ValueError(
            "Every service, use and impact record must belong to the selected instrument."
        )
    signed = [
        record
        for record in calibrations
        if record["values"]["kind"] == "calibration" and record["values"]["review"] in SIGNED
    ]
    failures = [record for record in signed if record["values"]["outcome"] == "out_of_tolerance"]
    if not failures:
        raise ValueError("Select at least one signed out-of-tolerance calibration for the trace.")
    passes = sorted(
        timestamp(record["values"]["performed_at"])
        for record in signed
        if record["values"]["outcome"] == "in_tolerance"
    )
    measurements = sorted(
        ((timestamp(use["values"]["measurement_at"]), use) for use in uses), key=lambda m: m[0]
    )
    already = {record["values"]["key"] for record in existing}
    # One case for each result a failure affects, from its earliest use in the window: a result
    # that already has one, traced or opened manually, gets no second.
    covered = {(record["values"]["failure"], record["values"]["result"]) for record in existing}
    impacts = []
    cased = 0
    for failure in failures:
        values = failure["values"]
        start = timestamp(values["suspect_since"])
        discovery = timestamp(values["performed_at"])
        next_pass = bisect_right(passes, discovery)
        cleared = passes[next_pass] if next_pass < len(passes) else None
        # What the investigator reviews: the interval, and what it was checked against.
        within = [m for m in measurements if m[0] >= start and (cleared is None or m[0] < cleared)]
        print(
            f"{failure.get('hrid', failure['id'])}: reviewed measurements from "
            f"{start.isoformat()} "
            + (
                f"to the passing calibration at {cleared.isoformat()}"
                if cleared is not None
                else "onward: no passing calibration after it was selected, so the window "
                "stays open"
            )
            + f". {len(within)} of the {len(measurements)} selected uses are in it"
            + (f", the last at {within[-1][0].isoformat()}." if within else ".")
        )
        for at, use in measurements:
            case = key(failure["id"], use["id"])
            if at < start or (cleared is not None and at >= cleared) or case in already:
                continue
            affected = (failure["id"], use["values"]["result"])
            if affected in covered:
                cased += 1
                continue
            covered.add(affected)
            impacts.append(
                {
                    "type": PREFIX + "CalibrationImpact",
                    "derived_from": [instrument["id"], failure["id"], use["id"]],
                    "values": {
                        "key": case,
                        "instrument": instrument["id"],
                        "failure": failure["id"],
                        "failure_revision": failure["revision"],
                        "use": use["id"],
                        "result": use["values"]["result"],
                        "measurement_at": use["values"]["measurement_at"],
                        "review_flow": values["review_flow"],
                        "opened": "traced",
                    },
                }
            )
            if len(impacts) > 1000:
                raise ValueError(
                    "Trace at most 1,000 new impacts per run. Divide the use records into batches."
                )
    print(
        f"Checked {len(measurements)} uses against {len(failures)} failures. "
        f"{len(impacts)} new cases"
        + (
            f", and {cased} uses of results that already have a case for the failure"
            if cased
            else ""
        )
        + ". Only the selected uses were checked: trace again with "
        "any other uses of this instrument in these intervals, from this project or others."
    )
    return impacts


def open_case(run: Record) -> list[Record]:
    """A case Equipment QA opens for a result no instrument use was recorded for."""
    inputs = run["inputs"]
    (failure,) = inputs["failure"]
    (result,) = inputs["result"]
    values = failure["values"]
    if (
        values["kind"] != "calibration"
        or values["outcome"] != "out_of_tolerance"
        or values["review"] not in SIGNED
        or not values.get("suspect_since")
    ):
        raise ValueError(
            "Select a signed out-of-tolerance calibration that says when the fault began."
        )
    if result["type"].startswith(PREFIX) or result["id"] == values["instrument"]:
        raise ValueError("Select the actual measured result.")
    reading_time = timestamp(run["params"]["measurement_at"])
    if reading_time < timestamp(values["suspect_since"]):
        raise ValueError("The measurement was before the failed calibration's suspected onset.")
    reason = run["params"]["reason"].strip()
    if not reason:
        raise ValueError("Say why the result was affected though no instrument use was recorded.")
    opener = run["run"].get("requested_by")
    if not opener:
        raise ValueError("Opening an impact case manually needs a signed-in person.")
    return [
        {
            "type": PREFIX + "CalibrationImpact",
            "derived_from": [failure["id"], result["id"]],
            "values": {
                "key": key(failure["id"], "opened manually", result["id"]),
                "instrument": values["instrument"],
                "failure": failure["id"],
                "failure_revision": failure["revision"],
                "result": result["id"],
                "measurement_at": reading_time.isoformat(),
                "review_flow": values["review_flow"],
                "opened": "manually",
                "opened_by": opener,
                "opened_reason": reason,
            },
        }
    ]


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    action = {
        "schedule": schedule,
        "record-use": record_use,
        "trace": trace,
        "open-case": open_case,
    }[sys.argv[1]]
    try:
        creates = action(run)
    except (ValueError, KeyError) as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from None
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": creates}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()

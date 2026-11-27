"""Trace a failed calibration to affected measurements in the instrument’s project."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from bisect import bisect_right
from datetime import UTC, datetime
from typing import Any

import iki_action as iki

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
    """Use Equipment’s failure/use key so a manual trace finds the same case."""
    return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()


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
    # Equipment keeps one case for each result a failure affects, from its earliest use in the
    # window: a result that already has one, traced or opened manually, gets no second.
    covered = {(record["values"]["failure"], record["values"]["result"]) for record in existing}
    impacts = []
    for failure in failures:
        values = failure["values"]
        start = timestamp(values["suspect_since"])
        discovery = timestamp(values["performed_at"])
        next_pass = bisect_right(passes, discovery)
        cleared = passes[next_pass] if next_pass < len(passes) else None
        for at, use in measurements:
            case = key(failure["id"], use["id"])
            if at < start or (cleared is not None and at >= cleared) or case in already:
                continue
            affected = (failure["id"], use["values"]["result"])
            if affected in covered:
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
    sys.stdout.write(
        f"Checked {len(measurements)} uses against {len(failures)} failures. "
        f"{len(impacts)} new cases.\n"
    )
    return impacts


if __name__ == "__main__":
    iki.main(lambda run: {"creates": trace(run)}, refuse=(ValueError, KeyError))

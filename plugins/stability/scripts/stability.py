"""Generate the dated pulls of a study revision or record one planned stability test."""

from __future__ import annotations

import calendar
import json
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PLUGIN = "iki/stability"
FLOWS = {"research", "regulated_qc"}


def anniversary(start: date, months: int) -> date:
    """Use calendar months from the original day, clamped at shorter months' end."""
    year, month = divmod(start.month - 1 + months, 12)
    year += start.year
    return date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))


def output(**effects: object) -> dict[str, Any]:
    return {"apiVersion": "iki/v1", "kind": "RunOutput", **effects}


def schedule(run: dict[str, Any]) -> dict[str, Any]:
    study = run["inputs"]["study"][0]
    values = study["values"]
    if values.get("scheduled"):
        raise ValueError("Pulls have already been scheduled for this protocol revision.")
    conditions = {entry["code"]: entry for entry in values["conditions"]}
    time_points = {entry["month"]: entry for entry in values["time_points"]}
    assignments = {(row["condition"], row["month"]): row for row in values["assignments"]}
    expected = {(condition, month) for condition in conditions for month in time_points}
    if (
        not expected
        or expected != assignments.keys()
        or len(assignments) != len(values["assignments"])
    ):
        raise ValueError("Assign exactly one tube and volume to every condition and time point.")
    if not values["tests"]:
        raise ValueError("List the tests to run on each pull.")
    for condition in conditions.values():
        humidity = condition.get("humidity")
        tolerance = condition.get("humidity_tolerance")
        if tolerance is not None and (
            humidity is None or not 0 <= humidity - tolerance <= humidity + tolerance <= 100
        ):
            raise ValueError(f"Enter a valid humidity range for {condition['label']}.")
    includes = study["included"]
    tubes = {record["id"]: record for record in includes["assignments.tube"]}
    lot = includes["lot"][0]
    material_field = lot.get("material_field")
    if not material_field or not lot["values"].get(material_field):
        raise ValueError("The selected lot must identify its material before scheduling pulls.")
    if lot["values"][material_field] != values["material"]:
        raise ValueError("The selected material does not match the lot.")
    flow = run.get("settings", {}).get("review_flow", "research")
    if flow not in FLOWS:
        raise ValueError("Choose a stability measurement review flow before scheduling this study.")
    start = date.fromisoformat(values["starts_on"])
    tube_conditions: dict[str, str] = {}
    pulls = []
    for (condition, month), assignment in sorted(assignments.items()):
        tube = tubes.get(assignment["tube"])
        if tube is None or tube["values"].get("lot") != values["lot"]:
            raise ValueError(f"The tube for {condition}, month {month} must hold the study lot.")
        previous = tube_conditions.setdefault(tube["id"], condition)
        if previous != condition:
            raise ValueError(
                f"Assign separate tubes to storage conditions {previous} and {condition}."
            )
        point = time_points[month]
        due = anniversary(start, month)
        opens = due - timedelta(days=point["days_before"])
        closes = due + timedelta(days=point["days_after"])
        pulls.append(
            {
                "type": f"{PLUGIN}.Pull",
                "values": {
                    "study": study["id"],
                    "study_revision": study["revision"],
                    "lot": values["lot"],
                    "tube": assignment["tube"],
                    "volume_ul": assignment["volume_ul"],
                    "condition": condition,
                    "condition_label": conditions[condition]["label"],
                    "month": month,
                    "opens_on": opens.isoformat(),
                    "due_on": due.isoformat(),
                    "closes_on": closes.isoformat(),
                    "owner": values["owner"],
                },
            }
        )
    return output(
        creates=pulls,
        updates={
            "study": [
                {
                    "entity": study["id"],
                    "require_unchanged": True,
                    "values": {"scheduled": True, "review_flow": flow},
                }
            ]
        },
    )


def measure(run: dict[str, Any]) -> dict[str, Any]:
    pull = run["inputs"]["pull"][0]
    values = pull["values"]
    if (
        values["status"] != "pulled"
        or not values.get("study_revision")
        or not values.get("pulled_on")
    ):
        raise ValueError("Reconcile the draw and close the pull before recording its test.")
    study = pull["included"]["study"][0]["values"]
    flow = study.get("review_flow")
    if flow not in FLOWS or not study.get("scheduled"):
        raise ValueError("The study has no pinned measurement review flow.")
    params = run["params"]
    test = next((row for row in study["tests"] if row["test"] == params["test"]), None)
    if test is None:
        raise ValueError("Choose a test listed on the study protocol.")
    if not math.isfinite(params["value"]):
        raise ValueError("Enter a finite test result.")
    return output(
        creates=[
            {
                "type": f"{PLUGIN}.Measurement",
                "values": {
                    **{
                        name: values[name]
                        for name in ("study", "condition", "condition_label", "month", "pulled_on")
                    },
                    **test,
                    "pull": pull["id"],
                    "pull_revision": pull["revision"],
                    "review_flow": flow,
                    "value": params["value"],
                    "report": params.get("report"),
                    "notes": params.get("notes"),
                },
            }
        ]
    )


def main() -> None:
    args = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        action = {"schedule": schedule, "measure": measure}[sys.argv[1]]
        rendered = json.dumps(action(args), allow_nan=False)
    except (ValueError, KeyError, OSError, OverflowError) as error:
        print(f"::error {error}", flush=True)
        sys.exit(1)
    Path(os.environ["IKI_OUTPUT"]).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()

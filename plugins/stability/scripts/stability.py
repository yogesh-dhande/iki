"""Generate the dated pulls of a study revision or record one planned stability test.

A study's protocol plans its pulls: each condition, time point and replicate it
assigns a tube and amount to, and the tests each gets. That's every condition at every
time point, or a reduced schedule that says why. Time points count from the
start in calendar months, weeks or days (ADR 0243)."""

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


def due_date(start: date, interval: int, unit: str) -> date:
    """A time point's due date: calendar months from the start day, or weeks or days."""
    if unit == "month":
        return anniversary(start, interval)
    return start + timedelta(days=interval * (7 if unit == "week" else 1))


def schedule(run: dict[str, Any]) -> dict[str, Any]:
    study = run["inputs"]["study"][0]
    values = study["values"]
    if values.get("scheduled"):
        raise ValueError("Pulls have already been scheduled for this protocol revision.")
    conditions = {entry["code"]: entry for entry in values["conditions"]}
    time_points = {entry["code"]: entry for entry in values["time_points"]}
    tests = [row["test"] for row in values["tests"]]
    if not tests:
        raise ValueError("List the tests to run on each pull.")
    for row in values["tests"]:
        if (row.get("kind") or "numeric") == "numeric" and not row.get("unit"):
            raise ValueError(f"Give {row['test']}'s reporting unit.")
        if row.get("kind") == "conforms" and row.get("unit"):
            raise ValueError(f"{row['test']} conforms or doesn't: it has no reporting unit.")
    seen: dict[tuple[int, str], str] = {}
    for code, point in time_points.items():
        same = seen.setdefault((point["interval"], point["unit"]), code)
        if same != code:
            raise ValueError(f"Time points {same} and {code} are the same time from the start.")
    assignments = {
        (row["condition"], row["time_point"], row.get("replicate") or 1): row
        for row in values["assignments"]
    }
    if not assignments or len(assignments) != len(values["assignments"]):
        raise ValueError("Plan each condition, time point and replicate's pull once.")
    for condition, point, _ in assignments:
        if condition not in conditions:
            raise ValueError(f"Pull condition {condition} isn't one of the study's conditions.")
        if point not in time_points:
            raise ValueError(f"Pull time point {point} isn't one of the study's time points.")
    planned = {(condition, point) for condition, point, _ in assignments}
    for code, condition in conditions.items():
        if not any(c == code for c, _ in planned):
            raise ValueError(f"Plan at least one pull at {condition['label']}.")
    for code in time_points:
        if not any(t == code for _, t in planned):
            raise ValueError(f"Plan at least one pull at time point {code}.")
    reduced = planned != {(c, t) for c in conditions for t in time_points}
    plan: dict[tuple[str, str], set[str]] = {}
    if values.get("test_plan"):
        for row in values["test_plan"]:
            where = (row["condition"], row["time_point"])
            if where not in planned:
                raise ValueError(
                    f"The test plan tests {row['test']} at {where[0]}, {where[1]}, "
                    "where no pull is planned."
                )
            if row["test"] not in tests:
                raise ValueError(f"{row['test']} isn't one of the study's tests.")
            plan.setdefault(where, set()).add(row["test"])
        if missing := sorted(planned - plan.keys()):
            condition, point = missing[0]
            raise ValueError(f"Plan the tests for the pulls at {condition}, {point}.")
        reduced = reduced or any(len(chosen) < len(tests) for chosen in plan.values())
    else:
        plan = {where: set(tests) for where in planned}
    if reduced and not (values.get("design_justification") or "").strip():
        raise ValueError(
            "This design doesn't pull or test every condition at every time point: say why "
            "in its design justification, as its protocol does."
        )
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
    material_field = (lot.get("roles") or {}).get("material")
    material = lot["id"] if material_field == "id" else lot["values"].get(material_field or "")
    if not material:
        raise ValueError("The selected lot must identify its material before scheduling pulls.")
    if material != values["material"]:
        raise ValueError("The selected material does not match the lot.")
    flow = run.get("settings", {}).get("review_flow", "research")
    if flow not in FLOWS:
        raise ValueError("Choose a stability measurement review flow before scheduling this study.")
    start = date.fromisoformat(values["starts_on"])
    tube_conditions: dict[str, str] = {}
    pulls = []
    order = {code: index for index, code in enumerate(time_points)}
    for (condition, code, replicate), assignment in sorted(
        assignments.items(), key=lambda item: (item[0][0], order[item[0][1]], item[0][2])
    ):
        tube = tubes.get(assignment["tube"])
        if tube is None or tube["values"].get("contents") != values["lot"]:
            raise ValueError(f"The tube for {condition}, {code} must hold the study lot.")
        previous = tube_conditions.setdefault(tube["id"], condition)
        if previous != condition:
            raise ValueError(
                f"Assign separate tubes to storage conditions {previous} and {condition}."
            )
        point = time_points[code]
        due = due_date(start, point["interval"], point["unit"])
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
                    "amount": assignment["amount"],
                    "amount_unit": assignment.get("amount_unit") or "uL",
                    "condition": condition,
                    "condition_label": conditions[condition]["label"],
                    "time_point": code,
                    "interval": point["interval"],
                    "interval_unit": point["unit"],
                    "elapsed_days": (due - start).days,
                    "replicate": replicate,
                    "tests": [test for test in tests if test in plan[(condition, code)]],
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
    if test["test"] not in (values.get("tests") or [test["test"]]):
        raise ValueError(f"The protocol doesn't plan {test['test']} at this pull.")
    typed: dict[str, Any] = {"kind": test.get("kind") or "numeric"}
    if typed["kind"] == "conforms":
        if params.get("value") is not None:
            raise ValueError(f"{test['test']} conforms or doesn't: say which, without a value.")
        if params.get("result") not in ("conforms", "does_not_conform"):
            raise ValueError(f"Say whether {test['test']} conforms.")
        typed["result"] = params["result"]
    else:
        value = params.get("value")
        if params.get("result") is not None:
            raise ValueError(f"{test['test']} is a number: give its observed value.")
        if value is None or not math.isfinite(value):
            raise ValueError("Enter a finite test result.")
        typed["value"] = value
    if seen := str(params.get("observation") or "").strip():
        typed["observation"] = seen
    return output(
        creates=[
            {
                "type": f"{PLUGIN}.Measurement",
                "values": {
                    **{
                        name: values[name]
                        for name in (
                            "study",
                            "condition",
                            "condition_label",
                            "time_point",
                            "elapsed_days",
                            "replicate",
                            "pulled_on",
                        )
                    },
                    **{name: test[name] for name in ("test", "method") if name in test},
                    **({"unit": test["unit"]} if test.get("unit") else {}),
                    **typed,
                    "pull": pull["id"],
                    "pull_revision": pull["revision"],
                    "review_flow": flow,
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

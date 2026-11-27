"""Select, evaluate and snapshot a lot's release evidence using pinned run inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

PLUGIN = "iki/qc_release"
MAX_ATTEMPTS = 250
SUCCESSOR_POLICIES = {"future", "same_day", "backdated"}
#: A limit as a specification states it: a plain decimal, to its last significant place.
#: The criterion's pattern says the same.
LIMIT = re.compile(r"-?(0|[1-9][0-9]{0,8})([.][0-9]{1,6})?")
#: How a criterion compares a result with its limits (USP General Notices 7.20).
ROUNDING = ("limit_places", "unrounded")
#: Enough digits to round any finite double to a limit's places without losing one.
EXACT = Context(prec=400)
FLOWS: dict[str, dict[str, Any]] = {
    "research": {
        "investigation": "ResearchOOSInvestigation",
        "certificate": "ResearchCertificate",
        "investigation_final": "signed_off",
        "certificate_final": "signed_off",
        "issued": "issued",
        "title": "Research certificate of analysis",
        # Said on the certificate, so it is never mistaken for a quality unit's release.
        "intended_use": (
            "For research use. Issued on one reviewer's sign-off, not released by a quality unit."
        ),
    },
    "regulated_qc": {
        "investigation": "QCOOSInvestigation",
        "certificate": "QCCertificate",
        "investigation_final": "released",
        "certificate_final": "released",
        "issued": "released",
        "title": "Certificate of analysis",
        "intended_use": None,
    },
}


def output(
    name: str, record: dict[str, Any], values: dict[str, Any], *creates: dict[str, Any]
) -> dict[str, Any]:
    """An update checks the pinned input revision before any creates are written."""
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {name: [{"entity": record["id"], "values": values, "require_unchanged": True}]},
        "creates": list(creates),
    }


def create(
    kind: str, values: dict[str, Any], *parents: dict[str, Any], key: str | None = None
) -> dict[str, Any]:
    return {
        "type": f"{PLUGIN}.{kind}",
        "values": values,
        "derived_from": [record["id"] for record in parents],
        **({"key": key} if key else {}),
    }


def included(record: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return record.get("included", {}).get(field) or []


def pinned(record: dict[str, Any], field: str) -> dict[str, Any]:
    given = included(record, field)
    if len(given) != 1 or given[0]["id"] != record["values"].get(field):
        raise ValueError(f"The case's {field} is missing from the pinned inputs.")
    return given[0]


def calendar_date(value: object) -> date:
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        raise ValueError("Give the testing and effective dates as YYYY-MM-DD.") from None
    if value != day.isoformat():
        raise ValueError("Give the testing and effective dates as YYYY-MM-DD.")
    return day


def number(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("An observed value or acceptance limit must be a finite number.") from None
    if not amount.is_finite():
        raise ValueError("An observed value or acceptance limit must be a finite number.")
    return amount


def kind(criterion: dict[str, Any]) -> str:
    """A criterion's kind of result: a number against limits, or conforms or not."""
    return str(criterion.get("kind") or "numeric")


def limit(criterion: dict[str, Any], name: str) -> Decimal | None:
    """A limit as its specification states it, significant to its last digit shown."""
    text = criterion.get(name)
    if text is None:
        return None
    if not isinstance(text, str) or not LIMIT.fullmatch(text):
        raise ValueError(
            f"State the {name} limit for {criterion.get('attribute')} as a decimal number, "
            "to its last significant place, such as 99.0."
        )
    return Decimal(text)


def places(amount: Decimal) -> int:
    exponent = amount.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def rounding(criterion: dict[str, Any]) -> str:
    rule = criterion.get("rounding") or "limit_places"
    if rule not in ROUNDING:
        raise ValueError(
            f"Compare {criterion.get('attribute')} rounded to its limits' places, or unrounded."
        )
    return str(rule)


def reportable(measured: Decimal, criterion: dict[str, Any]) -> Decimal:
    """The value a numeric criterion compares with its limits. USP General Notices 7.20
    rounds the final result to the limit expression's places, considering only the next
    digit: below 5 it's dropped, 5 or more rounds up. The limits are fixed, and the
    observed value is kept as measured (21 CFR 211.194(a)(5))."""
    if rounding(criterion) == "unrounded":
        return measured
    bounds = (limit(criterion, "lower"), limit(criterion, "upper"))
    stated = next(bound for bound in bounds if bound is not None)
    step = Decimal(1).scaleb(-places(stated))
    return measured.quantize(step, rounding=ROUND_HALF_UP, context=EXACT)


def criteria(version: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = version["values"].get("criteria") or []
    if not rows:
        raise ValueError("The specification must have at least one acceptance criterion.")
    by_attribute = {}
    for row in rows:
        attribute = row.get("attribute")
        if not all(str(row.get(field) or "").strip() for field in ("attribute", "method")):
            raise ValueError("Every criterion needs an attribute and a method.")
        if attribute in by_attribute:
            raise ValueError(f"The specification repeats the attribute {attribute}.")
        if kind(row) == "conforms":
            if not str(row.get("acceptance") or "").strip():
                raise ValueError(f"The specification needs the requirement for {attribute}.")
            if row.get("lower") is not None or row.get("upper") is not None:
                raise ValueError(f"{attribute} conforms to a requirement: it has no limits.")
            by_attribute[attribute] = row
            continue
        if not str(row.get("unit") or "").strip():
            raise ValueError(f"The specification needs a reporting unit for {attribute}.")
        lower, upper = limit(row, "lower"), limit(row, "upper")
        if lower is None and upper is None:
            raise ValueError(f"The specification needs an acceptance limit for {attribute}.")
        if lower is not None and upper is not None:
            if lower > upper:
                raise ValueError(f"The acceptance limits for {attribute} are reversed.")
            if rounding(row) == "limit_places" and places(lower) != places(upper):
                raise ValueError(
                    f"State both limits for {attribute} to the same decimal place, such as "
                    "98.0 and 102.0: a result is rounded to the limits' last place."
                )
        rounding(row)
        by_attribute[attribute] = row
    return by_attribute


def approved(version: dict[str, Any], schedule: dict[str, Any]) -> date:
    values = version["values"]
    if values.get("schedule") != schedule["id"]:
        raise ValueError("Choose a specification version that belongs to this material schedule.")
    if values.get("approval") != "approved":
        raise ValueError("Approve the specification version before publishing or using it.")
    return calendar_date(values.get("effective_from"))


def scheduled_versions(schedule: dict[str, Any]) -> list[tuple[date, dict[str, Any]]]:
    versions = included(schedule, "versions")
    if [version["id"] for version in versions] != (schedule["values"].get("versions") or []):
        raise ValueError("The specification schedule's pinned version list is incomplete.")
    dated = [(approved(version, schedule), version) for version in versions]
    if any(left[0] >= right[0] for left, right in pairwise(dated)):
        raise ValueError(
            "Published specification versions must have strictly increasing effective dates."
        )
    return dated


def successor(run: dict[str, Any], schedule: dict[str, Any], day: date, previous: date) -> None:
    """A successor starts after its predecessor and every pinned testing date, and today or
    earlier only as the deployment's policy allows."""
    policy = run.get("settings", {}).get("successor_effective_dates", "future")
    if policy not in SUCCESSOR_POLICIES:
        raise ValueError("Choose future, same_day or backdated for successor effective dates.")
    reason = str(run["params"].get("backdated_because") or "").strip()
    today = datetime.now(UTC).date()
    if day <= previous:
        raise ValueError("A successor version must start after its predecessor.")
    tested = schedule["values"].get("tested_through")
    if tested is not None and day <= calendar_date(tested):
        raise ValueError(
            f"A release case has already pinned the version in force on {tested}. "
            "A successor must start after that testing date."
        )
    if policy == "future" and day <= today:
        raise ValueError("A successor version must start on a future date after its predecessor.")
    if policy == "same_day" and day < today:
        raise ValueError("A successor version must start today or later, after its predecessor.")
    if day < today and not reason:
        raise ValueError(
            "Say why this version took effect in the past, such as importing an approved history."
        )
    if day >= today and reason:
        raise ValueError(
            "Leave the backdating reason empty for a version that starts today or later."
        )


def publish_version(run: dict[str, Any]) -> dict[str, Any]:
    schedule = run["inputs"]["schedule"][0]
    version = run["inputs"]["version"][0]
    dated = scheduled_versions(schedule)
    day = approved(version, schedule)
    criteria(version)
    ids = [row["id"] for _, row in dated]
    if version["id"] in ids:
        return output("schedule", schedule, {"versions": ids})
    if dated:
        successor(run, schedule, day, dated[-1][0])
    elif str(run["params"].get("backdated_because") or "").strip():
        raise ValueError("A schedule's first version needs no backdating reason.")
    return output("schedule", schedule, {"versions": [*ids, version["id"]]})


def named(material: dict[str, Any] | None, expected: object) -> str:
    """The material's name, which its certificate of analysis states (ICH Q7 §11.41)."""
    if material is None or material["id"] != expected:
        raise ValueError("The specification's material is missing from the pinned inputs.")
    name = str(material["values"].get("name") or "").strip()
    if not name:
        raise ValueError(
            f"Name {material['hrid']} on its record first: a certificate of analysis names "
            "the material it's for."
        )
    return name


def lot_material(lot: dict[str, Any]) -> object:
    """The material a lot is of, by its type's declared material field, as the run reads it."""
    field = lot.get("material_field")
    if field == "id":
        return lot["id"]
    return lot["values"].get(field) if field else None


def concluded(case: dict[str, Any], investigations: list[dict[str, Any]]) -> None:
    """A case left without a certificate is tested again only once every out-of-specification
    result in it has a signed conclusion: a new cycle never sidesteps an open investigation."""
    final_state = FLOWS[case["values"]["review_flow"]]["investigation_final"]
    results = {
        row["sequence"]
        for row in case["values"].get("attempts") or []
        if row["outcome"] == "oos" and row.get("purpose") != "investigational"
    }
    covered = set()
    for investigation in investigations:
        values = investigation["values"]
        test = next(iter(included(investigation, "test")), None)
        if (
            values.get("case") != case["id"]
            or test is None
            or test["id"] != values.get("test")
            or test["values"].get("case") != case["id"]
        ):
            raise ValueError("Each investigation must be of a result in the previous case.")
        if values.get("review") != final_state or not values.get("resolution"):
            raise ValueError(
                "Finish signing every investigation in the previous case before testing the "
                "lot again."
            )
        covered.add(test["values"].get("sequence"))
    if covered != results or len(investigations) != len(results):
        raise ValueError(
            "Include the signed investigation of every out-of-specification result in the "
            "previous case."
        )


def next_cycle(
    run: dict[str, Any], lot: dict[str, Any], schedule: dict[str, Any], testing_on: date
) -> dict[str, Any]:
    previous = optional_input(run, "previous")
    reason = str(run["params"].get("reason") or "").strip()
    investigations = run["inputs"].get("investigations") or []
    if previous is None:
        if reason or investigations:
            raise ValueError(
                "A lot's first release cycle has no previous case. Leave the reason and "
                "investigations empty."
            )
        return {"cycle": 1}
    values = previous["values"]
    if values.get("lot") != lot["id"] or values.get("schedule") != schedule["id"]:
        raise ValueError("The previous case must be this lot's, against this specification.")
    if values.get("superseded"):
        raise ValueError("A later cycle already follows that case. Choose the lot's latest case.")
    if not reason:
        raise ValueError("Say why the lot is tested again, such as its retest date or a rework.")
    if testing_on < calendar_date(values["testing_on"]):
        raise ValueError("A later cycle can't be tested before the cycle it follows.")
    if values.get("sealed"):
        if investigations:
            raise ValueError(
                "The previous case has its certificate. Leave the investigations empty."
            )
    else:
        concluded(previous, investigations)
    return {"cycle": (values.get("cycle") or 1) + 1, "previous": previous["id"], "reason": reason}


def open_release(run: dict[str, Any]) -> dict[str, Any]:
    schedule = run["inputs"]["schedule"][0]
    lot = run["inputs"]["lot"][0]
    material = schedule["values"].get("material")
    if not material or material != lot_material(lot):
        raise ValueError(
            "The specification schedule must be for the material registered on this lot."
        )
    named(next(iter(included(schedule, "material")), None), material)
    lot_number = lot["values"].get("lot_number")
    if not str(lot_number or "").strip():
        raise ValueError("Record the physical lot number before opening release testing.")
    testing_on = calendar_date(run["params"]["testing_on"])
    if testing_on > datetime.now(UTC).date():
        raise ValueError("The testing date cannot be in the future.")
    dated = scheduled_versions(schedule)
    version = next((row for day, row in reversed(dated) if day <= testing_on), None)
    if version is None:
        raise ValueError("No approved specification version was effective on the testing date.")
    criteria(version)
    cycle = next_cycle(run, lot, schedule, testing_on)
    flow = run.get("settings", {}).get("review_flow", "research")
    if flow not in FLOWS:
        raise ValueError("Choose Research or Regulated QC in the release testing settings.")
    values = {
        "lot": lot["id"],
        "lot_revision": lot["revision"],
        "lot_number": lot_number,
        "material": material,
        "schedule": schedule["id"],
        "schedule_revision": schedule["revision"],
        "context": schedule["values"].get("context") or "Release",
        **({"grade": grade} if (grade := schedule["values"].get("grade")) else {}),
        **cycle,
        "specification": version["id"],
        "specification_revision": version["revision"],
        "version": version["values"]["version"],
        "testing_on": testing_on.isoformat(),
        "review_flow": flow,
        "attempts": [],
        "sealed": False,
    }
    # Rewriting the versions takes and checks the schedule lock, so a competing append
    # cannot race a case whose selected version would no longer be in force. The latest
    # pinned testing date keeps any later successor after it.
    tested = schedule["values"].get("tested_through")
    latest = max(testing_on, calendar_date(tested)) if tested is not None else testing_on
    parents = [
        lot,
        schedule,
        *([previous] if (previous := optional_input(run, "previous")) else []),
    ]
    result = output(
        "schedule",
        schedule,
        {"versions": [row["id"] for _, row in dated], "tested_through": latest.isoformat()},
        create("ReleaseCase", values, *parents, *(run["inputs"].get("investigations") or [])),
    )
    if previous is not None:
        # Checked against the revision read: two retests can't both follow one cycle.
        result["updates"]["previous"] = [
            {"entity": previous["id"], "values": {"superseded": True}, "require_unchanged": True}
        ]
    return result


def check_case(case: dict[str, Any]) -> dict[str, Any]:
    if case["values"].get("superseded"):
        raise ValueError("A later release cycle follows this case. Record tests in that cycle.")
    if case["values"].get("sealed") is not False:
        raise ValueError("Use an open release case. This case is closed or incomplete.")
    specification = pinned(case, "specification")
    if (
        specification["revision"] != case["values"].get("specification_revision")
        or specification["values"].get("approval") != "approved"
    ):
        raise ValueError("The case must retain its approved specification revision.")
    return specification


def receipt(case: dict[str, Any], test: dict[str, Any], investigation: dict[str, Any]) -> None:
    values = investigation["values"]
    final_state = FLOWS[case["values"]["review_flow"]]["investigation_final"]
    if values.get("case") != case["id"] or values.get("test") != test["id"]:
        raise ValueError("The investigation must belong to this case and the original test.")
    if values.get("review") != final_state:
        raise ValueError("Finish signing the investigation before relying on its decision.")
    if values.get("resolution") == "inconclusive":
        raise ValueError(
            "An inconclusive investigation leaves the original out-of-specification result "
            "standing, which prevents a passing certificate."
        )
    if values.get("resolution") != "invalidated":
        raise ValueError("A confirmed out-of-specification result prevents a passing certificate.")
    if not str(values.get("findings") or "").strip():
        raise ValueError(
            "A signed invalidation must record why the original result was invalidated."
        )


def repeat(
    case: dict[str, Any],
    attribute: str,
    test: dict[str, Any] | None,
    investigation: dict[str, Any] | None,
) -> None:
    previous = latest_reportable(case, attribute)
    if previous is None:
        if test is not None or investigation is not None:
            raise ValueError(
                "This is the first attempt for the attribute. Leave repeat inputs empty."
            )
        return
    if previous["outcome"] != "oos":
        raise ValueError("This attribute already has a passing attempt.")
    if test is None or investigation is None:
        raise ValueError(
            "Select the most recent out-of-specification test and its signed invalidation."
        )
    values = test["values"]
    if values.get("case") != case["id"] or any(values.get(k) != v for k, v in previous.items()):
        raise ValueError("The repeat must cite the most recent out-of-specification attempt.")
    receipt(case, test, investigation)


def latest_reportable(case: dict[str, Any], attribute: str) -> dict[str, Any] | None:
    """The attribute's most recent reportable attempt: investigational tests replace nothing."""
    attempts = case["values"].get("attempts") or []
    return next(
        (
            row
            for row in reversed(attempts)
            if row["attribute"] == attribute and row.get("purpose") != "investigational"
        ),
        None,
    )


def under_plan(
    case: dict[str, Any],
    attribute: str,
    test: dict[str, Any] | None,
    investigation: dict[str, Any] | None,
) -> dict[str, Any]:
    """An investigational test is run under an open investigation's written plan, into the
    attribute's most recent reportable result, which is out of specification. It replaces
    nothing; only a signed invalidation permits a reportable repeat (FDA OOS guidance, §IV)."""
    if test is not None:
        raise ValueError("An investigational test repeats nothing. Leave the repeat empty.")
    previous = latest_reportable(case, attribute)
    if investigation is None or previous is None or previous["outcome"] != "oos":
        raise ValueError(
            "Run an investigational test under the open investigation of the attribute's "
            "out-of-specification result."
        )
    values = investigation["values"]
    investigated = next(iter(included(investigation, "test")), None)
    if (
        values.get("case") != case["id"]
        or investigated is None
        or investigated["id"] != values.get("test")
        or investigated["values"].get("case") != case["id"]
        or investigated["values"].get("sequence") != previous["sequence"]
    ):
        raise ValueError(
            "Choose the investigation of this attribute's most recent out-of-specification result."
        )
    if values.get("review") != "recorded":
        raise ValueError("The investigation has been submitted or signed: its testing is done.")
    if not str(values.get("plan") or "").strip():
        raise ValueError("Write the investigation's testing plan before testing under it.")
    return {
        "purpose": "investigational",
        "investigation": investigation["id"],
        "investigation_revision": investigation["revision"],
    }


def observation(
    case: dict[str, Any],
    params: dict[str, Any],
    assay: dict[str, Any] | None,
    criterion: dict[str, Any],
) -> tuple[int | float, dict[str, Any]]:
    if assay is None:
        if params.get("value") is None:
            raise ValueError("Enter an observed value or choose a signed plate assay result.")
        return params["value"], {}
    if params.get("value") is not None:
        raise ValueError("Leave the manual value empty when using a plate assay result.")
    values = assay["values"]
    flow = values.get("review_flow")
    state = {"research": "signed", "regulated_qc": "released"}.get(flow)
    if (
        state is None
        or values.get("review") != state
        or values.get("fit_status") != "fitted"
        or values.get("qualifier") != "within_range"
        or values.get("endpoint_value") is None
    ):
        raise ValueError("Choose a finally signed plate assay with a reportable endpoint in range.")
    if case["values"]["review_flow"] == "regulated_qc" and flow != "regulated_qc":
        raise ValueError("A regulated release requires a QA-released plate assay result.")
    if values.get("sample") != case["values"]["lot"]:
        raise ValueError("The plate assay must have measured this physical lot.")
    if (
        values.get("endpoint_kind") != params["attribute"]
        or values.get("concentration_unit") != params["unit"]
    ):
        raise ValueError("The assay's endpoint and unit must match the criterion.")
    # A specification names the fit method it accepts, by its name and its version (ADR
    # 0248): plate_assays' own 4PL-OLS/1, or a lab's method under its own name. A result
    # fitted another way, or by another version, isn't evidence for it.
    named = fit_method(criterion["method"])
    if named is None:
        raise ValueError(
            "Name the fit method and its version in the specification criterion, such as "
            "4PL-OLS/1, to release on a plate assay result."
        )
    if fit_method(values.get("fit_version")) != named:
        raise ValueError(
            f"The assay was fitted by {values.get('fit_version') or 'an unnamed method'}; "
            f"the specification accepts {named[0]} version {named[1]}."
        )
    for field in ("procedure", "procedure_revision"):
        expected = criterion.get(field)
        if not isinstance(expected, str) or not expected.strip() or values.get(field) != expected:
            raise ValueError(
                "The assay procedure and revision must match the specification criterion."
            )
    return values["endpoint_value"], {
        "evidence": assay["id"],
        "evidence_revision": assay["revision"],
    }


def fit_method(text: object) -> tuple[str, str] | None:
    """A fit method's name and version, as ``name/version`` states them, or None when either
    is missing: a bare version, or a name without one, names no method."""
    if not isinstance(text, str):
        return None
    name, _, version = text.strip().rpartition("/")
    return (name, version) if name.strip() and version.strip() else None


def optional_input(run: dict[str, Any], field: str) -> dict[str, Any] | None:
    return next(iter(run["inputs"].get(field) or []), None)


def record_test(run: dict[str, Any]) -> dict[str, Any]:
    case = run["inputs"]["case"][0]
    specification = check_case(case)
    case_values = case["values"]
    attempts = case_values.get("attempts") or []
    if len(attempts) >= MAX_ATTEMPTS:
        raise ValueError(f"A release case can hold at most {MAX_ATTEMPTS} attempts.")
    operator = run["run"].get("requested_by")
    if not operator:
        raise ValueError("Recording an observation requires an authenticated run requester.")
    params = run["params"]
    attribute, method, unit = params["attribute"], params["method"], params.get("unit")
    criterion = criteria(specification).get(attribute)
    if criterion is None:
        raise ValueError("Choose an attribute named by the pinned specification.")
    if criterion["method"] != method or (criterion.get("unit") or None) != (unit or None):
        raise ValueError("The observed unit and method must match the pinned specification.")
    assessor = optional_input(run, "assay")
    original = optional_input(run, "repeat_of")
    investigation = optional_input(run, "investigation")
    investigational = params.get("purpose") == "investigational"
    if investigational:
        purpose = under_plan(case, attribute, original, investigation)
    else:
        repeat(case, attribute, original, investigation)
        purpose = {"purpose": "reportable"}
    lower, upper = criterion.get("lower"), criterion.get("upper")
    typed: dict[str, Any] = {"kind": kind(criterion)}
    if typed["kind"] == "conforms":
        # A requirement, such as an identification test's: the analyst says whether what
        # they saw meets it. An assay's number or a value isn't such a result.
        if assessor is not None or params.get("value") is not None:
            raise ValueError(f"{attribute} conforms or doesn't: say which, without a value.")
        result = params.get("result")
        if result not in ("conforms", "does_not_conform"):
            raise ValueError(f"Say whether {attribute} conforms to its requirement.")
        is_outside = result == "does_not_conform"
        reading: int | float | None = None
        evidence: dict[str, Any] = {}
        typed |= {"result": result, "acceptance": criterion["acceptance"]}
    else:
        if params.get("result") is not None:
            raise ValueError(f"{attribute} is a number: give its observed value.")
        reading, evidence = observation(case, params, assessor, criterion)
        # The limits are inclusive and fixed. The reportable value is the one compared:
        # rounded to the limits' places, or as measured, and never averaged with repeats.
        compared = reportable(number(reading), criterion)
        low, high = limit(criterion, "lower"), limit(criterion, "upper")
        is_outside = (low is not None and compared < low) or (high is not None and compared > high)
        typed |= {"reported": format(compared, "f"), "rounding": rounding(criterion)}
    if seen := str(params.get("observation") or "").strip():
        typed["observation"] = seen
    attempt = {
        "sequence": len(attempts) + 1,
        "attribute": attribute,
        "outcome": "oos" if is_outside else "pass",
        **({"purpose": "investigational"} if investigational else {}),
    }
    values = {
        **attempt,
        "case": case["id"],
        "lot": case_values["lot"],
        "method": method,
        "unit": unit or None,
        "value": reading,
        "lower": lower,
        "upper": upper,
        "observed_by": operator,
        **typed,
        **evidence,
        **purpose,
    }
    if original is not None:
        values["repeat_of"] = original["id"]
    if report := params.get("lab_report"):
        if not run.get("files", {}).get(report, {}).get("size"):
            raise ValueError("The supplied laboratory report must be a nonempty run input file.")
        values["lab_report"] = report
    parents = [row for row in (case, assessor, original, investigation) if row is not None]
    creates = [create("ReleaseTest", values, *parents, key="measurement")]
    # The open investigation covers an investigational result outside the limits.
    if is_outside and not investigational:
        investigated = FLOWS[case_values["review_flow"]]["investigation"]
        creates.append(
            create(investigated, {"case": case["id"], "test": {"$create": "measurement"}}, *parents)
        )
    return output("case", case, {"attempts": [*attempts, attempt]}, *creates)


def pin_fields(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "hrid": record["hrid"],
        "revision": record["revision"],
        **{field: record["values"][field] for field in fields if field in record["values"]},
    }


def review_attempts(
    case: dict[str, Any],
    specification: dict[str, Any],
    tests: list[dict[str, Any]],
    investigations: list[dict[str, Any]],
) -> None:
    expected = case["values"].get("attempts") or []
    if len(tests) != len(expected):
        raise ValueError(
            "Include every attempt recorded for this case before preparing a certificate."
        )
    exceptions = {row["values"].get("test"): row for row in investigations}
    if len(exceptions) != len(investigations):
        raise ValueError("Include exactly one investigation for each out-of-specification attempt.")
    previous: dict[str, dict[str, Any]] = {}
    selected = {row["id"]: row for row in investigations}
    for test, attempt in zip(tests, expected, strict=True):
        values = test["values"]
        if (
            values.get("case") != case["id"]
            or values.get("lot") != case["values"]["lot"]
            or any(values.get(k) != v for k, v in attempt.items())
        ):
            raise ValueError("The selected attempts do not match this case's recorded evidence.")
        attribute = attempt["attribute"]
        prior = previous.get(attribute)
        if attempt.get("purpose") == "investigational":
            # It informs the investigation of the reportable result before it, which the
            # selection must hold, and stays out of the attribute's chain.
            held = selected.get(values.get("investigation"))
            if (
                prior is None
                or values.get("repeat_of") is not None
                or held is None
                or held["values"].get("test") != prior["id"]
            ):
                raise ValueError(
                    "Include the investigation every investigational test was run under."
                )
            continue
        if values.get("repeat_of") != (prior["id"] if prior else None) or (
            prior and prior["values"]["outcome"] != "oos"
        ):
            raise ValueError("The attempts do not form an attributable original and repeat chain.")
        if attempt["outcome"] == "oos":
            investigation = exceptions.pop(test["id"], None)
            if investigation is None:
                raise ValueError(
                    "Include the signed investigation for every out-of-specification attempt."
                )
            receipt(case, test, investigation)
        previous[attribute] = test
    if exceptions:
        raise ValueError(
            "The selected investigations include evidence outside this case's attempts."
        )
    passing = {
        attribute for attribute, test in previous.items() if test["values"]["outcome"] == "pass"
    }
    if passing != set(criteria(specification)):
        raise ValueError(
            "Every criterion needs a final passing attempt before preparing a certificate."
        )


#: A final result's fields the certificate states, where the test has them: its limits as
#: the specification states them, and its reportable value. The value as measured stays in
#: the evidence.
STATED = (
    "attribute",
    "method",
    "unit",
    "lower",
    "upper",
    "rounding",
    "reported",
    "kind",
    "acceptance",
    "result",
)


def statement(
    case: dict[str, Any],
    lot: dict[str, Any],
    specification: dict[str, Any],
    tests: list[dict[str, Any]],
    issuer: str | None,
) -> dict[str, Any]:
    """What the issued certificate states (ICH Q7 §11.41-11.43): its issuer, the material's
    name and grade, the lot and its dates, the specification, and each criterion's limits
    and final reportable result. Its issuing signature dates it. Failed attempts,
    investigations and analysts stay in the evidence."""
    values = case["values"]
    flow = FLOWS[values["review_flow"]]
    final: dict[str, dict[str, Any]] = {}
    for test in tests:
        if test["values"].get("purpose") != "investigational":
            final[test["values"]["attribute"]] = test
    lot_values = lot["values"]
    return {
        "format": "iki/coa/v2",
        "title": flow["title"],
        **({"intended_use": flow["intended_use"]} if flow["intended_use"] else {}),
        **({"issuer": issuer} if issuer else {}),
        "material": {
            # Read when the certificate was prepared, from the material's current revision.
            "name": named(pinned(case, "material"), values["material"]),
            "hrid": pinned(case, "material")["hrid"],
            **({"grade": values["grade"]} if values.get("grade") else {}),
        },
        "lot": {
            "lot_number": values["lot_number"],
            "hrid": lot["hrid"],
            # Read when the certificate was prepared, from the lot's current revision.
            "revision": lot["revision"],
            **{
                name: lot_values[name]
                for name in ("made_on", "retest_on", "expires_on")
                if lot_values.get(name)
            },
        },
        "specification": {
            # Which of the material's approved specifications applied: release or shelf life.
            **({"context": values["context"]} if values.get("context") else {}),
            "version": values["version"],
            "hrid": specification["hrid"],
            "revision": specification["revision"],
            "effective_from": specification["values"]["effective_from"],
            "source": specification["values"]["source"],
        },
        "testing_on": values["testing_on"],
        # The results as stated, without the records they came from: those stay in the
        # evidence, on the certificate record.
        "results": [
            {
                k: final[name]["values"][k]
                for k in STATED
                if final[name]["values"].get(k) is not None
            }
            for name in criteria(specification)
        ],
        # The certificate of analysis's own issuing state, not its record's.
        "issue": {"state": flow["issued"]},
    }


def digest(coa: dict[str, Any]) -> str:
    """The SHA-256 of a certificate's canonical JSON, which links its record to it."""
    text = json.dumps(coa, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def issue_certificate(run: dict[str, Any]) -> dict[str, Any]:
    """The certificate of analysis a finally signed certificate record states, signed on its
    own when it's issued: its export holds only what it states (ICH Q7 §11.41-11.43). The record
    keeps the evidence, and names the certificate by its digest."""
    record = run["inputs"]["certificate"][0]
    values = record["values"]
    flow = values.get("review_flow")
    coa = values.get("coa")
    if flow not in FLOWS or values.get("review") != FLOWS[flow]["certificate_final"]:
        raise ValueError("Finish signing the certificate record before issuing its certificate.")
    if not isinstance(coa, dict) or digest(coa) != values.get("coa_digest"):
        raise ValueError("The certificate record's certificate doesn't match its digest.")
    issued = {"coa": coa, "digest": values["coa_digest"], "review_flow": flow}
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [create("CertificateOfAnalysis", issued, record)],
    }


def prepare_certificate(run: dict[str, Any]) -> dict[str, Any]:
    case = run["inputs"]["case"][0]
    specification = check_case(case)
    lot = pinned(case, "lot")
    values = case["values"]
    tests = sorted(run["inputs"]["tests"], key=lambda row: row["values"].get("sequence") or 0)
    investigations = run["inputs"].get("investigations") or []
    review_attempts(case, specification, tests, investigations)
    sequence = {test["id"]: test["values"]["sequence"] for test in tests}
    ordered_investigations = sorted(investigations, key=lambda row: sequence[row["values"]["test"]])
    flow = values["review_flow"]
    issuer = str(run.get("settings", {}).get("certificate_issuer") or "").strip() or None
    if flow == "regulated_qc" and issuer is None:
        raise ValueError(
            "Set the certificate issuer in the release testing settings: a regulated "
            "certificate states its manufacturer's name, address and telephone."
        )
    report = {
        "format": "iki/coa-evidence/v1",
        "title": "Certificate evidence",
        "issue": {"state": FLOWS[flow]["certificate_final"]},
        "testing_on": values["testing_on"],
        "case": pin_fields(case, ()),
        "lot": {
            "id": lot["id"],
            "hrid": lot["hrid"],
            "revision": values["lot_revision"],
            "lot_number": values["lot_number"],
        },
        "material": pin_fields(pinned(case, "material"), ("name",)),
        "schedule": {"id": values["schedule"], "revision": values["schedule_revision"]},
        "specification": pin_fields(
            specification, ("version", "effective_from", "source", "criteria")
        ),
        "tests": [
            pin_fields(
                test,
                (
                    "sequence",
                    "attribute",
                    "method",
                    "kind",
                    "unit",
                    "value",
                    "reported",
                    "rounding",
                    "lower",
                    "upper",
                    "acceptance",
                    "result",
                    "observation",
                    "outcome",
                    "observed_by",
                    "repeat_of",
                    "purpose",
                    "investigation",
                    "investigation_revision",
                    "evidence",
                    "evidence_revision",
                    "lab_report",
                ),
            )
            for test in tests
        ],
        "investigations": [
            pin_fields(investigation, ("test", "resolution", "plan", "findings", "evidence"))
            for investigation in ordered_investigations
        ],
    }
    certificate = {
        "case": case["id"],
        "lot": lot["id"],
        "specification": specification["id"],
        "specification_revision": specification["revision"],
        "version": values["version"],
        "testing_on": values["testing_on"],
        "review_flow": values["review_flow"],
        "analysts": sorted({test["values"]["observed_by"] for test in tests}),
        "report": report,
        "coa": statement(case, lot, specification, tests, issuer),
    }
    certificate["coa_digest"] = digest(certificate["coa"])
    kind = FLOWS[values["review_flow"]]["certificate"]
    # The signed records above are permanently locked. A newly recorded test races this
    # seal on the case revision and either invalidates the preparation or is refused.
    return output(
        "case",
        case,
        {"sealed": True},
        create(kind, certificate, case),
    )


ACTIONS = {
    "publish_version": publish_version,
    "open_release": open_release,
    "record_test": record_test,
    "prepare_certificate": prepare_certificate,
    "issue_certificate": issue_certificate,
}


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        result = ACTIONS[sys.argv[1]](run)
        rendered = json.dumps(result, allow_nan=False)
    except ValueError as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from None
    Path(os.environ["IKI_OUTPUT"]).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()

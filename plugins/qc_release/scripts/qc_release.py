"""Select, evaluate and snapshot a lot's release evidence using pinned run inputs."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

PLUGIN = "iki/qc_release"
MAX_ATTEMPTS = 250
LOT_MATERIAL_FIELDS = ("of", "protein", "oligonucleotide", "conjugate", "polymer", "plasmid")
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


def criteria(version: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = version["values"].get("criteria") or []
    if not rows:
        raise ValueError("The specification must have at least one acceptance criterion.")
    by_attribute = {}
    for row in rows:
        attribute = row.get("attribute")
        if not all(str(row.get(field) or "").strip() for field in ("attribute", "method", "unit")):
            raise ValueError("Every criterion needs an attribute, a method and a unit.")
        if attribute in by_attribute:
            raise ValueError(f"The specification repeats the attribute {attribute}.")
        lower, upper = (
            number(row[name]) if row.get(name) is not None else None for name in ("lower", "upper")
        )
        if lower is None and upper is None:
            raise ValueError(f"The specification needs an acceptance limit for {attribute}.")
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(f"The acceptance limits for {attribute} are reversed.")
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


def publish_version(run: dict[str, Any]) -> dict[str, Any]:
    schedule = run["inputs"]["schedule"][0]
    version = run["inputs"]["version"][0]
    dated = scheduled_versions(schedule)
    day = approved(version, schedule)
    criteria(version)
    ids = [row["id"] for _, row in dated]
    if version["id"] in ids:
        return output("schedule", schedule, {"versions": ids})
    if dated and (day <= dated[-1][0] or day <= datetime.now(UTC).date()):
        raise ValueError("A successor version must start on a future date after its predecessor.")
    return output("schedule", schedule, {"versions": [*ids, version["id"]]})


def open_release(run: dict[str, Any]) -> dict[str, Any]:
    schedule = run["inputs"]["schedule"][0]
    lot = run["inputs"]["lot"][0]
    material = schedule["values"].get("material")
    if not material or material not in (lot["values"].get(field) for field in LOT_MATERIAL_FIELDS):
        raise ValueError(
            "The specification schedule must be for the material registered on this lot."
        )
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
    flow = run.get("settings", {}).get("review_flow", "research")
    if flow not in FLOWS:
        raise ValueError("Choose Research or Regulated QC in the release testing settings.")
    values = {
        "lot": lot["id"],
        "lot_revision": lot["revision"],
        "lot_number": lot_number,
        "schedule": schedule["id"],
        "schedule_revision": schedule["revision"],
        "specification": version["id"],
        "specification_revision": version["revision"],
        "version": version["values"]["version"],
        "testing_on": testing_on.isoformat(),
        "review_flow": flow,
        "attempts": [],
        "sealed": False,
    }
    # This no-op takes and checks the schedule lock, so a competing append cannot
    # race a case whose selected version would no longer be in force.
    return output(
        "schedule",
        schedule,
        {"versions": [row["id"] for _, row in dated]},
        create("ReleaseCase", values, lot, schedule),
    )


def check_case(case: dict[str, Any]) -> dict[str, Any]:
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
    attempts = case["values"].get("attempts") or []
    previous = next((row for row in reversed(attempts) if row["attribute"] == attribute), None)
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
        or values.get("fit_version") != params["method"]
    ):
        raise ValueError("The assay's endpoint, unit and fit method must match the criterion.")
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
    attribute, method, unit = (params[field] for field in ("attribute", "method", "unit"))
    criterion = criteria(specification).get(attribute)
    if criterion is None:
        raise ValueError("Choose an attribute named by the pinned specification.")
    if criterion["method"] != method or criterion["unit"] != unit:
        raise ValueError("The observed unit and method must match the pinned specification.")
    assessor = optional_input(run, "assay")
    original = optional_input(run, "repeat_of")
    investigation = optional_input(run, "investigation")
    repeat(case, attribute, original, investigation)
    reading, evidence = observation(case, params, assessor, criterion)
    measured = number(reading)
    lower, upper = criterion.get("lower"), criterion.get("upper")
    # Stored specification bounds are inclusive. Compare the supplied value directly,
    # without adding a tolerance, rounding across a boundary or averaging repeats.
    is_outside = (lower is not None and measured < number(lower)) or (
        upper is not None and measured > number(upper)
    )
    attempt = {
        "sequence": len(attempts) + 1,
        "attribute": attribute,
        "outcome": "oos" if is_outside else "pass",
    }
    values = {
        **attempt,
        "case": case["id"],
        "lot": case_values["lot"],
        "method": method,
        "unit": unit,
        "value": reading,
        "lower": lower,
        "upper": upper,
        "observed_by": operator,
        **evidence,
    }
    if original is not None:
        values["repeat_of"] = original["id"]
    if report := params.get("lab_report"):
        if not run.get("files", {}).get(report, {}).get("size"):
            raise ValueError("The supplied laboratory report must be a nonempty run input file.")
        values["lab_report"] = report
    parents = [row for row in (case, assessor, original, investigation) if row is not None]
    creates = [create("ReleaseTest", values, *parents, key="measurement")]
    if is_outside:
        kind = FLOWS[case_values["review_flow"]]["investigation"]
        creates.append(
            create(kind, {"case": case["id"], "test": {"$create": "measurement"}}, *parents)
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


#: A final result's fields the certificate states, where the test has them.
STATED = ("attribute", "method", "unit", "lower", "upper", "value", "kind", "acceptance", "result")


def statement(
    case: dict[str, Any],
    lot: dict[str, Any],
    specification: dict[str, Any],
    tests: list[dict[str, Any]],
    issuer: str | None,
) -> dict[str, Any]:
    """What the issued certificate states (ICH Q7 §11.4): its issuer, the lot and its
    dates, the specification, and each criterion's limits and final reportable result.
    Failed attempts, investigations and analysts stay in the evidence."""
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
    own when it's issued: its export holds only what it states (ICH Q7 §11.4). The record
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
                    "unit",
                    "value",
                    "lower",
                    "upper",
                    "outcome",
                    "observed_by",
                    "repeat_of",
                    "evidence",
                    "evidence_revision",
                    "lab_report",
                ),
            )
            for test in tests
        ],
        "investigations": [
            pin_fields(investigation, ("test", "resolution", "findings", "evidence"))
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

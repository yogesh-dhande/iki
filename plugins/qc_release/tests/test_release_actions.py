"""Exercise repeat authorization and certificate completeness with synthetic observations."""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def qc_actions(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    return importlib.import_module("qc_release")


def record(identity: str, revision: int, **values: Any) -> dict[str, Any]:
    return {"id": identity, "hrid": identity.upper(), "revision": revision, "values": values}


def inputs() -> dict[str, Any]:
    specification = record(
        "spec1",
        2,
        approval="approved",
        version="v1",
        effective_from="2020-01-01",
        source="Synthetic specification used for testing the software",
        criteria=[
            {
                "attribute": "Assay",
                "method": "Synthetic example",
                "unit": "AU",
                "lower": "90",
                "upper": "110",
            },
        ],
    )
    lot = record("lot1", 8, lot_number="Corrected after testing", retest_on="2023-04-01")
    material = record("material1", 4, name="Synthetic assay standard")
    case = record(
        "case1",
        1,
        lot=lot["id"],
        lot_revision=7,
        lot_number="Synthetic lot 1",
        material=material["id"],
        schedule="schedule1",
        schedule_revision=3,
        specification=specification["id"],
        specification_revision=specification["revision"],
        version="v1",
        testing_on="2021-04-02",
        review_flow="regulated_qc",
        attempts=[],
        sealed=False,
    )
    case["included"] = {"specification": [specification], "lot": [lot], "material": [material]}
    return {
        "run": {"requested_by": "analyst1"},
        "inputs": {"case": [case]},
        "params": {"attribute": "Assay", "method": "Synthetic example", "unit": "AU", "value": 89},
    }


def start_oos(qc_actions: ModuleType) -> tuple[dict[str, Any], dict[str, Any]]:
    run = inputs()
    output = qc_actions.record_test(run)
    test, investigation = output["creates"]
    assert test["values"]["value"] == 89
    assert test["values"]["outcome"] == "oos"
    assert investigation["type"] == "iki/qc_release.QCOOSInvestigation"
    assert investigation["values"]["test"] == {"$create": test["key"]}
    (update,) = output["updates"]["case"]
    assert update["require_unchanged"]
    run["inputs"]["case"][0]["values"].update(update["values"])
    original = record("test1", 1, **test["values"])
    run["inputs"]["repeat_of"] = [original]
    run["params"]["value"] = 90
    return run, original


def investigation_for(original: dict[str, Any], review: str, resolution: str) -> dict[str, Any]:
    return record(
        "inv1",
        6,
        case=original["values"]["case"],
        test=original["id"],
        review=review,
        resolution=resolution,
        findings="Synthetic proof of an instrument fault established an assignable cause.",
        evidence="file1",
    )


@pytest.mark.parametrize(
    ("review", "resolution", "message"),
    [
        ("reviewed", "invalidated", "Finish signing the investigation"),
        ("released", "confirmed", "A confirmed out-of-specification result"),
        ("released", "inconclusive", "An inconclusive investigation leaves the original"),
    ],
)
def test_repeat_requires_final_signed_invalidation(
    qc_actions: ModuleType, review: str, resolution: str, message: str
) -> None:
    run, original = start_oos(qc_actions)
    run["inputs"]["investigation"] = [investigation_for(original, review, resolution)]
    with pytest.raises(ValueError, match=message):
        qc_actions.record_test(run)


def test_certificate_preserves_original_and_ordered_retest(qc_actions: ModuleType) -> None:
    run, original = start_oos(qc_actions)
    investigation = investigation_for(original, "released", "invalidated")
    run["inputs"]["investigation"] = [investigation]
    output = qc_actions.record_test(run)
    (recorded_repeat,) = output["creates"]
    assert recorded_repeat["values"]["repeat_of"] == original["id"]
    assert recorded_repeat["values"]["outcome"] == "pass"
    (update,) = output["updates"]["case"]
    case = run["inputs"]["case"][0]
    case["values"].update(update["values"])
    repeat = record("test2", 1, **recorded_repeat["values"])
    prepare: dict[str, Any] = {
        "inputs": {"case": [case], "tests": [repeat, original], "investigations": [investigation]},
        "settings": {"certificate_issuer": "Synthetic Labs, 1 Example Way · +1 555 0100"},
    }

    omitted = deepcopy(prepare)
    omitted["inputs"]["tests"] = [repeat]
    with pytest.raises(ValueError, match="every attempt"):
        qc_actions.prepare_certificate(omitted)

    confirmed = deepcopy(prepare)
    confirmed["inputs"]["investigations"][0]["values"]["resolution"] = "confirmed"
    with pytest.raises(ValueError, match="A confirmed out-of-specification result"):
        qc_actions.prepare_certificate(confirmed)

    anonymous = deepcopy(prepare)
    anonymous["settings"]["certificate_issuer"] = "  "
    with pytest.raises(ValueError, match="Set the certificate issuer"):
        qc_actions.prepare_certificate(anonymous)

    result = qc_actions.prepare_certificate(prepare)
    (certificate,) = result["creates"]
    assert certificate["type"] == "iki/qc_release.QCCertificate"
    assert certificate["values"]["analysts"] == ["analyst1"]
    report = certificate["values"]["report"]
    assert report["specification"]["revision"] == 2
    assert report["lot"]["revision"] == 7
    assert report["lot"]["lot_number"] == "Synthetic lot 1"
    assert [(test["id"], test["value"]) for test in report["tests"]] == [
        ("test1", 89),
        ("test2", 90),
    ]
    assert report["investigations"][0]["revision"] == 6
    assert report["investigations"][0]["findings"] == investigation["values"]["findings"]
    assert report["issue"] == {"state": "released"}
    # The certificate states the final result only, its issuer, and the lot's dates.
    coa = certificate["values"]["coa"]
    assert coa["issuer"] == prepare["settings"]["certificate_issuer"]
    assert coa["title"] == "Certificate of analysis"
    assert coa["lot"] == {
        "lot_number": "Synthetic lot 1",
        "hrid": "LOT1",
        "revision": 8,
        "retest_on": "2023-04-01",
    }
    # It names the material, read when the certificate was prepared (ICH Q7 §11.41).
    assert coa["material"] == {"name": "Synthetic assay standard", "hrid": "MATERIAL1"}
    assert report["material"] == {
        "id": "material1",
        "hrid": "MATERIAL1",
        "revision": 4,
        "name": "Synthetic assay standard",
    }
    assert coa["results"] == [
        {
            "attribute": "Assay",
            "method": "Synthetic example",
            "unit": "AU",
            "lower": "90",
            "upper": "110",
            "rounding": "limit_places",
            "reported": "90",
            "kind": "numeric",
        }
    ]
    assert {"analysts", "investigations", "tests"}.isdisjoint(coa)
    sealed = result["updates"]["case"][0]
    assert sealed["require_unchanged"]
    case["values"].update(sealed["values"])
    with pytest.raises(ValueError, match="closed"):
        qc_actions.record_test(run)
    with pytest.raises(ValueError, match="closed"):
        qc_actions.prepare_certificate(prepare)


@pytest.mark.parametrize(
    ("specified", "fitted", "refused"),
    [
        # plate_assays' own method, by its name and version (ADR 0248).
        ("4PL-OLS/1", "4PL-OLS/1", None),
        # Another version of the same method is another method.
        ("4PL-OLS/1", "4PL-OLS/2", "accepts 4PL-OLS version 1"),
        # A lab's own method counts only where the specification names it.
        ("4PL-OLS/1", "acme/5PL-WLS/1", "fitted by acme/5PL-WLS/1"),
        ("acme/5PL-WLS/1", "acme/5PL-WLS/1", None),
        ("acme/5PL-WLS/1", "4PL-OLS/1", "accepts acme/5PL-WLS version 1"),
        # Its version alone, or its name alone, names no method, whatever the result says.
        ("1", "1", "Name the fit method and its version"),
        ("4PL-OLS", "4PL-OLS", "Name the fit method and its version"),
        ("4PL-OLS/1", None, "an unnamed method"),
    ],
)
def test_a_release_test_counts_a_fit_by_the_method_its_specification_names(
    qc_actions: ModuleType, specified: str, fitted: str | None, refused: str | None
) -> None:
    run = inputs()
    case = run["inputs"]["case"][0]
    criterion = case["included"]["specification"][0]["values"]["criteria"][0]
    criterion.update(
        attribute="IC50",
        method=specified,
        unit="nM",
        procedure="SOP-A",
        procedure_revision="2",
    )
    run["params"] = {"attribute": "IC50", "method": specified, "unit": "nM"}
    run["inputs"]["assay"] = [
        record(
            "assay1",
            4,
            review="released",
            review_flow="regulated_qc",
            sample="lot1",
            fit_status="fitted",
            qualifier="within_range",
            endpoint_kind="IC50",
            endpoint_value=95,
            concentration_unit="nM",
            procedure="SOP-A",
            procedure_revision="2",
            **({"fit_version": fitted} if fitted else {}),
        )
    ]
    if refused:
        with pytest.raises(ValueError, match=refused):
            qc_actions.record_test(run)
        return
    (measurement, *_) = qc_actions.record_test(run)["creates"]
    assert measurement["values"]["method"] == specified
    assert measurement["values"]["evidence"] == "assay1"


def test_plate_fit_cannot_be_transcribed_to_a_different_physical_lot(
    qc_actions: ModuleType,
) -> None:
    run = inputs()
    case = run["inputs"]["case"][0]
    criterion = case["included"]["specification"][0]["values"]["criteria"][0]
    criterion.update(attribute="IC50", method="4PL-OLS/1", unit="nM")
    run["params"] = {"attribute": "IC50", "method": "4PL-OLS/1", "unit": "nM"}
    assay = record(
        "assay1",
        4,
        review="released",
        review_flow="regulated_qc",
        sample="lot2",
        fit_status="fitted",
        qualifier="within_range",
        endpoint_kind="IC50",
        endpoint_value=95,
        concentration_unit="nM",
        fit_version="4PL-OLS/1",
    )
    run["inputs"]["assay"] = [assay]
    with pytest.raises(ValueError, match="this physical lot"):
        qc_actions.record_test(run)

    assay["values"]["sample"] = "lot1"
    criterion.update(procedure="SOP-A", procedure_revision="2")
    assay["values"].update(procedure="SOP-B", procedure_revision="2")
    with pytest.raises(ValueError, match="procedure"):
        qc_actions.record_test(run)
    assay["values"].update(procedure="SOP-A", procedure_revision="1")
    with pytest.raises(ValueError, match="procedure"):
        qc_actions.record_test(run)
    del assay["values"]["procedure_revision"]
    with pytest.raises(ValueError, match="procedure"):
        qc_actions.record_test(run)
    del criterion["procedure_revision"]
    with pytest.raises(ValueError, match="procedure"):
        qc_actions.record_test(run)
    assay["values"]["procedure_revision"] = criterion["procedure_revision"] = "2"
    (measurement,) = qc_actions.record_test(run)["creates"]
    assert measurement["values"]["evidence"] == "assay1"
    assert measurement["values"]["evidence_revision"] == 4
    assert measurement["values"]["value"] == 95


def test_lab_report_must_name_nonempty_run_input(qc_actions: ModuleType) -> None:
    run = inputs()
    run["params"].update(value=100, lab_report="report1")
    run["files"] = {"report1": {"size": 0}}
    with pytest.raises(ValueError, match="nonempty run input file"):
        qc_actions.record_test(run)

    run["files"]["report1"]["size"] = 100
    (measurement,) = qc_actions.record_test(run)["creates"]
    assert measurement["values"]["lab_report"] == "report1"


def opening(qc_actions: ModuleType, **more: Any) -> dict[str, Any]:
    """A run of open_release for a GenericLot of material1, under the Release schedule."""
    version = record(
        "spec1",
        2,
        schedule="schedule1",
        approval="approved",
        version="v1",
        effective_from="2020-01-01",
        source="Synthetic specification used for testing the software",
        criteria=inputs()["inputs"]["case"][0]["included"]["specification"][0]["values"][
            "criteria"
        ],
    )
    schedule = record("schedule1", 3, material="material1", context="Release", versions=["spec1"])
    material = record("material1", 4, name="Synthetic assay standard")
    schedule["included"] = {"versions": [version], "material": [material]}
    lot = record("lot1", 8, of="material1", lot_number="Synthetic lot 1")
    lot["roles"] = {"material": "of"}
    return {
        "run": {"requested_by": "analyst1"},
        "settings": {"review_flow": "regulated_qc"},
        "inputs": {"lot": [lot], "schedule": [schedule]} | more.pop("inputs", {}),
        "params": {"testing_on": "2021-04-02"} | more,
    }


def test_a_lot_is_matched_to_its_specification_by_its_declared_material(
    qc_actions: ModuleType,
) -> None:
    run = opening(qc_actions)
    (case,) = qc_actions.open_release(run)["creates"]
    assert case["values"] | {"cycle": 1, "context": "Release"} == case["values"]
    # A strain isolate is its own material: its schedule names the isolate.
    isolate = run["inputs"]["lot"][0] | {"id": "material1", "roles": {"material": "id"}}
    qc_actions.open_release(run | {"inputs": run["inputs"] | {"lot": [isolate]}})
    # A protein lot reads its protein, not a field of the same name on another lot type.
    other = run["inputs"]["lot"][0] | {"roles": {"material": "protein"}}
    with pytest.raises(ValueError, match="material registered on this lot"):
        qc_actions.open_release(run | {"inputs": run["inputs"] | {"lot": [other]}})


def test_a_lot_is_tested_again_in_a_new_cycle_that_keeps_the_last(qc_actions: ModuleType) -> None:
    """A retest or a rework is a new case after the lot's latest one. That case keeps its
    attempts and decisions and takes no more tests. A case without a certificate is
    followed only once every OOS result in it has a signed conclusion."""
    first = qc_actions.open_release(opening(qc_actions))["creates"][0]["values"]
    with pytest.raises(ValueError, match="first release cycle has no previous case"):
        qc_actions.open_release(opening(qc_actions, reason="Retest date"))

    run = inputs()
    case = run["inputs"]["case"][0]
    case["values"] |= {"cycle": 1, "context": "Release"}
    output = qc_actions.record_test(run)
    original = record("test1", 1, **output["creates"][0]["values"])
    case["values"].update(output["updates"]["case"][0]["values"])
    previous = {k: case[k] for k in ("id", "hrid", "revision", "values")}
    assert first["lot"] == previous["values"]["lot"]
    retest = opening(qc_actions, inputs={"previous": [previous]}, testing_on="2022-04-02")

    with pytest.raises(ValueError, match="Say why"):
        qc_actions.open_release(retest)
    retest["params"]["reason"] = "Synthetic: reprocessed by recrystallization under deviation D-1."
    with pytest.raises(ValueError, match="signed investigation of every"):
        qc_actions.open_release(retest)
    investigation = investigation_for(original, "submitted", "confirmed")
    investigation["included"] = {"test": [original]}
    retest["inputs"]["investigations"] = [investigation]
    with pytest.raises(ValueError, match="Finish signing every investigation"):
        qc_actions.open_release(retest)
    investigation["values"]["review"] = "released"
    early = deepcopy(retest)
    early["params"]["testing_on"] = "2021-04-01"
    with pytest.raises(ValueError, match="before the cycle it follows"):
        qc_actions.open_release(early)

    output = qc_actions.open_release(retest)
    (second,) = output["creates"]
    assert second["values"]["cycle"] == 2
    assert second["values"]["previous"] == "case1"
    assert second["values"]["reason"] == retest["params"]["reason"]
    assert set(second["derived_from"]) >= {"case1", "inv1"}
    (closed,) = output["updates"]["previous"]
    assert closed == {"entity": "case1", "values": {"superseded": True}, "require_unchanged": True}

    case["values"].update(closed["values"])
    with pytest.raises(ValueError, match="later release cycle follows this case"):
        qc_actions.record_test(run)
    with pytest.raises(ValueError, match="already follows that case"):
        qc_actions.open_release(retest)


def test_an_investigation_tests_under_its_plan_and_replaces_nothing(
    qc_actions: ModuleType,
) -> None:
    """FDA's OOS guidance separates testing during an investigation from the decision to
    replace the original result (§IV.B). Each investigational result is kept in sequence and
    pinned to the plan's revision; only a signed invalidation permits the reportable repeat."""
    run, original = start_oos(qc_actions)
    investigation = investigation_for(original, "recorded", "invalidated")
    investigation["values"].pop("resolution")
    investigation["included"] = {"test": [original]}
    trial = deepcopy(run)
    trial["inputs"].pop("repeat_of")
    trial["inputs"]["investigation"] = [investigation]
    trial["params"] = trial["params"] | {"purpose": "investigational", "value": 88}

    with pytest.raises(ValueError, match="testing plan"):
        qc_actions.record_test(trial)
    plan = "Synthetic plan: retest the original preparation twice on a second titrator."
    investigation["values"]["plan"] = plan
    with pytest.raises(ValueError, match="repeats nothing"):
        qc_actions.record_test(trial | {"inputs": trial["inputs"] | {"repeat_of": [original]}})

    output = qc_actions.record_test(trial)
    # Outside the limits too, it opens no second investigation: the open one covers it.
    (retest,) = output["creates"]
    assert retest["values"]["outcome"] == "oos"
    assert {
        k: retest["values"][k] for k in ("purpose", "investigation", "investigation_revision")
    } == {
        "purpose": "investigational",
        "investigation": "inv1",
        "investigation_revision": 6,
    }
    assert "repeat_of" not in retest["values"]
    (update,) = output["updates"]["case"]
    case = run["inputs"]["case"][0]
    case["values"].update(update["values"])
    assert case["values"]["attempts"][-1] == {
        "sequence": 2,
        "attribute": "Assay",
        "outcome": "oos",
        "purpose": "investigational",
    }

    # A signed investigation tests no more.
    investigation["values"]["review"] = "submitted"
    with pytest.raises(ValueError, match="testing is done"):
        qc_actions.record_test(trial | {"inputs": trial["inputs"] | {"case": [case]}})

    # The reportable repeat still needs the signed invalidation, and still cites the
    # original, not the investigational result.
    investigation["values"] |= {"review": "released", "resolution": "invalidated"}
    run["inputs"]["investigation"] = [investigation]
    output = qc_actions.record_test(run)
    (repeat,) = output["creates"]
    assert repeat["values"]["repeat_of"] == original["id"]
    assert repeat["values"]["purpose"] == "reportable"
    case["values"].update(output["updates"]["case"][0]["values"])

    tests = [
        original,
        record("test2", 1, **retest["values"]),
        record("test3", 1, **repeat["values"]),
    ]
    prepare: dict[str, Any] = {
        "inputs": {"case": [case], "tests": tests, "investigations": [investigation]},
        "settings": {"certificate_issuer": "Synthetic Labs, 1 Example Way · +1 555 0100"},
    }
    with pytest.raises(ValueError, match="every attempt"):
        qc_actions.prepare_certificate(
            prepare | {"inputs": prepare["inputs"] | {"tests": tests[::2]}}
        )
    (certificate,) = qc_actions.prepare_certificate(prepare)["creates"]
    report = certificate["values"]["report"]
    assert [t["purpose"] for t in report["tests"]] == [
        "reportable",
        "investigational",
        "reportable",
    ]
    assert report["tests"][1]["investigation_revision"] == 6
    assert report["investigations"][0]["plan"] == plan
    # The certificate states the reportable repeat, not the investigation's own test.
    stated = certificate["values"]["coa"]["results"]
    assert [row["reported"] for row in stated] == [tests[2]["values"]["reported"]]


#: USP Sodium Chloride's Identification A, a test for sodium under USP <191>, whose
#: requirement is met or not rather than a number (ICH Q6A §3.2.1 b, Identification).
IDENTIFICATION = {
    "attribute": "Identification A (sodium)",
    "method": "USP <191> Identification Tests—General, Sodium",
    "kind": "conforms",
    "acceptance": "Meets the requirements",
}


def test_an_identification_conforms_to_its_requirement_or_is_out_of_specification(
    qc_actions: ModuleType,
) -> None:
    run = inputs()
    case = run["inputs"]["case"][0]
    specification = case["included"]["specification"][0]
    specification["values"]["criteria"].append(IDENTIFICATION)
    identify = {k: IDENTIFICATION[k] for k in ("attribute", "method")}

    def attempt(**params: Any) -> dict[str, Any]:
        trial = deepcopy(run)
        trial["params"] = identify | params
        output: dict[str, Any] = qc_actions.record_test(trial)
        return output

    # It conforms or doesn't: neither a value nor a missing answer is a result.
    with pytest.raises(ValueError, match="without a value"):
        attempt(value=1.0, result="conforms")
    with pytest.raises(ValueError, match="Say whether"):
        attempt()
    with pytest.raises(ValueError, match="is a number"):
        qc_actions.record_test(run | {"params": run["params"] | {"result": "conforms"}})

    seen = "Synthetic: yellow flame, as the sodium test describes."
    (test,) = attempt(result="conforms", observation=seen)["creates"]
    assert test["values"] | {"observed_by": None} == test["values"] | {
        "kind": "conforms",
        "result": "conforms",
        "acceptance": "Meets the requirements",
        "observation": seen,
        "outcome": "pass",
        "value": None,
        "unit": None,
        "observed_by": None,
    }
    failed = attempt(result="does_not_conform", observation="Synthetic: no yellow flame.")
    original, investigation = failed["creates"]
    assert original["values"]["outcome"] == "oos"
    assert investigation["type"] == "iki/qc_release.QCOOSInvestigation"

    # A numeric result keeps its kind, and a certificate pins both kinds of result.
    run["params"] = run["params"] | {"value": 100}
    (assay,) = qc_actions.record_test(run)["creates"]
    assert assay["values"]["kind"] == "numeric"
    case["values"]["attempts"] = [
        {"sequence": 1, "attribute": "Assay", "outcome": "pass"},
        {"sequence": 2, "attribute": IDENTIFICATION["attribute"], "outcome": "pass"},
    ]
    tests = [
        record("test1", 1, **assay["values"] | {"sequence": 1}),
        record("test2", 1, **test["values"] | {"sequence": 2}),
    ]
    prepared = qc_actions.prepare_certificate(
        {
            "inputs": {"case": [case], "tests": tests},
            "settings": {"certificate_issuer": "Synthetic Labs, 1 Example Way · +1 555 0100"},
        }
    )
    (certificate,) = prepared["creates"]
    # What the certificate states: a number, and a requirement it conforms to.
    stated = {row["attribute"]: row for row in certificate["values"]["coa"]["results"]}
    assert (stated["Assay"]["reported"], "value" in stated["Assay"]) == ("100", False)
    assert stated[IDENTIFICATION["attribute"]]["result"] == "conforms"
    assert stated[IDENTIFICATION["attribute"]]["acceptance"] == "Meets the requirements"
    assert "value" not in stated[IDENTIFICATION["attribute"]]
    pinned = {t["attribute"]: t for t in certificate["values"]["report"]["tests"]}
    assert pinned[IDENTIFICATION["attribute"]]["result"] == "conforms"
    assert pinned[IDENTIFICATION["attribute"]]["acceptance"] == "Meets the requirements"
    assert pinned["Assay"]["value"] == 100


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"acceptance": None}, "requirement"),
        ({"upper": 1}, "no limits"),
        ({"kind": "numeric", "acceptance": None}, "reporting unit"),
    ],
)
def test_a_criterion_is_a_number_with_limits_or_a_requirement(
    qc_actions: ModuleType, change: dict[str, Any], why: str
) -> None:
    version = {"values": {"criteria": [IDENTIFICATION | change]}}
    with pytest.raises(ValueError, match=why):
        qc_actions.criteria(version)


def test_a_research_certificate_says_what_it_is_for(qc_actions: ModuleType) -> None:
    run = inputs()
    case = run["inputs"]["case"][0]
    case["values"]["review_flow"] = "research"
    run["params"]["value"] = 100
    output = qc_actions.record_test(run)
    case["values"].update(output["updates"]["case"][0]["values"])
    test = record("test1", 1, **output["creates"][0]["values"])
    # No issuer is needed for one reviewer's research sign-off.
    prepared = qc_actions.prepare_certificate({"inputs": {"case": [case], "tests": [test]}})
    (certificate,) = prepared["creates"]
    assert certificate["type"] == "iki/qc_release.ResearchCertificate"
    coa = certificate["values"]["coa"]
    assert coa["title"] == "Research certificate of analysis"
    assert "not released by a quality unit" in coa["intended_use"]
    assert "issuer" not in coa
    assert coa["issue"] == {"state": "issued"}


def test_a_certificate_of_analysis_is_issued_from_its_signed_record_alone(
    qc_actions: ModuleType,
) -> None:
    """The certificate of analysis holds only what its record says it states, so its own
    signed export carries nothing of the evidence. The record names it by its digest."""
    run, original = start_oos(qc_actions)
    investigation = investigation_for(original, "released", "invalidated")
    run["inputs"]["investigation"] = [investigation]
    output = qc_actions.record_test(run)
    case = run["inputs"]["case"][0]
    case["values"].update(output["updates"]["case"][0]["values"])
    repeat = record("test2", 1, **output["creates"][0]["values"])
    prepared = qc_actions.prepare_certificate(
        {
            "inputs": {
                "case": [case],
                "tests": [repeat, original],
                "investigations": [investigation],
            },
            "settings": {"certificate_issuer": "Synthetic Labs, 1 Example Way · +1 555 0100"},
        }
    )
    values = prepared["creates"][0]["values"]
    certificate = record("cert1", 4, **values)

    with pytest.raises(ValueError, match="Finish signing the certificate record"):
        qc_actions.issue_certificate({"inputs": {"certificate": [certificate]}})
    certificate["values"]["review"] = "released"
    tampered = deepcopy(certificate)
    tampered["values"]["coa"]["results"][0]["reported"] = "101"
    with pytest.raises(ValueError, match="doesn't match its digest"):
        qc_actions.issue_certificate({"inputs": {"certificate": [tampered]}})

    (issued,) = qc_actions.issue_certificate({"inputs": {"certificate": [certificate]}})["creates"]
    assert issued["type"] == "iki/qc_release.CertificateOfAnalysis"
    assert issued["derived_from"] == ["cert1"]
    assert issued["values"] == {
        "coa": values["coa"],
        "digest": values["coa_digest"],
        "review_flow": "regulated_qc",
    }
    shared = json.dumps(issued["values"])
    evidence = ["test1", "TEST1", "test2", "inv1", "INV1", "analyst1", '": 89', "Synthetic proof"]
    assert [text for text in evidence if text in shared] == []


def schedule_with(qc_actions: ModuleType, first: date, **schedule: Any) -> dict[str, Any]:
    """A schedule whose one published version took effect on ``first``. Its criterion and
    source are synthetic."""
    version = record(
        "v1",
        1,
        schedule="sched1",
        version="1",
        effective_from=first.isoformat(),
        approval="approved",
        source="Synthetic specification",
        criteria=[{"attribute": "Assay", "method": "Synthetic", "unit": "AU", "lower": "1"}],
    )
    found = record("sched1", 2, material="material1", versions=["v1"], **schedule)
    material = record("material1", 1, name="Synthetic material")
    found["included"] = {"versions": [version], "material": [material]}
    return found


def publish(
    qc_actions: ModuleType,
    schedule: dict[str, Any],
    starts: date,
    policy: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    version = deepcopy(schedule["included"]["versions"][0])
    version.update(id="v2", values={**version["values"], "version": "2"})
    version["values"]["effective_from"] = starts.isoformat()
    run: dict[str, Any] = {
        "inputs": {"schedule": [schedule], "version": [version]},
        "params": {"backdated_because": reason} if reason else {},
        "settings": {"successor_effective_dates": policy} if policy else {},
    }
    result: dict[str, Any] = qc_actions.publish_version(run)
    return result


TODAY = datetime.now(UTC).date()
LONG_AGO = TODAY - timedelta(days=400)


@pytest.mark.parametrize(
    ("policy", "starts", "reason", "refused"),
    [
        # The incumbent policy: a successor starts after today.
        (None, TODAY + timedelta(days=1), None, None),
        (None, TODAY, None, "future date"),
        ("future", TODAY, None, "future date"),
        ("same_day", TODAY, None, None),
        ("same_day", TODAY - timedelta(days=1), None, "today or later"),
        ("backdated", TODAY - timedelta(days=30), None, "Say why"),
        ("backdated", TODAY - timedelta(days=30), "Importing the approved paper history", None),
        ("backdated", TODAY, "Importing", "Leave the backdating reason empty"),
        ("future", TODAY + timedelta(days=1), "Importing", "Leave the backdating reason empty"),
        ("backdated", LONG_AGO, "Importing", "after its predecessor"),
        ("weekly", TODAY + timedelta(days=1), None, "Choose future, same_day or backdated"),
    ],
)
def test_a_successor_starts_when_the_sites_policy_allows(
    qc_actions: ModuleType,
    policy: str | None,
    starts: date,
    reason: str | None,
    refused: str | None,
) -> None:
    schedule = schedule_with(qc_actions, LONG_AGO)
    if refused:
        with pytest.raises(ValueError, match=refused):
            publish(qc_actions, schedule, starts, policy, reason)
        return
    (update,) = publish(qc_actions, schedule, starts, policy, reason)["updates"]["schedule"]
    assert update == {
        "entity": "sched1",
        "values": {"versions": ["v1", "v2"]},
        "require_unchanged": True,
    }


@pytest.mark.parametrize("policy", ["same_day", "backdated"])
def test_no_policy_moves_a_testing_date_a_case_has_pinned(
    qc_actions: ModuleType, policy: str
) -> None:
    """A case tested today, or ten days ago, already pinned the version in force then."""
    for tested in (TODAY, TODAY - timedelta(days=10)):
        schedule = schedule_with(qc_actions, LONG_AGO, tested_through=tested.isoformat())
        reason = "Importing" if tested < TODAY else None
        with pytest.raises(ValueError, match="already pinned the version in force"):
            publish(qc_actions, schedule, tested, policy, reason)
        if policy == "backdated":
            later = tested + timedelta(days=1)
            reason = "Importing" if later < TODAY else None
            assert publish(qc_actions, schedule, later, policy, reason)["updates"]


def test_opening_a_case_records_the_latest_testing_date(qc_actions: ModuleType) -> None:
    lot = record("lot1", 1, of="material1", lot_number="Synthetic lot")
    lot["roles"] = {"material": "of"}
    for before, tested, latest in (
        (None, LONG_AGO + timedelta(days=5), LONG_AGO + timedelta(days=5)),
        (TODAY.isoformat(), LONG_AGO + timedelta(days=5), TODAY),
    ):
        extra = {"tested_through": before} if before else {}
        schedule = schedule_with(qc_actions, LONG_AGO, **extra)
        run = {
            "inputs": {"lot": [lot], "schedule": [schedule]},
            "params": {"testing_on": tested.isoformat()},
            "settings": {"review_flow": "research"},
        }
        (update,) = qc_actions.open_release(run)["updates"]["schedule"]
        assert update["values"] == {"versions": ["v1"], "tested_through": latest.isoformat()}
        assert update["require_unchanged"]


#: USP-NF General Notices 7.20's own examples: the limit, the unrounded result, the
#: result rounded to the limit's places, and whether it conforms. A number is a string
#: here as the analyst reads it; the run takes it as entered.
USP_7_20 = [
    ("98.0", None, 97.96, "98.0", "pass"),
    ("98.0", None, 97.92, "97.9", "oos"),
    ("98.0", None, 97.95, "98.0", "pass"),
    (None, "101.5", 101.55, "101.6", "oos"),
    (None, "101.5", 101.46, "101.5", "pass"),
    (None, "101.5", 101.45, "101.5", "pass"),
    (None, "0.02", 0.025, "0.03", "oos"),
    (None, "0.02", 0.015, "0.02", "pass"),
    (None, "0.02", 0.027, "0.03", "oos"),
    (None, "3", 3.5, "4", "oos"),
    (None, "3", 3.4, "3", "pass"),
    (None, "3", 2.5, "3", "pass"),
]


@pytest.mark.parametrize(("lower", "upper", "measured", "reported", "outcome"), USP_7_20)
def test_a_result_is_rounded_to_its_limits_places_before_its_compared(
    qc_actions: ModuleType,
    lower: str | None,
    upper: str | None,
    measured: float,
    reported: str,
    outcome: str,
) -> None:
    run = inputs()
    (criterion,) = run["inputs"]["case"][0]["included"]["specification"][0]["values"]["criteria"]
    criterion.update(lower=lower, upper=upper)
    run["params"]["value"] = measured
    test = qc_actions.record_test(run)["creates"][0]["values"]
    # The limits are as stated, the value as measured, and the reportable value is compared.
    assert (test["lower"], test["upper"], test["value"]) == (lower, upper, measured)
    assert (test["reported"], test["rounding"], test["outcome"]) == (
        reported,
        "limit_places",
        outcome,
    )


def test_a_criterion_can_compare_a_result_unrounded(qc_actions: ModuleType) -> None:
    run = inputs()
    (criterion,) = run["inputs"]["case"][0]["included"]["specification"][0]["values"]["criteria"]
    criterion.update(lower="98.0", upper=None, rounding="unrounded")
    run["params"]["value"] = 97.96
    test = qc_actions.record_test(run)["creates"][0]["values"]
    assert (test["reported"], test["rounding"], test["outcome"]) == ("97.96", "unrounded", "oos")


@pytest.mark.parametrize(
    ("limits", "why"),
    [
        ({"lower": "98", "upper": "102.0"}, "same decimal place"),
        ({"lower": 98.0}, "as a decimal number"),
        ({"upper": "1e2"}, "as a decimal number"),
        ({"upper": "100.", "lower": None}, "as a decimal number"),
        ({"lower": "98.0", "rounding": "nearest"}, "rounded to its limits' places"),
    ],
)
def test_a_limit_is_a_decimal_stated_to_its_place(
    qc_actions: ModuleType, limits: dict[str, Any], why: str
) -> None:
    row = {"attribute": "Assay", "method": "Synthetic", "unit": "%"} | limits
    with pytest.raises(ValueError, match=why):
        qc_actions.criteria({"values": {"criteria": [row]}})
    # Unrounded, a range may mix places: nothing is rounded to either.
    mixed = {"attribute": "Assay", "method": "M", "unit": "%", "lower": "98", "upper": "102.0"}
    assert qc_actions.criteria({"values": {"criteria": [mixed | {"rounding": "unrounded"}]}})


def test_a_case_pins_its_material_and_grade_and_needs_its_name(qc_actions: ModuleType) -> None:
    run = opening(qc_actions)
    schedule = run["inputs"]["schedule"][0]
    schedule["values"]["grade"] = "USP"
    (case,) = qc_actions.open_release(run)["creates"]
    assert (case["values"]["material"], case["values"]["grade"]) == ("material1", "USP")
    # A registered material is pinned as its record in the project, under its entry.
    entry = schedule["included"]["material"][0]
    schedule["included"]["material"] = [entry | {"id": "record1", "entry_id": "material1"}]
    assert qc_actions.open_release(run)["creates"][0]["values"]["material"] == "material1"
    schedule["included"]["material"] = [entry | {"id": "record1", "entry_id": "material2"}]
    with pytest.raises(ValueError, match="material is missing"):
        qc_actions.open_release(run)
    schedule["included"]["material"] = [entry]
    schedule["included"]["material"][0]["values"]["name"] = " "
    with pytest.raises(ValueError, match="Name MATERIAL1 on its record first"):
        qc_actions.open_release(run)
    schedule["included"]["material"] = []
    with pytest.raises(ValueError, match="material is missing"):
        qc_actions.open_release(run)


def test_a_certificate_names_the_material_as_it_is_when_prepared(qc_actions: ModuleType) -> None:
    run = inputs()
    case = run["inputs"]["case"][0]
    case["values"] |= {"review_flow": "research", "grade": "Reagent grade"}
    run["params"]["value"] = 100
    output = qc_actions.record_test(run)
    case["values"].update(output["updates"]["case"][0]["values"])
    test = record("test1", 1, **output["creates"][0]["values"])
    prepare = {"inputs": {"case": [case], "tests": [test]}}
    (certificate,) = qc_actions.prepare_certificate(prepare)["creates"]
    assert certificate["values"]["coa"]["material"] == {
        "name": "Synthetic assay standard",
        "hrid": "MATERIAL1",
        "grade": "Reagent grade",
    }
    material = case["included"]["material"][0]
    case["included"]["material"] = [material | {"id": "record1", "entry_id": "material1"}]
    (certificate,) = qc_actions.prepare_certificate(prepare)["creates"]
    assert certificate["values"]["coa"]["material"]["name"] == "Synthetic assay standard"
    case["included"]["material"] = []
    with pytest.raises(ValueError, match="material is missing"):
        qc_actions.prepare_certificate(prepare)
    case["included"]["material"] = [material]
    case["included"]["material"][0]["values"]["name"] = None
    with pytest.raises(ValueError, match="names the material"):
        qc_actions.prepare_certificate(prepare)

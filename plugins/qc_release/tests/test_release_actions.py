"""Exercise repeat authorization and certificate completeness with synthetic observations."""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
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
                "lower": 90,
                "upper": 110,
            },
        ],
    )
    lot = record("lot1", 8, lot_number="Corrected after testing", retest_on="2023-04-01")
    case = record(
        "case1",
        1,
        lot=lot["id"],
        lot_revision=7,
        lot_number="Synthetic lot 1",
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
    case["included"] = {"specification": [specification], "lot": [lot]}
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
    prepare = {
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
    assert coa["results"] == [
        {
            "attribute": "Assay",
            "method": "Synthetic example",
            "unit": "AU",
            "lower": 90,
            "upper": 110,
            "value": 90,
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
    tampered["values"]["coa"]["results"][0]["value"] = 101
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

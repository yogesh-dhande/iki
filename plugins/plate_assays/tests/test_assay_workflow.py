"""A synthetic reader matrix flows through commit to the fitted result without re-entry."""

import importlib
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

PLUGIN = Path(__file__).resolve().parents[1]


def uid(number):
    return str(UUID(int=number))


@pytest.fixture
def actions(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    return importlib.import_module("assays")


def plate_run():
    rows = [
        {
            "well": f"A{i + 1}",
            "sample": uid(5),
            "replicate": 1,
            "concentration": dose,
            "concentration_unit": "uM",
        }
        for i, dose in enumerate([0.25, 0.5, 1, 2, 4, 8, 16, 32])
    ]
    rows.extend(
        [
            {"well": "A9", "control": "low", "replicate": 1},
            {"well": "A10", "control": "high", "replicate": 1},
        ]
    )
    return {
        "inputs": {
            "plate": [
                {
                    "id": uid(1),
                    "revision": 3,
                    "values": {
                        "size": "plate_96",
                        "assignments": rows,
                        "reader_file": uid(2),
                        "name": "Synthetic",
                        "procedure": "SOP-A",
                        "procedure_revision": "2",
                    },
                }
            ]
        },
        "files": {uid(2): {"path": str(PLUGIN / "tests/data/mars-96-series.csv")}},
    }


def committed_read(actions):
    run = plate_run()
    preview = actions.reader_output(run, commit=False)["updates"]["plate"][0]
    assert preview["require_unchanged"] and not preview["values"]["preview"]["issues"]
    run["inputs"]["plate"][0]["values"].update(preview["values"])
    result = actions.reader_output(run, commit=True)
    assert result["updates"]["plate"] == [
        {"entity": uid(1), "require_unchanged": True, "values": {"committed": True}}
    ]
    *created, read = result["creates"]
    readings = [
        {"id": uid(100 + i), "hrid": f"WME-{i:06d}", "revision": 1, "values": row["values"]}
        for i, row in enumerate(created, 1)
    ]
    assert [row["key"] for row in created] == [
        ref["$create"] for ref in read["values"]["measurements"]
    ]
    read["values"]["measurements"] = [m["id"] for m in readings]
    return {
        "id": uid(3),
        "revision": 1,
        "values": read["values"],
        "included": {"measurements": readings},
    }


@pytest.mark.parametrize(
    ("flow", "request_id", "kind"),
    [("research", None, "ResearchAssayResult"), ("regulated_qc", uid(7), "RequestedQCAssayResult")],
)
def test_fit_from_committed_measurements(actions, flow, request_id, kind):
    read = committed_read(actions)
    read["values"]["request"] = request_id
    read["included"]["request"] = (
        [{"id": request_id, "covered_materials": [uid(6)]}] if request_id else []
    )
    run = {
        "run": {"requested_by": uid(8)},
        "inputs": {"read": [read], "sample": [{"id": uid(6), "entry_id": uid(5)}]},
        "settings": {"review_flow": flow},
        "params": {"exclusions": "A2: bubble"},
    }
    (fit,) = actions.fit_output(run)["creates"]
    assert fit["type"] == f"iki/plate_assays.{kind}"
    assert fit["derived_from"] == [uid(3), uid(6)]
    evidence = fit["values"]
    assert (evidence["procedure"], evidence["procedure_revision"]) == ("SOP-A", "2")
    assert evidence["fit_status"] == "fitted"
    assert evidence["ci_low"] < evidence["endpoint_value"] < evidence["ci_high"]
    assert evidence["review_flow"] == flow
    assert evidence["normalization"] == "none"
    assert evidence["point_count"] == 7 and len(evidence["points"]) == 8
    (excluded,) = [point for point in evidence["points"] if point["excluded_by"]]
    assert excluded == {
        "reading": uid(102),
        "reading_hrid": "WME-000002",
        "revision": 1,
        "well": "A2",
        "replicate": 1,
        "dose": 0.5,
        "response": 0.866429,
        "excluded_by": uid(8),
        "exclusion_reason": "bubble",
    }
    assert evidence["measurements"] == [point["reading"] for point in evidence["points"]]
    with pytest.raises(ValueError, match="outside this sample"):
        actions.fit_output({**run, "params": {"exclusions": "A9: a control"}})
    with pytest.raises(ValueError, match="authenticated"):
        actions.fit_output({**run, "run": {}})


def test_commit_requires_the_clean_preview_of_the_current_input(actions):
    run = plate_run()
    prior = actions.reader_output(run, commit=False)["updates"]["plate"][0]["values"]
    plate = run["inputs"]["plate"][0]["values"]
    plate.update(deepcopy(prior))
    plate["procedure_revision"] = "3"
    with pytest.raises(ValueError, match="current layout"):
        actions.reader_output(run, commit=True)
    plate["procedure_revision"] = "2"
    plate["assignments"][0]["concentration"] = 70
    with pytest.raises(ValueError, match="current layout"):
        actions.reader_output(run, commit=True)
    plate["assignments"].pop()
    with pytest.raises(ValueError, match="without assignments"):
        actions.reader_output(run, commit=True)
    plate["committed"] = True
    with pytest.raises(ValueError, match="already been committed"):
        actions.reader_output(run, commit=True)

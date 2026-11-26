"""A synthetic reader matrix flows through commit to the fitted result without re-entry."""

import hashlib
import importlib
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid5

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
                        "reader_format": "bmg_mars_absorbance",
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
    digest = hashlib.sha256((PLUGIN / "tests/data/mars-96-series.csv").read_bytes()).hexdigest()
    plate = run["inputs"]["plate"][0]["values"]
    assert result["updates"]["plate"] == [
        {
            "entity": uid(1),
            "require_unchanged": True,
            "values": {
                "committed": True,
                "committed_layout": {
                    "size": "plate_96",
                    "assignments": plate["assignments"],
                    "procedure": "SOP-A",
                    "procedure_revision": "2",
                    "request": None,
                },
                "committed_reads": [{"reader_file": uid(2), "sha256": digest}],
            },
        }
    ]
    *created, read = result["creates"]
    assert read["values"]["reader_format"] == "bmg_mars_absorbance/1"
    assert read["values"]["import_key"] == str(uuid5(UUID(uid(1)), digest))
    # The synthetic export's run header, as MARS wrote it.
    assert read["values"]["acquisition"] == {
        "user": "synthetic",
        "test_run": "1",
        "test_name": "Synthetic decreasing dose response",
        "date": "01/01/2025",
        "time": "00:00:00",
        "id1": "synthetic-96",
    }
    assert {row["values"]["readout"] for row in created} == {"absorbance"}
    assert all(row["values"]["signal"] is not None for row in created)
    assert not any("activity_percent" in row["values"] for row in created)
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
    with pytest.raises(ValueError, match="keeps its layout"):
        actions.reader_output(run, commit=True)


def test_the_same_export_is_one_read(actions):
    """The same export can't be committed to a plate twice."""
    run = plate_run()
    plate = run["inputs"]["plate"][0]
    plate["values"].update(
        actions.reader_output(run, commit=False)["updates"]["plate"][0]["values"]
    )
    first = actions.reader_output(run, commit=True)
    plate["values"].update(first["updates"]["plate"][0]["values"])
    plate["revision"] += 1
    # Previewing the same export again says so, and commit refuses it.
    again = actions.reader_output(run, commit=False)["updates"]["plate"][0]["values"]["preview"]
    assert again["issues"] == [
        "This reader file is already committed to this plate. Attach the next read's file."
    ]
    plate["values"]["preview"] = again
    with pytest.raises(ValueError, match="already committed"):
        actions.reader_output(run, commit=True)


def test_a_later_read_commits_on_the_same_layout(actions, tmp_path):
    """Here the same synthetic values, re-exported with another run number."""
    run = plate_run()
    plate = run["inputs"]["plate"][0]
    plate["values"].update(
        actions.reader_output(run, commit=False)["updates"]["plate"][0]["values"]
    )
    first = actions.reader_output(run, commit=True)
    plate["values"].update(first["updates"]["plate"][0]["values"])
    later = tmp_path / "second-read.csv"
    later.write_bytes(
        (PLUGIN / "tests/data/mars-96-series.csv")
        .read_bytes()
        .replace(b"Test run no.: 1", b"Test run no.: 2")
    )
    plate["values"]["reader_file"] = uid(9)
    run["files"][uid(9)] = {"path": str(later)}
    plate["values"].update(
        actions.reader_output(run, commit=False)["updates"]["plate"][0]["values"]
    )
    second = actions.reader_output(run, commit=True)
    *_, read = second["creates"]
    assert read["values"]["acquisition"]["test_run"] == "2"
    assert read["values"]["reader_file"] == uid(9)
    assert read["values"]["import_key"] != first["creates"][-1]["values"]["import_key"]
    reads = second["updates"]["plate"][0]["values"]["committed_reads"]
    assert [r["reader_file"] for r in reads] == [uid(2), uid(9)]

"""The adapter's two attempts, with the provider's responses supplied by the test."""

import json
import runpy
import urllib.request
from io import BytesIO
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "predict.py"
HOST = "https://models.example"
CALLBACK = "https://iki.example/api/v1/outside-apis/callbacks/test"
PDB = "ATOM      1  N   MET A   1      11.104  13.207   2.100  1.00  0.00           N\n"


def test_provider_job_survives_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as the worker does. The provider accepts a job, then serves its result."""
    run_input = tmp_path / "input.json"
    output = tmp_path / "output.json"
    monkeypatch.setenv("IKI_INPUT", str(run_input))
    monkeypatch.setenv("IKI_OUTPUT", str(output))
    monkeypatch.setenv("IKI_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("IKI_API_FOLDCLOUD_URL", HOST)
    monkeypatch.setenv("IKI_API_FOLDCLOUD_CALLBACK_URL", CALLBACK)

    calls = []

    def respond(request: urllib.request.Request, *, timeout: int) -> BytesIO:
        calls.append((request.get_method(), request.full_url))
        if request.data is not None:
            assert isinstance(request.data, bytes)
            assert json.loads(request.data) == {
                "sequence": "MKVLAAGIVG",
                "model": "fold-2",
                "num_recycles": 3,
                "webhook": CALLBACK,
            }
            assert request.get_header("Authorization") is None
            return BytesIO(b'{"id":"job-87"}')
        return BytesIO(json.dumps({"model": "fold-2", "output": {"pdb": PDB}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    protein = {"id": "p1", "hrid": "PRT-000001", "values": {"sequence": "MKVLAAGIVG"}}
    run = {
        "inputs": {"protein": [protein]},
        "params": {"model": "fold-2", "num_recycles": 3},
    }
    run_input.write_text(json.dumps(run))
    runpy.run_path(str(SCRIPT), run_name="__main__")
    parked = json.loads(output.read_text())["park"]
    assert parked == {
        "api": "foldcloud",
        "job": "job-87",
        "state": {"protein": "p1", "hrid": "PRT-000001"},
    }

    # Some providers return only a completion status and serve the file on a final GET.
    run_input.write_text(json.dumps({**run, "resume": {**parked, "answer": None}}))
    runpy.run_path(str(SCRIPT), run_name="__main__")
    assert calls == [
        ("POST", f"{HOST}/v1/predictions"),
        ("GET", f"{HOST}/v1/predictions/job-87"),
    ]
    (created,) = json.loads(output.read_text())["creates"]
    assert created["type"] == "iki/protein.ProteinStructure"
    assert created["values"]["chains"] == ["p1"]
    assert created["values"]["chain_ids"] == ["A"]
    artifact = created["values"]["structure"]["$file"]
    assert (tmp_path / artifact).read_text() == PDB
    assert created["origin"]["id"] == "job-87"
    assert created["origin"]["made"]["software"] == [{"name": "FoldCloud fold-2"}]

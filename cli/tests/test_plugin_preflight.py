"""A deployment's admin checks the built manifest with their normal API sign-in."""

import json
from pathlib import Path
from typing import Any

import pytest

from iki import Client, Forbidden
from iki_cli.main import main
from iki_spec import API_VERSION
from iki_spec.model import plugin_to_dict
from iki_spec.validate import load_plugin

PLUGIN = Path(__file__).parents[2] / "spec/tests/plugins/dna"
REPORT = {
    "plugin": "acme/dna",
    "version": "1.2.0",
    "configuration_revision": "a" * 64,
    "candidate_revision": "b" * 64,
    "blocking": True,
    "types": [
        {
            "type": "acme/dna.DNASequence",
            "checked": 100,
            "sampled": True,
            "findings": [
                {
                    "kind": "enum",
                    "subject": "topology",
                    "message": "Stored topology is absent from the proposed enum.",
                    "blocking": True,
                    "count": 3,
                    "examples": ["DNA-000002"],
                }
            ],
        },
    ],
}


@pytest.fixture
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("IKI_TOKEN", "fixture-token")
    plugin = load_plugin(PLUGIN).plugin
    assert plugin
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"apiVersion": API_VERSION, "plugin": plugin_to_dict(plugin)}))
    return path


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_plugin_preflight(
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
    blocked: bool,
) -> None:
    report = {**REPORT, "blocking": blocked}
    if not blocked:
        report["types"] = []

    def send(client: Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        assert client.url == "https://lab.example"
        assert (method, path) == ("POST", "/plugins/acme/dna/preflight")
        assert kwargs["json"] == json.loads(manifest.read_text())
        return report

    monkeypatch.setattr(Client, "request", send)
    args = ["plugin", "preflight", str(manifest), "--url", "https://lab.example"]
    assert main([*args, "--json"] if as_json else args) == int(blocked)
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == report
    else:
        assert str(report["configuration_revision"]) in output
        assert str(report["candidate_revision"]) in output
        if blocked:
            for word in ("3", "sampled", "topology", "DNA-000002"):
                assert word in output


def test_a_clean_sample_still_requires_review(
    manifest: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IKI_URL", "https://lab.example")
    report = {
        **REPORT,
        "types": [
            {"type": "acme/dna.DNASequence", "checked": 500, "sampled": True, "findings": []}
        ],
    }
    monkeypatch.setattr(Client, "request", lambda *a, **k: report)
    assert main(["plugin", "preflight", str(manifest)]) == 1
    assert "Sample" in capsys.readouterr().out


def test_bad_preflight_inputs_and_refusals(
    manifest: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied(*args: Any, **kwargs: Any) -> None:
        raise Forbidden(403, "An Org Admin must run this check.")

    monkeypatch.setenv("IKI_URL", "https://lab.example")
    monkeypatch.setattr(Client, "request", denied)
    assert main(["plugin", "preflight", str(manifest)]) == 2
    assert "Org Admin" in capsys.readouterr().err
    manifest.write_text("missing manifest")
    assert main(["plugin", "preflight", str(manifest)]) == 2
    assert "manifest" in capsys.readouterr().err.lower()

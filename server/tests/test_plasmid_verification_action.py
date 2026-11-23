"""The archival worker contract for prep evidence, including distinct entry and record IDs."""

import json
import runpy
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/lots/scripts/verification.py"


def uid(n: int) -> str:
    return f"00000000-0000-7000-8003-{n:012d}"


def verification_input() -> dict[str, Any]:
    # Synthetic input snapshots. The plasmid and DNA registry entries differ from
    # their project records. The worker receives the archival launch's pinned revisions.
    return {
        "inputs": {
            "prep": [
                {
                    "id": uid(1),
                    "revision": 4,
                    "values": {"plasmid": uid(2)},
                    "included": {
                        "plasmid": [{"id": uid(3), "revision": 7, "values": {"sequence": uid(4)}}]
                    },
                }
            ],
            "alignment": [
                {"id": uid(5), "revision": 3, "values": {"reference": uid(4), "aligned": uid(6)}}
            ],
        },
        "files": {uid(6): {"size": 30}, uid(7): {"size": 80}},
        "settings": {"verification_flow": "regulated_qc"},
        "params": {"outcome": "discrepancy", "report": uid(7), "notes": "Synthetic fixture."},
    }


def test_archive_both_evidence_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = verification_input()
    # A client parameter cannot downgrade the installation's review procedure.
    run["params"]["verification_flow"] = "research"
    source, target = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps(run), encoding="utf-8")
    monkeypatch.setenv("IKI_INPUT", str(source))
    monkeypatch.setenv("IKI_OUTPUT", str(target))
    runpy.run_path(str(SCRIPT), run_name="__main__")
    output = json.loads(target.read_text(encoding="utf-8"))
    assert output["creates"] == [
        {
            "type": "iki/lots.PlasmidVerification",
            "derived_from": [uid(1), uid(5)],
            "values": {
                "prep": uid(1),
                "plasmid": uid(2),
                "plasmid_record_id": uid(3),
                "plasmid_revision": 7,
                "alignment": uid(5),
                "alignment_revision": 3,
                "report": uid(7),
                "outcome": "discrepancy",
                "notes": "Synthetic fixture.",
                "review_flow": "regulated_qc",
            },
        }
    ]
    assert set(tmp_path.iterdir()) == {source, target}


@pytest.mark.parametrize("reference", [None, uid(2), uid(3)])
def test_alignment_requires_same_registered_dna(reference: str | None) -> None:
    run = verification_input()
    run["inputs"]["alignment"][0]["values"]["reference"] = reference
    with pytest.raises(ValueError, match="prep plasmid's registered DNA"):
        runpy.run_path(str(SCRIPT))["archive"](run)


@pytest.mark.parametrize("mode", [None, "", "unknown"])
def test_review_flow_comes_from_deployment(mode: str | None) -> None:
    run = verification_input()
    run["settings"] = {"verification_flow": mode}
    with pytest.raises(ValueError, match="deployment must select"):
        runpy.run_path(str(SCRIPT))["archive"](run)


@pytest.mark.parametrize(
    ("removed", "message"), [(uid(6), "pinned aligned FASTA"), (uid(7), "file supplied")]
)
def test_files_belong_to_this_run(removed: str, message: str) -> None:
    run = verification_input()
    del run["files"][removed]
    with pytest.raises(ValueError, match=message):
        runpy.run_path(str(SCRIPT))["archive"](run)


@pytest.mark.parametrize(
    ("empty", "message"), [(uid(6), "aligned FASTA file is empty"), (uid(7), "report is empty")]
)
def test_empty_files_cannot_be_archived_as_evidence(empty: str, message: str) -> None:
    run = verification_input()
    run["files"][empty]["size"] = 0
    with pytest.raises(ValueError, match=message):
        runpy.run_path(str(SCRIPT))["archive"](run)

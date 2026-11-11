"""The DNA plugin's actions: what its sequence calculations give, run as a worker runs
them (an input.json in, an output.json out)."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "dna" / "scripts" / "sequences.py"


def run(tmp_path: Path, kind: str, name: str, bases: list[str]) -> dict[str, Any]:
    entities = [
        {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"X-{i}", "values": {"bases": b}}
        for i, b in enumerate(bases)
    ]
    (tmp_path / "input.json").write_text(json.dumps({"inputs": {name: entities}}))
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(SCRIPT), kind],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1].startswith("::progress 1.00 ")
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return {u["entity"][-1]: u["values"] for u in output["updates"][name]}


def test_composition_counts_gc_among_bases_whose_pairing_is_known(tmp_path: Path) -> None:
    found = run(tmp_path, "composition", "sequence", ["ATGCGC", "atgcNNNNS", "NNNN"])
    assert found["0"] == {"length": 6, "gc_content": 66.67}
    assert found["1"] == {"length": 9, "gc_content": 60.0}
    assert found["2"] == {"length": 4}  # nothing to count


@pytest.mark.parametrize(
    ("bases", "tm"),
    [
        # As Biopython 1.85 gives them: Tm_NN with DNA_NN3, Na=50 mM, 50 nM primer, saltcorr=5.
        ("AGCGGATAACAATTTCACACAGGA", 54.8),  # M13 reverse
        ("GTAAAACGACGGCCAGT", 49.2),  # M13 forward (-20)
        ("TAATACGACTCACTATAGGG", 44.6),  # T7 promoter
        ("GCGCGC", 17.3),  # pairs with itself
    ],
)
def test_primer_melting_temperatures_match_santalucia(
    tmp_path: Path, bases: str, tm: float
) -> None:
    (found,) = run(tmp_path, "primers", "primer", [bases]).values()
    assert found["melting_temperature"] == tm
    assert found["length"] == len(bases)


def test_primers_with_ambiguous_bases_get_no_melting_temperature(tmp_path: Path) -> None:
    (found,) = run(tmp_path, "primers", "primer", ["ACGTNACGTACGTACGT"]).values()
    assert "melting_temperature" not in found
    assert found["gc_content"] == 50.0

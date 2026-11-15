"""The protein plugin's multiple alignments: proteins through MAFFT, into a core.Alignment
of them, rows in the order given, the reference first.

MAFFT is in the plugin's image, not here: these tests put a stand-in on the PATH that
aligns as a test asks, or changes residues, or fails, and records how it was called. The
image's own check and the dev cluster's end-to-end run the real one."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "protein" / "scripts" / "multiple.py"

FAKE = """\
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("v7.505 (2022/Apr/10)", file=sys.stderr)
    sys.exit(0)
Path(os.environ["CALLED"]).write_text(json.dumps(sys.argv[1:]))
mode = os.environ["MODE"]
if mode == "broken":
    print("nthread = 2\\nERROR: Too few sequences", file=sys.stderr)
    sys.exit(1)
names, rows = [], []
for line in Path(sys.argv[-1]).read_text().splitlines():
    if line.startswith(">"):
        names.append(line[1:])
        rows.append("")
    else:
        rows[-1] += line
aligned = json.loads(os.environ["ALIGNED"]) if os.environ.get("ALIGNED") else None
if aligned is None:
    width = max(len(r) for r in rows)
    aligned = [r.lower().ljust(width, "-") for r in rows]
if mode == "changed":
    aligned[1] = "W" + aligned[1][1:]
if mode == "reordered":
    names.reverse()
    aligned.reverse()
print("Strategy:", file=sys.stderr)
print(" L-INS-i (Probably most accurate, very slow)", file=sys.stderr)
print(" Iterative refinement method", file=sys.stderr)
for name, row in zip(names, aligned):
    print(f">{name}")
    for i in range(0, len(row), 60):
        print(row[i : i + 60])
"""


def protein(i: int, sequence: str, name: str | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {"sequence": sequence}
    if name:
        values["name"] = name
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"PRT-{i:06d}", "values": values}


def run(
    tmp_path: Path,
    proteins: list[dict[str, Any]],
    reference: list[dict[str, Any]] | None = None,
    mode: str = "ok",
    aligned: list[str] | None = None,
    **params: Any,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "mafft"
    fake.write_text(f"#!{sys.executable}\n{FAKE}")
    fake.chmod(0o755)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    output_path = tmp_path / "output.json"
    output_path.unlink(missing_ok=True)
    (tmp_path / "input.json").write_text(
        json.dumps(
            {
                "run": {"hrid": "RUN-000009"},
                "params": {"strategy": "auto", **params},
                "inputs": {"proteins": proteins, "reference": reference or []},
            }
        )
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(SCRIPT)],
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(output_path),
            "IKI_OUT_DIR": str(out_dir),
            "CALLED": str(tmp_path / "called"),
            "MODE": mode,
            "ALIGNED": json.dumps(aligned) if aligned else "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if not output_path.exists():
        return result, None
    output: dict[str, Any] = json.loads(output_path.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


def called(tmp_path: Path) -> list[str]:
    found: list[str] = json.loads((tmp_path / "called").read_text())
    return found


def test_aligns_proteins_into_an_alignment_of_them_in_the_order_given(tmp_path: Path) -> None:
    given = [
        protein(1, "MKTAYIAKQRQISFVKSHFSRQ", "A"),
        protein(2, "MKTAYIAKQRFVKSHFSRQ*", "B"),
        protein(3, "MKTAYLAKQRQISFVKSHFSRQ", "C"),
    ]
    rows = ["MKTAYIAKQRQISFVKSHFSRQ", "MKTAYIAKQR---FVKSHFSRQ", "MKTAYLAKQRQISFVKSHFSRQ"]

    result, output = run(tmp_path, given, aligned=rows)

    assert output is not None, result.stdout
    assert (tmp_path / "out" / "alignment.fasta").read_text() == "".join(
        f">PRT-00000{i}\n{row}\n" for i, row in enumerate(rows, start=1)
    )
    (created,) = output["creates"]
    assert created == {
        "type": "core.Alignment",
        "values": {
            "name": "A, B and C",
            "description": (
                "3 proteins over 22 columns: 81.8% of columns identical, 13.6% with gaps."
            ),
            "members": [p["id"] for p in given],
            "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-aa-fasta"},
            "method": "MAFFT v7.505 (2022/Apr/10), L-INS-i (chosen by --auto)",
        },
        "derived_from": [p["id"] for p in given],
    }
    # 18 of 22 columns agree: three are gapped, and I/L at column 6 differs.
    assert output["metrics"] == {
        "rows": 3,
        "length": 22,
        "identity_percent": 81.8,
        "gaps_percent": 13.6,
    }
    assert called(tmp_path)[:2] == ["--auto", "--amino"]
    assert "--anysymbol" not in called(tmp_path)
    assert "Aligned 3 proteins over 22 columns with MAFFT's L-INS-i." in result.stdout


def test_the_reference_is_the_first_row_once_and_names_the_alignment(tmp_path: Path) -> None:
    wild = protein(1, "MKTAYIAKQR", "ABL1")
    given = [protein(2, "MKTAYLAKQR"), wild, protein(3, "MKTAYIAKQK")]

    _, output = run(tmp_path, given, reference=[wild])

    assert output is not None
    values = output["creates"][0]["values"]
    assert values["reference"] == wild["id"]
    assert values["members"] == [wild["id"], given[0]["id"], given[2]["id"]]
    assert values["name"] == "3 proteins aligned to ABL1"
    headers = [
        line
        for line in (tmp_path / "out" / "alignment.fasta").read_text().splitlines()
        if line.startswith(">")
    ]
    assert headers == [">PRT-000001", ">PRT-000002", ">PRT-000003"]


def test_names_strategies_and_unusual_residues(tmp_path: Path) -> None:
    given = [protein(i, "MKUAYIAKQR" if i == 1 else "MKTAYIAKQR") for i in range(1, 6)]

    _, output = run(tmp_path, given, strategy="E-INS-i", name="  Selenoproteins  ")

    assert output is not None
    values = output["creates"][0]["values"]
    assert values["name"] == "Selenoproteins"
    assert values["method"] == (
        "MAFFT v7.505 (2022/Apr/10), L-INS-i, with --anysymbol for unusual residues"
    )
    assert called(tmp_path)[:3] == ["--genafpair", "--maxiterate", "1000"]
    assert "--anysymbol" in called(tmp_path)

    _, output = run(tmp_path, given)
    assert output is not None
    assert output["creates"][0]["values"]["name"] == "PRT-000001, PRT-000002 and 3 more"


@pytest.mark.parametrize(
    ("proteins", "mode", "params", "said"),
    [
        ([protein(1, "MKT")], "ok", {}, "Choose at least two proteins to align."),
        ([protein(1, "MKT"), protein(2, "*")], "ok", {}, "PRT-000002 has no residues to align."),
        (
            [protein(1, "MKT"), protein(2, "MK*T")],
            "ok",
            {},
            "PRT-000002 has a stop inside it, at residue 3.",
        ),
        (
            [protein(1, "MKT"), protein(2, "MK-T")],
            "ok",
            {},
            "PRT-000002 has '-' at residue 3, not a residue.",
        ),
        (
            [protein(1, "MKT"), protein(2, "M" * 40_001)],
            "ok",
            {},
            "PRT-000002 is too long to align here: 40,001 residues is more than 40,000.",
        ),
        (
            [protein(i, "MKT") for i in range(1, 202)],
            "ok",
            {"strategy": "L-INS-i"},
            "201 proteins are too many for this strategy, which MAFFT means for up to 200:"
            " choose Auto or FFT-NS-2.",
        ),
        (
            [protein(1, "MKT"), protein(2, "MKV")],
            "broken",
            {},
            "MAFFT failed (1): ERROR: Too few sequences",
        ),
        (
            [protein(1, "MKT"), protein(2, "MKV")],
            "changed",
            {},
            "MAFFT changed PRT-000002's residues, not only its gaps.",
        ),
        (
            [protein(1, "MKT"), protein(2, "MKV")],
            "reordered",
            {},
            "MAFFT's alignment doesn't have the proteins given, in order.",
        ),
    ],
)
def test_what_cant_be_aligned_fails_its_run_saying_why(
    tmp_path: Path, proteins: list[dict[str, Any]], mode: str, params: dict[str, Any], said: str
) -> None:
    result, output = run(tmp_path, proteins, mode=mode, **params)

    assert output is None
    assert result.returncode == 1
    assert result.stdout.strip() == f"::error {said}"

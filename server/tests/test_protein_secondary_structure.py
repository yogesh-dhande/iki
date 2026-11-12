"""The protein plugin's secondary structure: each structure's file through DSSP, into a
letter per residue of each chain, the shares in helices and strands, the accessible
surface, and the annotated file.

mkdssp is in the plugin's image, not here: these tests put a stand-in on the PATH that
writes what mkdssp writes for a made-up structure, or fails as it does. The image's own
check and the dev cluster's end-to-end run the real one."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "protein" / "scripts" / "secondary.py"

#: A made-up structure: chains A and B of one protein, two models (as NMR gives).
STRUCTURE = """\
data_1XYZ
_entry.id 1XYZ
loop_
_entity_poly.entity_id
_entity_poly.type
_entity_poly.pdbx_strand_id
_entity_poly.pdbx_seq_one_letter_code_can
1 'polypeptide(L)' A,B MKVW
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.auth_asym_id
_atom_site.pdbx_PDB_model_num
ATOM 1 C CA MET A 1 1 0.0 0.0 0.0 A 1
ATOM 2 C CA MET A 1 1 0.0 0.0 0.0 A 2
"""

#: What mkdssp writes for it: the sequence scheme (B's residue 4 has no coordinates, and
#: residue 2 of A has two conformers) and its summary, which leaves out residues it
#: couldn't place. Coil is `.`.
ANNOTATED = """\
data_1XYZ
loop_
_pdbx_poly_seq_scheme.asym_id
_pdbx_poly_seq_scheme.entity_id
_pdbx_poly_seq_scheme.seq_id
_pdbx_poly_seq_scheme.mon_id
_pdbx_poly_seq_scheme.pdb_strand_id
A 1 1 MET A
A 1 2 LYS A
A 1 2 ARG A
A 1 3 VAL A
A 1 4 TRP A
C 1 1 MET B
C 1 2 LYS B
C 1 3 VAL B
C 1 4 TRP B
loop_
_dssp_struct_summary.entry_id
_dssp_struct_summary.label_asym_id
_dssp_struct_summary.label_seq_id
_dssp_struct_summary.label_comp_id
_dssp_struct_summary.secondary_structure
_dssp_struct_summary.accessibility
1XYZ A 1 MET . 120.5
1XYZ A 2 LYS H 30.0
1XYZ A 3 VAL H 10.25
1XYZ A 4 TRP G 5.0
1XYZ C 1 MET E 50.0
1XYZ C 2 LYS B 20.0
1XYZ C 3 VAL T ?
"""

FAKE = """\
import os, sys
from pathlib import Path
Path(os.environ["CALLED"]).write_text(" ".join(sys.argv[1:]))
given = Path(sys.argv[-2]).read_text()
mode = os.environ["MODE"]
if mode == "unknown":
    print("Unknown compound: XYZ\\nMissing compound information for XYZ", file=sys.stderr)
    sys.exit(134)
if mode == "broken":
    print("Some warning\\nCannot reconstruct PDBx file, atom data missing\\n", file=sys.stderr)
    sys.exit(1)
assert given.startswith("data_"), given[:40]
Path(sys.argv[-1]).write_text(os.environ["ANNOTATED"])
"""


def run(
    tmp_path: Path, files: dict[str, str], chains: dict[str, list[str]], mode: str = "ok"
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "mkdssp"
    fake.write_text(f"#!{sys.executable}\n{FAKE}")
    fake.chmod(0o755)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    structures = []
    described = {}
    for n, (name, text) in enumerate(files.items(), start=1):
        path = tmp_path / name
        path.write_text(text)
        described[f"f{n}"] = {"name": name, "path": str(path)}
        values = {"structure": f"f{n}", "chain_ids": chains[name]}
        structures.append({"id": f"e{n}", "hrid": f"PST-00000{n}", "values": values})
    (tmp_path / "input.json").write_text(
        json.dumps({"inputs": {"structure": structures}, "files": described})
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT)],
        cwd=SCRIPT.parent.parent,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(tmp_path / "output.json"),
            "IKI_OUT_DIR": str(out_dir),
            "CALLED": str(tmp_path / "called"),
            "MODE": mode,
            "ANNOTATED": ANNOTATED,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    output: dict[str, Any] = json.loads((tmp_path / "output.json").read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


def test_each_chain_gets_a_letter_per_residue_and_the_structure_its_shares(
    tmp_path: Path,
) -> None:
    result, output = run(tmp_path, {"1xyz.cif": STRUCTURE}, {"1xyz.cif": ["A", "B"]})

    (update,) = output["updates"]["structure"]
    assert update["entity"] == "e1"
    values = update["values"]
    # A's second residue once, though it has two conformers; B's last has no coordinates.
    assert values["secondary_structure"] == ["-HHG", "EBT?"]
    # 3 of 7 placed residues in helices (H, G), 2 in strands (E, B).
    assert values["helix_fraction"] == 42.9
    assert values["strand_fraction"] == 28.6
    assert values["accessible_surface_area"] == 235.8
    assert values["dssp"] == {"$file": "PST-000001-dssp.cif", "media_type": "chemical/x-mmcif"}
    assert (tmp_path / "out" / "PST-000001-dssp.cif").read_text() == ANNOTATED
    called = (tmp_path / "called").read_text().split()
    assert called[:3] == ["--output-format", "mmcif", "--calculate-accessibility"]
    note = "It has several models: DSSP annotated the first."
    assert output["messages"] == [{"level": "info", "text": note, "entity": "e1"}]
    assert "PST-000001: 42.9% helix, 28.6% strand" in result.stdout


def test_chains_come_in_the_order_of_chain_ids_and_a_missing_one_is_said(
    tmp_path: Path,
) -> None:
    _, output = run(tmp_path, {"1xyz.cif": STRUCTURE}, {"1xyz.cif": ["B", "Z", "A"]})

    (update,) = output["updates"]["structure"]
    assert update["values"]["secondary_structure"] == ["EBT?", "", "-HHG"]
    texts = [m["text"] for m in output["messages"]]
    assert "Chain Z isn't in DSSP's output: it has no secondary structure." in texts


def test_a_pdb_file_reaches_dssp_as_the_mmcif_the_import_read(tmp_path: Path) -> None:
    pdb = "ATOM      1  CA  MET A   1       0.000   0.000   0.000  1.00 90.00           C\nEND\n"

    _, output = run(tmp_path, {"model.pdb": pdb}, {"model.pdb": ["A"]})

    (update,) = output["updates"]["structure"]
    assert update["values"]["secondary_structure"] == ["-HHG"]
    given = (tmp_path / "called").read_text().split()[-2]
    assert given.endswith(".cif")


@pytest.mark.parametrize(
    ("mode", "said"),
    [
        (
            "unknown",
            "DSSP doesn't know residue XYZ: it isn't in the Chemical Component Dictionary"
            " of 2026-01-01.",
        ),
        ("broken", "DSSP couldn't annotate it: Cannot reconstruct PDBx file, atom data missing"),
    ],
)
def test_a_structure_dssp_cant_annotate_fails_its_own_run_saying_why(
    tmp_path: Path, mode: str, said: str
) -> None:
    result, output = run(tmp_path, {"1xyz.cif": STRUCTURE}, {"1xyz.cif": ["A"]}, mode=mode)

    assert output["updates"] == {"structure": []}
    assert output["messages"] == [{"level": "error", "text": said, "entity": "e1"}]
    assert f"PST-000001: {said}" in result.stdout


def test_an_unreadable_file_fails_its_run_and_the_others_go_on(tmp_path: Path) -> None:
    _, output = run(
        tmp_path,
        {"empty.cif": "", "1xyz.cif": STRUCTURE},
        {"empty.cif": ["A"], "1xyz.cif": ["A"]},
    )

    assert [u["entity"] for u in output["updates"]["structure"]] == ["e2"]
    errors = [m for m in output["messages"] if m["level"] == "error"]
    assert errors == [{"level": "error", "text": "It's empty.", "entity": "e1"}]

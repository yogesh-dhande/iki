"""The protein plugin's structures: a structure file, or a PDB entry fetched from RCSB, read
into a structure linked to the proteins its chains are, in one run's output."""

import gzip
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "protein" / "scripts" / "structures.py"
FILE_ID = "0190e9a4-0000-7000-8000-000000000002"

#: A made-up entry: two chains of one protein, a DNA strand and a zinc ion.
MMCIF = """\
data_1ABC
_entry.id 1ABC
_struct.title 'A made-up kinase, bound to DNA'
_exptl.method 'X-RAY DIFFRACTION'
_refine.ls_d_res_high 2.10
loop_
_entity.id
_entity.type
_entity.pdbx_description
1 polymer 'Tyrosine-protein kinase ABL1'
2 polymer 'DNA (5'-D(*AP*CP*GP*T)-3')'
3 non-polymer 'ZINC ION'
loop_
_entity_poly.entity_id
_entity_poly.type
_entity_poly.pdbx_strand_id
_entity_poly.pdbx_seq_one_letter_code_can
1 'polypeptide(L)' B,A
;MKVW
HEAY
;
2 polydeoxyribonucleotide C ACGT
loop_
_entity_src_gen.entity_id
_entity_src_gen.pdbx_gene_src_scientific_name
_entity_src_gen.pdbx_gene_src_gene
1 'Homo sapiens' 'ABL1, ABL'
loop_
_struct_ref.id
_struct_ref.entity_id
_struct_ref.db_name
_struct_ref.pdbx_db_accession
1 1 UNP P00519
loop_
_pdbx_audit_revision_history.ordinal
_pdbx_audit_revision_history.major_revision
_pdbx_audit_revision_history.minor_revision
_pdbx_audit_revision_history.revision_date
1 1 0 2020-01-15
2 1 1 2021-03-03
2 2 0 2024-05-22
"""


def _atom(serial: int, residue: str, chain: str, number: int) -> str:
    x = 3.8 * number
    return (
        f"ATOM  {serial:5d}  CA  {residue} {chain}{number:4d}    "
        f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 90.00           C"
    )


#: A prediction's PDB file: atoms, and no SEQRES.
PREDICTED = "\n".join(
    [_atom(i, r, "A", i) for i, r in enumerate(["MET", "LYS", "TRP", "SER"], start=1)]
    + ["TER", "END", ""]
)


def run(tmp_path: Path, text: str, name: str) -> tuple[subprocess.CompletedProcess[str], Any]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    run_input = {
        "params": {"file": FILE_ID},
        "files": {FILE_ID: {"name": name, "path": str(path), "size": path.stat().st_size}},
    }
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(SCRIPT), "file"],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if not out.exists():
        return result, None
    output = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


def test_an_mmcif_file_gives_a_structure_linked_to_its_proteins(tmp_path: Path) -> None:
    result, output = run(tmp_path, MMCIF, "kinase.cif")
    assert result.returncode == 0, result.stderr
    protein, structure = output["creates"]
    origin = {
        "source": "mmCIF file",
        "id": "kinase.cif",
        "file": FILE_ID,
        "sha256": hashlib.sha256(MMCIF.encode()).hexdigest(),
    }
    assert protein == {
        "type": "protein.Protein",
        "key": "1",
        "values": {
            "name": "Tyrosine-protein kinase ABL1",
            "description": "Chains A, B of 1ABC.",
            "sequence": "MKVWHEAY",
            "organism": "Homo sapiens",
            "gene": "ABL1",
            "uniprot_accession": "P00519",
        },
        "origin": origin,
    }
    assert structure == {
        "type": "protein.ProteinStructure",
        "values": {
            "structure": FILE_ID,
            "name": "1ABC",
            "description": "A made-up kinase, bound to DNA",
            "pdb_id": "1ABC",
            "method": "xray",
            "resolution": 2.1,
            "chains": [{"$create": "1"}, {"$create": "1"}],
            "chain_ids": ["A", "B"],
            "released": "2020-01-15",
        },
        "origin": origin,
    }
    assert output["messages"] == [
        {
            "level": "info",
            "text": "1ABC: chain C is polydeoxyribonucleotide, not a protein, so it was left out.",
        }
    ]
    assert output["metrics"] == {"structures": 1}


def test_a_pdb_file_without_seqres_gives_the_residues_it_has(tmp_path: Path) -> None:
    result, output = run(tmp_path, PREDICTED, "model_1.pdb")
    assert result.returncode == 0, result.stderr
    protein, structure = output["creates"]
    assert protein["values"]["sequence"] == "MKWS"
    assert protein["values"]["name"] == "model_1 chain A"
    assert structure["values"]["name"] == "model_1"
    assert structure["values"]["chain_ids"] == ["A"]
    assert "pdb_id" not in structure["values"]
    assert structure["origin"]["source"] == "PDB file"


def test_chains_of_the_same_protein_refer_to_one_and_others_to_their_own(
    tmp_path: Path,
) -> None:
    def chain(name: str, residues: list[str], first: int) -> list[str]:
        return [_atom(first + i, r, name, i + 1) for i, r in enumerate(residues)] + ["TER"]

    dimer = ["MET", "LYS", "TRP"]
    text = "\n".join(
        [*chain("A", dimer, 1), *chain("B", dimer, 10), *chain("C", ["GLY", "SER"], 20), "END\n"]
    )
    result, output = run(tmp_path, text, "complex.pdb")
    assert result.returncode == 0, result.stderr
    *proteins, structure = output["creates"]
    assert [(p["key"], p["values"]["sequence"], p["values"]["description"]) for p in proteins] == [
        ("A", "MKW", "Chains A, B of complex."),
        ("C", "GS", "Chain C of complex."),
    ]
    assert structure["values"]["chains"] == [
        {"$create": "A"},
        {"$create": "A"},
        {"$create": "C"},
    ]
    assert structure["values"]["chain_ids"] == ["A", "B", "C"]


@pytest.mark.parametrize(
    ("text", "name", "error"),
    [
        ("", "empty.cif", "It's empty."),
        (
            MMCIF.replace("'polypeptide(L)'", "'polypeptide(D)'"),
            "d.cif",
            "It has no protein chains.",
        ),
        ("HEADER    NOTHING HERE\nEND\n", "nothing.pdb", "It has no protein chains."),
    ],
)
def test_a_file_that_cant_be_read_fails_saying_why(
    tmp_path: Path, text: str, name: str, error: str
) -> None:
    result, output = run(tmp_path, text, name)
    assert result.returncode == 1
    assert output is None
    assert result.stdout.splitlines()[-1].startswith(f"::error {name}: {error}")


# --- Fetching from the PDB -----------------------------------------------------------------------


class FakeRcsb:
    """files.rcsb.org's downloads, from made-up entries."""

    def __init__(self) -> None:
        self.entries = {"1ABC": MMCIF, "2XYZ": MMCIF.replace("1ABC", "2XYZ")}
        self.asked: list[str] = []
        self.failures: list[int] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        url = request.full_url
        self.asked.append(url)
        assert request.get_header("User-agent") == "iki-protein"
        if self.failures:
            code = self.failures.pop(0)
            raise urllib.error.HTTPError(url, code, "Busy", Message(), None)
        entry = url.removeprefix("https://files.rcsb.org/download/").removesuffix(".cif.gz")
        if entry not in self.entries:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        return io.BytesIO(gzip.compress(self.entries[entry].encode()))


@pytest.fixture
def rcsb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """The script, loaded here with RCSB faked."""
    import importlib
    import time
    import urllib.request

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    sys.modules.pop("structures", None)
    module = importlib.import_module("structures")
    fake = FakeRcsb()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setenv("IKI_INPUT", str(tmp_path / "input.json"))
    monkeypatch.setenv("IKI_OUTPUT", str(tmp_path / "out.json"))
    monkeypatch.setenv("IKI_OUT_DIR", str(out_dir))
    yield SimpleNamespace(script=module, fake=fake, slept=slept, out_dir=out_dir)
    sys.modules.pop("structures", None)


def fetched(tmp_path: Path, rcsb: Any, ids: str) -> dict[str, Any]:
    (tmp_path / "input.json").write_text(json.dumps({"params": {"pdb_ids": ids}}))
    sys.argv = ["structures.py", "fetch"]
    rcsb.script.main()
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output


def test_fetches_entries_from_the_pdb_as_mmcif_files_it_attaches(tmp_path: Path, rcsb: Any) -> None:
    output = fetched(tmp_path, rcsb, "1abc, 2XYZ 1ABC")
    assert rcsb.fake.asked == [
        "https://files.rcsb.org/download/1ABC.cif.gz",
        "https://files.rcsb.org/download/2XYZ.cif.gz",
    ]
    assert rcsb.slept == [0.2]
    assert [(c["type"], c.get("key")) for c in output["creates"]] == [
        ("protein.Protein", "1ABC-1"),
        ("protein.ProteinStructure", None),
        ("protein.Protein", "2XYZ-1"),
        ("protein.ProteinStructure", None),
    ]
    structure = output["creates"][1]
    assert structure["values"]["structure"] == {
        "$file": "1ABC.cif",
        "media_type": "chemical/x-mmcif",
    }
    assert (rcsb.out_dir / "1ABC.cif").read_text() == MMCIF
    assert structure["values"]["chains"] == [{"$create": "1ABC-1"}] * 2
    origin = structure["origin"]
    assert {k: origin[k] for k in ("source", "id", "version", "url", "sha256")} == {
        "source": "RCSB PDB",
        "id": "1ABC",
        "version": "revision 2.0",
        "url": "https://www.rcsb.org/structure/1ABC",
        "sha256": hashlib.sha256(MMCIF.encode()).hexdigest(),
    }
    assert origin["retrieved_at"].endswith("+00:00")
    assert output["creates"][0]["origin"] == origin
    assert output["metrics"] == {"structures": 2}


def test_waits_and_asks_again_when_rcsb_is_busy(tmp_path: Path, rcsb: Any) -> None:
    rcsb.fake.failures = [503, 429]
    output = fetched(tmp_path, rcsb, "1ABC")
    assert len(rcsb.fake.asked) == 3
    assert rcsb.slept == [2, 4]
    assert output["metrics"] == {"structures": 1}


@pytest.mark.parametrize(
    ("ids", "error"),
    [
        (
            "1ABC 9ZZZ",
            "::error The PDB has no entry 9ZZZ: it may have been made obsolete. Search for "
            "it at https://www.rcsb.org.",
        ),
        ("1ABC hemoglobin", "::error HEMOGLOBIN isn't a PDB ID, such as 4HHB."),
        (" ", "::error Give at least one PDB ID, such as 4HHB."),
    ],
)
def test_a_fetch_fails_whole_and_says_why(
    tmp_path: Path, rcsb: Any, capsys: pytest.CaptureFixture[str], ids: str, error: str
) -> None:
    with pytest.raises(SystemExit) as stopped:
        fetched(tmp_path, rcsb, ids)
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines()[-1] == error
    assert not (tmp_path / "out.json").exists()

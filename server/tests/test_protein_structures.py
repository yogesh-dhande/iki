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
_entity_src_gen.pdbx_gene_src_ncbi_taxonomy_id
_entity_src_gen.pdbx_gene_src_gene
1 'Homo sapiens' 9606 'ABL1, ABL'
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
loop_
_software.pdbx_ordinal
_software.name
_software.version
_software.classification
1 PHENIX 1.21.2_5419 refinement
2 XDS . 'data reduction'
3 PHENIX 1.21.2_5419 phasing
"""

#: What MMCIF says made it: an experiment, and the software it names, each once.
X_RAY = {
    "kind": "experimental",
    "method": "X-RAY DIFFRACTION",
    "software": [{"name": "PHENIX", "version": "1.21.2_5419"}, {"name": "XDS"}],
}

#: AlphaFold DB's model of crambin, cut to its first residue (CC BY 4.0).
ALPHAFOLD = Path(__file__).parent / "data" / "AF-P01542-F1-model_v6.cif"


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
    # Its organism by its NCBI Taxonomy ID, first (ADR 0140), then the protein from it.
    organism, protein, structure = output["creates"]
    assert organism == {
        "type": "iki/core.Organism",
        "key": "taxon:9606",
        "values": {"taxon_id": 9606, "scientific_name": "Homo sapiens"},
        "fills": ["scientific_name"],
    }
    origin = {
        "source": "mmCIF file",
        "id": "kinase.cif",
        "file": FILE_ID,
        "sha256": hashlib.sha256(MMCIF.encode()).hexdigest(),
    }
    assert protein == {
        "type": "iki/protein.Protein",
        "key": "1",
        "values": {
            "name": "Tyrosine-protein kinase ABL1",
            "description": "Chains A, B of 1ABC.",
            "sequence": "MKVWHEAY",
            "organism": {"$create": "taxon:9606"},
            "gene": "ABL1",
            "uniprot_accession": "P00519",
        },
        "origin": origin,
    }
    assert structure == {
        "type": "iki/protein.ProteinStructure",
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
        # The structure was made by the experiment, the protein's sequence wasn't.
        "origin": {**origin, "made": X_RAY},
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
    # It doesn't say how it was made, so neither does its origin.
    assert "made" not in structure["origin"]


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
    ("expdta", "made"),
    [
        (
            "X-RAY DIFFRACTION; NEUTRON DIFFRACTION",
            {"kind": "experimental", "method": "X-RAY DIFFRACTION, NEUTRON DIFFRACTION"},
        ),
        ("THEORETICAL MODEL", {"kind": "computational", "method": "THEORETICAL MODEL"}),
    ],
)
def test_a_pdb_file_says_how_it_was_made_in_expdta(
    tmp_path: Path, expdta: str, made: dict[str, Any]
) -> None:
    result, output = run(tmp_path, f"EXPDTA    {expdta}\n{PREDICTED}", "model.pdb")
    assert result.returncode == 0, result.stderr
    assert output["creates"][-1]["origin"]["made"] == made
    assert "made" not in output["creates"][0]["origin"]


def test_a_method_that_isnt_wwpdbs_says_nothing_of_how(tmp_path: Path) -> None:
    text = MMCIF.replace("'X-RAY DIFFRACTION'", "'GUESSWORK'")
    result, output = run(tmp_path, text, "odd.cif")
    assert result.returncode == 0, result.stderr
    structure = output["creates"][-1]
    assert structure["values"]["method"] == "other"
    assert "made" not in structure["origin"]


def test_a_modelcif_file_says_what_made_the_model_and_how_sure_it_is(tmp_path: Path) -> None:
    text = ALPHAFOLD.read_text(encoding="utf-8")
    result, output = run(tmp_path, text, "AF-P01542-F1-model_v6.cif")
    assert result.returncode == 0, result.stderr
    organism, protein, structure = output["creates"]
    assert organism["values"] == {
        "taxon_id": 3721,
        "scientific_name": "Crambe hispanica subsp. abyssinica",
    }
    assert protein["values"] == {
        "name": "Crambin",
        "description": "Chain A of AF-P01542-F1-model_v6.",
        "sequence": "TTCCPSIVARSNFNVCRLPGTPEALCATYTGCIIIPGATCPGDYAN",
        # From ModelCIF's target: the UniProt entry it modelled.
        "organism": {"$create": "taxon:3721"},
        "gene": "THI2",
        "uniprot_accession": "P01542",
    }
    assert "made" not in protein["origin"]
    values = structure["values"]
    assert (values["method"], values["chain_ids"]) == ("predicted", ["A"])
    # Its revisions (2021-12-09 first) are the model's: there's no PDB entry to release.
    assert "released" not in values and "pdb_id" not in values
    # AlphaFold, as its software group says; not DSSP, which only annotated it.
    assert structure["origin"]["made"] == {
        "kind": "computational",
        "method": "Ab initio model",
        "software": [{"name": "AlphaFold", "version": "2.0"}],
        "details": {"mean_plddt": 93.48},
    }


#: 2RH1's chain A, as wwPDB annotates it: the β2-adrenergic receptor (UniProt P07550) with
#: T4 lysozyme (P00720) in its third intracellular loop, and an N-terminal FLAG tag. Its
#: sequence is cut to a residue of each segment; the ranges are the entry's.
CHIMERA = """\
data_2RH1
_entry.id 2RH1
_exptl.method 'X-RAY DIFFRACTION'
_refine.ls_d_res_high 2.400
loop_
_entity.id
_entity.type
_entity.pdbx_description
1 polymer 'beta-2-adrenergic receptor/T4-lysozyme chimera'
loop_
_entity_poly.entity_id
_entity_poly.type
_entity_poly.pdbx_strand_id
_entity_poly.pdbx_seq_one_letter_code_can
1 'polypeptide(L)' A DMNK
loop_
_entity_src_gen.entity_id
_entity_src_gen.pdbx_src_id
_entity_src_gen.pdbx_beg_seq_num
_entity_src_gen.pdbx_end_seq_num
_entity_src_gen.pdbx_gene_src_gene
_entity_src_gen.pdbx_gene_src_scientific_name
1 1 8 237 'ADRB2, ADRB2R, B2AR / E' 'Homo sapiens'
1 3 398 500 'ADRB2, ADRB2R, B2AR / E' 'Homo sapiens'
1 2 238 397 'ADRB2, ADRB2R, B2AR / E' 'Enterobacteria phage T4'
loop_
_struct_ref.id
_struct_ref.db_name
_struct_ref.pdbx_db_accession
_struct_ref.entity_id
1 UNP P07550 1
2 UNP P00720 1
3 UNP P07550 1
loop_
_struct_ref_seq.align_id
_struct_ref_seq.ref_id
_struct_ref_seq.pdbx_strand_id
_struct_ref_seq.seq_align_beg
_struct_ref_seq.seq_align_end
_struct_ref_seq.pdbx_db_accession
_struct_ref_seq.db_align_beg
_struct_ref_seq.db_align_end
1 1 A 8 237 P07550 1 230
2 2 A 238 397 P00720 2 161
3 3 A 398 500 P07550 263 365
"""


def test_a_chimeric_chain_is_none_of_its_parts_and_says_which_residues_are_which(
    tmp_path: Path,
) -> None:
    """wwPDB gives a chimera a reference and a source for each segment. 2RH1's last are
    the receptor's, and its sources' last is T4's: neither is the whole chain."""
    for text in (CHIMERA, CHIMERA.replace("1 1 UNP", "1 9 UNP")):
        result, output = run(tmp_path, text, "2rh1.cif")
        assert result.returncode == 0, result.stderr
        protein = output["creates"][0]["values"]
        assert protein == {
            "name": "beta-2-adrenergic receptor/T4-lysozyme chimera",
            "description": "Chain A of 2RH1, a chimera of UniProt P07550 (residues 8–237 "
            "and 398–500) and P00720 (residues 238–397); from Homo sapiens (residues 8–237 "
            "and 398–500) and Enterobacteria phage T4 (residues 238–397).",
            "sequence": "DMNK",
        }
        assert {
            "level": "info",
            "text": "2RH1: chain A is a chimera of P07550 and P00720, so its protein has no "
            "one UniProt accession, organism or gene: its description says which residues "
            "are which.",
        } in output["messages"]
    # Reordered, the rows still name no one entry.
    swapped = CHIMERA.replace("1 UNP P07550 1\n2 UNP P00720 1", "1 UNP P00720 1\n2 UNP P07550 1")
    _, output = run(tmp_path, swapped, "2rh1.cif")
    assert "uniprot_accession" not in output["creates"][0]["values"]


def test_one_entry_in_several_segments_is_that_entry(tmp_path: Path) -> None:
    text = (
        CHIMERA.replace("P00720", "P07550")
        .replace("Enterobacteria phage T4", "Homo sapiens")
        .replace(
            "_entity_src_gen.pdbx_gene_src_scientific_name\n",
            "_entity_src_gen.pdbx_gene_src_scientific_name\n"
            "_entity_src_gen.pdbx_gene_src_ncbi_taxonomy_id\n",
        )
        .replace("'Homo sapiens'\n", "'Homo sapiens' 9606\n")
    )
    _, output = run(tmp_path, text, "2rh1.cif")
    organism, made = output["creates"][:2]
    # Its organism, by its NCBI Taxonomy ID (ADR 0140).
    assert organism["values"] == {"taxon_id": 9606, "scientific_name": "Homo sapiens"}
    protein = made["values"]
    assert (protein["uniprot_accession"], protein["organism"], protein["gene"]) == (
        "P07550",
        {"$create": "taxon:9606"},
        "ADRB2",
    )
    assert protein["description"] == "Chain A of 2RH1."


def test_a_resolution_with_its_uncertainty_is_read_as_cif_writes_it(tmp_path: Path) -> None:
    """CIF numbers may carry a standard uncertainty in brackets (2.10(5)), as gemmi reads
    them; one that isn't a number is left out, and the run says so."""
    _, output = run(tmp_path, MMCIF.replace("2.10\n", "2.10(5)\n"), "kinase.cif")
    assert output["creates"][-1]["values"]["resolution"] == 2.1
    result, output = run(tmp_path, MMCIF.replace("2.10\n", "high\n"), "kinase.cif")
    assert result.returncode == 0, result.stderr
    assert "resolution" not in output["creates"][-1]["values"]
    assert {
        "level": "info",
        "text": "1ABC: its resolution, high, isn't a number, so it was left out.",
    } in output["messages"]


@pytest.mark.parametrize(
    ("entry", "kept"),
    [("pdb_00001abc", "1ABC"), ("PDB_10021ABC", "PDB_10021ABC"), ("not-an-id", None)],
)
def test_a_file_with_an_extended_pdb_id_keeps_it(
    tmp_path: Path, entry: str, kept: str | None
) -> None:
    """wwPDB's extended IDs: an entry's pdb_0000 form is its four-character ID."""
    _, output = run(tmp_path, MMCIF.replace("_entry.id 1ABC", f"_entry.id {entry}"), "x.cif")
    values = output["creates"][-1]["values"]
    assert values.get("pdb_id") == kept
    # Only a PDB entry has a release.
    assert values.get("released") == ("2020-01-15" if kept else None)


def test_a_modelcif_file_without_chain_ids_takes_its_residues(tmp_path: Path) -> None:
    text = ALPHAFOLD.read_text(encoding="utf-8").replace(
        "_entity_poly.pdbx_strand_id               A\n", ""
    )
    result, output = run(tmp_path, text, "model.cif")
    assert result.returncode == 0, result.stderr
    assert output["creates"][-1]["values"]["chain_ids"] == ["A"]


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
    # Both are from one organism, made once, where it's first needed (ADR 0140).
    assert [(c["type"], c.get("key")) for c in output["creates"]] == [
        ("iki/core.Organism", "taxon:9606"),
        ("iki/protein.Protein", "1ABC-1"),
        ("iki/protein.ProteinStructure", None),
        ("iki/protein.Protein", "2XYZ-1"),
        ("iki/protein.ProteinStructure", None),
    ]
    structure = output["creates"][2]
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
    assert origin["made"] == X_RAY
    assert output["creates"][1]["origin"] == {k: v for k, v in origin.items() if k != "made"}
    assert output["metrics"] == {"structures": 2}


def test_fetches_an_entry_by_its_extended_pdb_id(tmp_path: Path, rcsb: Any) -> None:
    """pdb_00001abc is 1ABC (wwPDB's extended IDs), so it's fetched, and kept, once."""
    rcsb.fake.entries["PDB_10021ABC"] = MMCIF.replace("_entry.id 1ABC", "_entry.id pdb_10021abc")
    output = fetched(tmp_path, rcsb, "pdb_00001abc 1ABC pdb_10021abc")
    assert rcsb.fake.asked == [
        "https://files.rcsb.org/download/1ABC.cif.gz",
        "https://files.rcsb.org/download/PDB_10021ABC.cif.gz",
    ]
    structures = [c for c in output["creates"] if c["type"].endswith("Structure")]
    assert [(s["values"]["pdb_id"], s["origin"]["id"]) for s in structures] == [
        ("1ABC", "1ABC"),
        ("PDB_10021ABC", "PDB_10021ABC"),
    ]


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
        ("1ABC hemoglobin", "::error HEMOGLOBIN isn't a PDB ID, such as 4HHB or pdb_00004hhb."),
        ("pdb_1abc", "::error PDB_1ABC isn't a PDB ID, such as 4HHB or pdb_00004hhb."),
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


# --- Fetching from AlphaFold DB ------------------------------------------------------------------

API = "https://alphafold.ebi.ac.uk/api/prediction/"
FILES = "https://alphafold.ebi.ac.uk/files/"


def _prediction(accession: str, model_id: str, version: int) -> dict[str, Any]:
    """An entry of AlphaFold DB's API's answer, as it gives them (some of its fields)."""
    return {
        "toolUsed": "AlphaFold Monomer v2.0 pipeline",
        "providerId": "GDM",
        "entityType": "protein",
        "modelEntityId": model_id,
        "globalMetricValue": 93.5,
        "latestVersion": version,
        "allVersions": list(range(1, version + 1)),
        "uniprotAccession": accession,
        "uniprotDescription": "Crambin",
        "cifUrl": f"{FILES}{model_id}-model_v{version}.cif",
        "pdbUrl": f"{FILES}{model_id}-model_v{version}.pdb",
        "isComplex": False,
        "entryId": model_id,
    }


class FakeAlphaFold:
    """alphafold.ebi.ac.uk's API and files, with crambin's model."""

    def __init__(self) -> None:
        self.models = {"P01542": [_prediction("P01542", "AF-P01542-F1", 6)]}
        self.files = {"AF-P01542-F1-model_v6.cif": ALPHAFOLD.read_bytes()}
        self.asked: list[str] = []
        self.failures: list[int] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        url = request.full_url
        self.asked.append(url)
        assert request.get_header("User-agent") == "iki-protein"
        if self.failures:
            raise urllib.error.HTTPError(url, self.failures.pop(0), "Busy", Message(), None)
        if url.startswith(API):
            assert request.get_header("Accept") == "application/json"
            found = self.models.get(url.removeprefix(API))
            if found is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
            return io.BytesIO(json.dumps(found).encode())
        name = url.removeprefix(FILES)
        if name not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        return io.BytesIO(self.files[name])


@pytest.fixture
def alphafold(rcsb: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The script, loaded here with AlphaFold DB faked."""
    import urllib.request

    fake = FakeAlphaFold()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return SimpleNamespace(script=rcsb.script, fake=fake, slept=rcsb.slept, out_dir=rcsb.out_dir)


def predicted(tmp_path: Path, alphafold: Any, accessions: str) -> dict[str, Any]:
    (tmp_path / "input.json").write_text(json.dumps({"params": {"accessions": accessions}}))
    sys.argv = ["structures.py", "alphafold"]
    alphafold.script.main()
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output


def test_fetches_alphafold_dbs_model_saying_what_made_it(tmp_path: Path, alphafold: Any) -> None:
    output = predicted(tmp_path, alphafold, "p01542, P01542")
    assert alphafold.fake.asked == [f"{API}P01542", f"{FILES}AF-P01542-F1-model_v6.cif"]
    assert alphafold.slept == [0.2]
    _, protein, structure = output["creates"]
    assert protein["key"] == "AF-P01542-F1-1"
    assert protein["values"]["uniprot_accession"] == "P01542"
    values = structure["values"]
    assert values["structure"] == {
        "$file": "AF-P01542-F1-model_v6.cif",
        "media_type": "chemical/x-mmcif",
    }
    assert (alphafold.out_dir / "AF-P01542-F1-model_v6.cif").read_bytes() == ALPHAFOLD.read_bytes()
    assert {k: values[k] for k in ("name", "description", "method", "chains", "chain_ids")} == {
        "name": "AF-P01542-F1",
        "description": "Crambin (P01542), predicted by AlphaFold Monomer v2.0 pipeline, from "
        "AlphaFold DB.",
        "method": "predicted",
        "chains": [{"$create": "AF-P01542-F1-1"}],
        "chain_ids": ["A"],
    }
    origin = structure["origin"]
    assert {k: v for k, v in origin.items() if k != "retrieved_at"} == {
        "source": "AlphaFold DB",
        "id": "AF-P01542-F1",
        "version": "6",
        "url": "https://alphafold.ebi.ac.uk/entry/P01542",
        "sha256": hashlib.sha256(ALPHAFOLD.read_bytes()).hexdigest(),
        # What its file says, not its API: the software that made it, and how sure it is.
        "made": {
            "kind": "computational",
            "method": "Ab initio model",
            "software": [{"name": "AlphaFold", "version": "2.0"}],
            "details": {"mean_plddt": 93.48},
        },
    }
    assert origin["retrieved_at"].endswith("+00:00")
    assert protein["origin"] == {k: v for k, v in origin.items() if k != "made"}
    assert output["metrics"] == {"structures": 1}


def test_takes_each_model_alphafold_db_lists(tmp_path: Path, alphafold: Any) -> None:
    second = _prediction("P01542", "AF-0000000365840314", 1)
    alphafold.fake.models["P01542"].append(second)
    alphafold.fake.files["AF-0000000365840314-model_v1.cif"] = ALPHAFOLD.read_bytes().replace(
        b"AF-P01542-F1", b"AF-0000000365840314"
    )
    output = predicted(tmp_path, alphafold, "P01542")
    names = [c["values"]["name"] for c in output["creates"] if c["type"].endswith("Structure")]
    assert names == ["AF-P01542-F1", "AF-0000000365840314"]
    assert output["metrics"] == {"structures": 2}


def test_waits_and_asks_again_when_alphafold_db_is_busy(tmp_path: Path, alphafold: Any) -> None:
    alphafold.fake.failures = [503, 429]
    output = predicted(tmp_path, alphafold, "P01542")
    assert len(alphafold.fake.asked) == 4
    assert alphafold.slept == [2, 4, 0.2]
    assert output["metrics"] == {"structures": 1}


@pytest.mark.parametrize(
    ("accessions", "error"),
    [
        (
            "P01542 Q00000",
            "::error AlphaFold DB has no prediction of Q00000. Search for it at "
            "https://alphafold.ebi.ac.uk.",
        ),
        (
            "P69905-2",
            "::error AlphaFold DB predicts UniProt's canonical sequences: give P69905, not "
            "the isoform P69905-2.",
        ),
        ("crambin", "::error CRAMBIN isn't a UniProt accession, such as P69905."),
        (" ", "::error Give at least one UniProt accession, such as P69905."),
    ],
)
def test_a_fetch_from_alphafold_db_fails_whole_and_says_why(
    tmp_path: Path,
    alphafold: Any,
    capsys: pytest.CaptureFixture[str],
    accessions: str,
    error: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        predicted(tmp_path, alphafold, accessions)
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines()[-1] == error
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"cifUrl": "https://example.org/AF-P01542-F1-model_v6.cif"},
        {"latestVersion": "6"},
        {"modelEntityId": "../../etc"},
    ],
)
def test_an_answer_it_cant_trust_fails_the_fetch(
    tmp_path: Path,
    alphafold: Any,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
) -> None:
    alphafold.fake.models["P01542"][0].update(change)
    with pytest.raises(SystemExit):
        predicted(tmp_path, alphafold, "P01542")
    assert capsys.readouterr().out.splitlines()[-1] == (
        "::error AlphaFold DB's answer for P01542 doesn't give a model's ID, version and "
        "mmCIF file as Iki reads them."
    )
    assert alphafold.fake.asked == [f"{API}P01542"]

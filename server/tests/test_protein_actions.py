"""The protein plugin's actions: what its calculations, translations, imports and UniProt
fetches give, run as a worker runs them (an input.json in, an output.json out)."""

import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iki_spec import schemas

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "protein" / "scripts"
#: Human haemoglobin's alpha chain (UniProt P69905).
HBA = (
    "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALSALSDLHAH"
    "KLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
)


def entity(i: int, **values: Any) -> dict[str, Any]:
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"X-{i}", "values": values}


def script(
    tmp_path: Path, name: str, run_input: dict[str, Any], *args: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(SCRIPTS / name), *args],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if not out.exists():
        return result, None
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


# --- Alignment -----------------------------------------------------------------------------------

#: Human haemoglobin's beta chain (UniProt P68871).
HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLDNLKGTF"
    "ATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)
NEEDLE = {"mode": "global", "matrix": "BLOSUM62", "gap_open": 10, "gap_extend": 0.5}


def aligned(
    tmp_path: Path, query: str, target: str, **params: Any
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    return script(
        tmp_path,
        "align.py",
        {
            "run": {"hrid": "RUN-000007"},
            "params": {**NEEDLE, **params},
            "inputs": {
                "query": [entity(1, sequence=query, name="HBA_HUMAN")],
                "target": [entity(2, sequence=target, name="HBB_HUMAN")],
            },
        },
    )


def test_aligns_two_proteins_as_needle_and_keeps_the_alignment(tmp_path: Path) -> None:
    result, output = aligned(tmp_path, HBA, HBB)

    assert result.returncode == 0, result.stderr
    assert output is not None
    assert output["metrics"] == {
        "score": 292.5,
        "length": 149,
        "identity_percent": 43.6,
        "similarity_percent": 60.4,
        "gaps_percent": 6.0,
    }
    text = (tmp_path / "out" / "alignment.txt").read_text()
    assert "# Matrix: BLOSUM62" in text and "# Identity:   65/149 (43.6%)" in text
    assert "X-1      1 MV-LSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF-D     48" in text
    assert text.rstrip().endswith(
        "X-2     99 VDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH    147"
    )
    fasta = (tmp_path / "out" / "alignment.fasta").read_text().split(">")[1:]
    rows = ["".join(record.splitlines()[1:]) for record in fasta]
    assert [r.replace("-", "") for r in rows] == [HBA, HBB]
    assert len(rows[0]) == len(rows[1]) == 149


def test_end_gaps_are_free_globally_and_local_finds_the_best_part(tmp_path: Path) -> None:
    part = HBA[30:80]
    _, whole = aligned(tmp_path, HBA, part)
    _, local = aligned(tmp_path, HBA, part, mode="local")

    assert whole is not None and local is not None
    assert whole["metrics"]["score"] == local["metrics"]["score"]
    assert whole["metrics"]["length"] == len(HBA)
    assert local["metrics"]["length"] == 50 and local["metrics"]["identity_percent"] == 100
    assert "X-1     31 " in (tmp_path / "out" / "alignment.txt").read_text()


def test_penalties_and_matrices_change_the_alignment(tmp_path: Path) -> None:
    _, default = aligned(tmp_path, HBA, HBB)
    _, costly = aligned(tmp_path, HBA, HBB, gap_open=50)
    _, pam = aligned(tmp_path, HBA, HBB, matrix="PAM250")

    assert default is not None and costly is not None and pam is not None
    assert costly["metrics"]["score"] < default["metrics"]["score"]
    assert pam["metrics"]["score"] != default["metrics"]["score"]


@pytest.mark.parametrize(
    ("query", "target", "why"),
    [
        ("*", HBB, "X-1 has no residues to align."),
        ("A" * 20_000, "A" * 20_000, "are too long to align here: 20,000 by 20,000 residues"),
    ],
)
def test_what_cant_be_aligned_fails_its_run_saying_why(
    tmp_path: Path, query: str, target: str, why: str
) -> None:
    result, output = aligned(tmp_path, query, target)

    assert result.returncode == 1 and output is None
    assert why in result.stdout


# --- Properties ----------------------------------------------------------------------------------


def properties(tmp_path: Path, *sequences: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    proteins = [entity(i, sequence=s) for i, s in enumerate(sequences)]
    result, output = script(
        tmp_path, "proteins.py", {"inputs": {"protein": proteins}}, "properties"
    )
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert result.stdout.splitlines()[-1].startswith("::progress 1.00 ")
    found = {u["entity"][-1]: u["values"] for u in output["updates"]["protein"]}
    return found, output["messages"]


def test_properties_are_protparams(tmp_path: Path) -> None:
    found = properties(tmp_path, HBA)[0]["0"]
    # ExPASy's ProtParam gives 15257.40 (its residue masses differ in the last digits),
    # pI 8.72, 9970 M⁻¹cm⁻¹ (one cysteine: no cystine), GRAVY 0.048, instability 6.97.
    assert found == {
        "length": 142,
        "molecular_weight": 15257.36,
        "isoelectric_point": 8.72,
        "extinction_coefficient_oxidized": 9970,
        "extinction_coefficient_reduced": 9970,
        "gravy": 0.048,
        "instability_index": 6.97,
    }


def test_cystines_add_to_the_oxidized_extinction_coefficient(tmp_path: Path) -> None:
    found, _ = properties(tmp_path, "MCWCYC")
    # Pace et al.: W 5500, Y 1490, and 125 for each cystine (two of the three cysteines).
    assert found["0"]["extinction_coefficient_reduced"] == 5500 + 1490
    assert found["0"]["extinction_coefficient_oxidized"] == 5500 + 1490 + 125


def test_a_final_stop_is_left_out_and_other_residues_leave_only_the_length(
    tmp_path: Path,
) -> None:
    found, messages = properties(tmp_path, "mkwv*", "MKXV", "MK*V")
    assert (
        found["0"]["molecular_weight"] == properties(tmp_path, "MKWV")[0]["0"]["molecular_weight"]
    )
    assert found["0"]["length"] == 5
    assert found["1"] == {"length": 4}
    assert found["2"] == {"length": 4}
    assert [(m["level"], m["entity"][-1], m["text"]) for m in messages] == [
        (
            "warning",
            "1",
            "It has X at residue 3, so only its length was set: the other properties need "
            "the 20 standard amino acids alone.",
        ),
        (
            "warning",
            "2",
            "It has a stop (*) at residue 3, so only its length was set: the other "
            "properties need the 20 standard amino acids alone.",
        ),
    ]


# --- Translation ---------------------------------------------------------------------------------

PARAMS = {"strand": "forward", "frame": 1, "table": 1, "to_stop": True}


def translated(
    tmp_path: Path, sequences: list[dict[str, Any]], **params: Any
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    run_input = {"params": PARAMS | params, "inputs": {"sequence": sequences}}
    return script(tmp_path, "proteins.py", run_input, "translate")


def test_translates_into_a_protein_linked_to_its_dna(tmp_path: Path) -> None:
    dna = entity(7, bases="atggccaagtaaggg", name="pDemo", organism="Escherichia coli")
    result, output = translated(tmp_path, [dna])
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert output["creates"] == [
        {
            "type": "protein.Protein",
            "values": {
                "name": "pDemo (+1)",
                "description": (
                    "Translated from X-7, frame +1, with NCBI's genetic code 1 (Standard)."
                ),
                "sequence": "MAK",
                "encoded_by": dna["id"],
                "organism": "Escherichia coli",
            },
            "derived_from": [dna["id"]],
        }
    ]


@pytest.mark.parametrize(
    ("params", "sequence"),
    [
        ({"to_stop": False}, "MAK*G"),
        ({"frame": 2}, "WPSK"),  # TGG CCA AGT AAG; the last two bases are left out
        ({"strand": "reverse"}, "PLLGH"),  # CCC TTA CTT GGC CAT
        ({"strand": "reverse", "frame": 3}, "LTWP"),  # CTT ACT TGG CCA, and T left out
        ({"table": 2, "to_stop": False}, "MAK*G"),
    ],
)
def test_frames_strands_and_codes(tmp_path: Path, params: dict[str, Any], sequence: str) -> None:
    _, output = translated(tmp_path, [entity(1, bases="ATGGCCAAGTAAGGG")], **params)
    assert output is not None
    assert output["creates"][0]["values"]["sequence"] == sequence


def test_genetic_codes_differ_where_ncbi_says(tmp_path: Path) -> None:
    # TGA is a stop in the standard code and tryptophan in vertebrate mitochondria; AGA
    # is arginine and a stop.
    bases = "ATGTGAAGA"
    found = {}
    for table in (1, 2):
        _, output = translated(tmp_path, [entity(1, bases=bases)], table=table, to_stop=False)
        assert output is not None
        found[table] = output["creates"][0]["values"]["sequence"]
    assert found == {1: "M*R", 2: "MW*"}


def test_ambiguous_codons_give_what_every_reading_agrees_on(tmp_path: Path) -> None:
    # GAY is D either way; NNN could be anything.
    _, output = translated(tmp_path, [entity(1, bases="ATGGAYNNN")])
    assert output is not None
    assert output["creates"][0]["values"]["sequence"] == "MDX"


def test_a_sequence_that_gives_no_residues_fails_its_own_run(tmp_path: Path) -> None:
    good, stop = entity(1, bases="ATGAAA"), entity(2, bases="TAAATG", name="pStop")
    result, output = translated(tmp_path, [good, stop])
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert [c["derived_from"] for c in output["creates"]] == [[good["id"]]]
    assert output["messages"] == [
        {
            "level": "error",
            "text": "pStop gives no residues in frame +1: it starts with a stop codon.",
            "entity": stop["id"],
        }
    ]


def test_a_genetic_code_ncbi_doesnt_use_fails_the_run(tmp_path: Path) -> None:
    result, output = translated(tmp_path, [entity(1, bases="ATG")], table=7)
    assert result.returncode == 1
    assert output is None
    assert result.stdout.splitlines()[-1] == (
        "::error NCBI has no genetic code 7: choose one of 1, 2, 3, 4, 5, 6, 9, 10, 11, 12, "
        "13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33."
    )


# --- Importing files -----------------------------------------------------------------------------

FILE_ID = "0190e9a4-0000-7000-8000-000000000001"
UNIPROT_FASTA = f"""\
>sp|P69905|HBA_HUMAN Hemoglobin subunit alpha OS=Homo sapiens OX=9606 GN=HBA1 PE=1 SV=2
{HBA[:60]}
{HBA[60:120]}
{HBA[120:]}
"""


def imported(
    tmp_path: Path, text: str, name: str = "proteins.fasta"
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    run_input = {
        "params": {"file": FILE_ID},
        "files": {FILE_ID: {"name": name, "path": str(path), "size": path.stat().st_size}},
    }
    return script(tmp_path, "imports.py", run_input)


def test_uniprot_headers_give_the_entry_accession_organism_and_gene(tmp_path: Path) -> None:
    result, output = imported(tmp_path, UNIPROT_FASTA)
    assert result.returncode == 0, result.stderr
    assert output is not None
    (create,) = output["creates"]
    assert create["values"] == {
        "name": "HBA_HUMAN",
        "description": "Hemoglobin subunit alpha",
        "uniprot_accession": "P69905",
        "organism": "Homo sapiens",
        "gene": "HBA1",
        "sequence": HBA,
    }
    assert create["origin"] == {
        "source": "FASTA file",
        "id": "HBA_HUMAN",
        "file": FILE_ID,
        "sha256": hashlib.sha256(UNIPROT_FASTA.encode()).hexdigest(),
    }
    assert output["metrics"] == {"records": 1}


def test_other_headers_give_a_name_a_description_and_an_organism_in_brackets(
    tmp_path: Path,
) -> None:
    text = (
        ">NP_000549.1 hemoglobin subunit alpha [Homo sapiens]\nMVLSPADKTN\n"
        ">tr|A0A0|X_Y A protein OS=Mus musculus\nmkwv*\n"
        ">\nMKV\n"
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert [c["values"] for c in output["creates"]] == [
        {
            "name": "NP_000549.1",
            "description": "hemoglobin subunit alpha",
            "organism": "Homo sapiens",
            "sequence": "MVLSPADKTN",
        },
        # Not an accession UniProt would give, so only its entry name is kept.
        {"name": "X_Y", "description": "A protein", "organism": "Mus musculus", "sequence": "MKWV"},
        {"sequence": "MKV"},
    ]
    assert output["creates"][2]["origin"]["id"] == "Record 3"


def test_a_large_file_goes_in_a_creates_file(tmp_path: Path) -> None:
    text = "".join(f">p{i}\nMK{'W' * (i % 7)}V\n" for i in range(1500))
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert output["creates_file"] == {"$file": "creates.ndjson"}
    lines = (tmp_path / "out" / "creates.ndjson").read_text().splitlines()
    assert len(lines) == 1500
    assert json.loads(lines[-1])["values"]["name"] == "p1499"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (">a\nMK*V\n", "Record 1 (a) has a stop (*) at residue 3: import the protein up to it."),
        (">a\nMK-V\n", "Record 1 (a) has a gap (-) at residue 3: import unaligned sequences."),
        (
            ">a\nMK7V\n>b\nMK!\n",
            "Record 2 (b) has '!' at residue 3, which isn't an amino acid's code.",
        ),
        (">a\n\n", "Record 1 (a) has no residues."),
        ("MKV\n", "It isn't FASTA: records start with '>'."),
        ("\n\n", "It isn't FASTA: records start with '>'."),
    ],
)
def test_a_record_that_cant_be_read_fails_the_import_saying_why(
    tmp_path: Path, text: str, error: str
) -> None:
    result, output = imported(tmp_path, text)
    assert result.returncode == 1
    assert output is None
    assert result.stdout.splitlines()[-1] == f"::error proteins.fasta: {error}"


# --- Fetching from UniProt -----------------------------------------------------------------------

HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLDNLKGTFATLSE"
    "LHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)


class FakeUniProt:
    """UniProt's accessions endpoint, answering from a few entries (in its own order)."""

    def __init__(self) -> None:
        self.records = {
            "P69905": UNIPROT_FASTA,
            "P68871": ">sp|P68871|HBB_HUMAN Hemoglobin subunit beta OS=Homo sapiens "
            f"OX=9606 GN=HBB PE=1 SV=2\n{HBB}\n",
        }
        self.asked: list[dict[str, list[str]]] = []
        self.failures: list[int] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        assert request.full_url.startswith("https://rest.uniprot.org/uniprotkb/accessions?")
        assert request.get_header("User-agent") == "iki-protein"
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.asked.append(query)
        if self.failures:
            code = self.failures.pop(0)
            raise urllib.error.HTTPError(request.full_url, code, "Slow down", Message(), None)
        wanted = query["accessions"][0].split(",")
        body = "".join(text for acc, text in sorted(self.records.items()) if acc in wanted)
        response = io.BytesIO(body.encode())
        headers = Message()
        headers["X-UniProt-Release"] = "2026_04"
        response.headers = headers  # type: ignore[attr-defined]
        return response


@pytest.fixture
def uniprot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """The fetch script, loaded here with UniProt faked."""
    import importlib
    import time
    import urllib.request

    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("uniprot", "imports"):
        sys.modules.pop(name, None)  # the dna plugin's tests load their own `imports`
    module = importlib.import_module("uniprot")
    fake = FakeUniProt()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setenv("IKI_INPUT", str(tmp_path / "input.json"))
    monkeypatch.setenv("IKI_OUTPUT", str(tmp_path / "out.json"))
    yield SimpleNamespace(script=module, fake=fake, slept=slept)
    for name in ("uniprot", "imports"):
        sys.modules.pop(name, None)


def fetched(tmp_path: Path, uniprot: Any, accessions: str) -> dict[str, Any]:
    (tmp_path / "input.json").write_text(json.dumps({"params": {"accessions": accessions}}))
    uniprot.script.main()
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output


def test_fetches_entries_from_uniprot_in_the_order_asked(tmp_path: Path, uniprot: Any) -> None:
    output = fetched(tmp_path, uniprot, "p69905, P68871\nP69905")
    assert [a["accessions"] for a in uniprot.fake.asked] == [["P69905,P68871"]]
    assert [a["format"] for a in uniprot.fake.asked] == [["fasta"]]
    hba, hbb = output["creates"]
    assert (hba["values"]["name"], hbb["values"]["name"]) == ("HBA_HUMAN", "HBB_HUMAN")
    assert hbb["values"]["sequence"] == HBB
    origin = hba["origin"]
    assert {k: origin[k] for k in ("source", "id", "version", "url")} == {
        "source": "UniProtKB",
        "id": "P69905",
        "version": "release 2026_04, sequence version 2",
        "url": "https://www.uniprot.org/uniprotkb/P69905/entry",
    }
    assert origin["retrieved_at"].endswith("+00:00")
    assert origin["sha256"] == hashlib.sha256(UNIPROT_FASTA.encode()).hexdigest()
    assert output["metrics"] == {"records": 2}


def test_asks_in_batches_with_a_pause_between(
    tmp_path: Path, uniprot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot.script, "BATCH", 1)
    fetched(tmp_path, uniprot, "P68871 P69905")
    assert [a["accessions"] for a in uniprot.fake.asked] == [["P68871"], ["P69905"]]
    assert uniprot.slept == [0.2]


def test_waits_and_asks_again_when_uniprot_says_to_slow_down(tmp_path: Path, uniprot: Any) -> None:
    uniprot.fake.failures = [429, 503]
    output = fetched(tmp_path, uniprot, "P69905")
    assert len(uniprot.fake.asked) == 3
    assert uniprot.slept == [2, 4]
    assert output["creates"][0]["values"]["uniprot_accession"] == "P69905"


@pytest.mark.parametrize(
    ("accessions", "error"),
    [
        (
            "P69905 Q99999",
            "::error UniProt has no entry Q99999: it may have been merged into another or "
            "made obsolete. Search for it at https://www.uniprot.org.",
        ),
        (
            "P69905 HBA_HUMAN",
            "::error HBA_HUMAN isn't a UniProtKB accession, such as P69905 or P69905-2.",
        ),
        (" , ", "::error Give at least one accession, such as P69905."),
    ],
)
def test_a_fetch_fails_whole_and_says_why(
    tmp_path: Path,
    uniprot: Any,
    capsys: pytest.CaptureFixture[str],
    accessions: str,
    error: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        fetched(tmp_path, uniprot, accessions)
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines()[-1] == error
    assert not (tmp_path / "out.json").exists()

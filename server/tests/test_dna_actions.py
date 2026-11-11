"""The DNA plugin's actions: what its sequence calculations give, run as a worker runs
them (an input.json in, an output.json out)."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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


# --- Importing files -----------------------------------------------------------------------------

IMPORTS = SCRIPT.with_name("imports.py")
FILE_ID = "0190e9a4-0000-7000-8000-000000000001"


def imported(
    tmp_path: Path, text: str, *, name: str = "seqs.txt", topology: str = "linear"
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    run_input = {
        "params": {"file": FILE_ID, "topology": topology},
        "files": {FILE_ID: {"name": name, "path": str(path)}},
    }
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(IMPORTS)],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(out)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if not out.exists():
        return result, None
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


BASES = ("gattacagat" * 6).upper()
GENBANK = f"""\
LOCUS       pDemo                     60 bp    DNA     circular SYN 11-NOV-2026
DEFINITION  A demonstration plasmid, cut down to a
            fragment.
ACCESSION   L09137
VERSION     L09137.2
KEYWORDS    .
SOURCE      synthetic construct
  ORGANISM  synthetic construct
            other sequences; artificial sequences.
FEATURES             Location/Qualifiers
     source          1..60
                     /organism="synthetic construct"
                     /mol_type="other DNA"
     promoter        complement(5..20)
                     /label="lac promoter"
                     /note="a long note that wraps onto
                     the next line"
     CDS             join(50..60,
                     1..10)
                     /gene="bla"
                     /translation="MSIQHFRVAL
                     IPFFAAF"
     misc_feature    J00194.1:100..202
                     /note="elsewhere"
ORIGIN
        1 {" ".join(BASES[i : i + 10].lower() for i in range(0, 60, 10))}
//
"""


def test_a_genbank_record_keeps_its_names_features_and_where_it_came_from(
    tmp_path: Path,
) -> None:
    result, output = imported(tmp_path, GENBANK + "\n" + GENBANK.replace("pDemo", "pDemo2"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    first, second = output["creates"]
    assert first["type"] == "dna.DNASequence"
    values = first["values"]
    assert values == {
        "name": "pDemo",
        "description": "A demonstration plasmid, cut down to a fragment",
        "organism": "synthetic construct",
        "accession": "L09137.2",
        "bases": BASES,
        "topology": "circular",
        "features": [
            {
                "type": "promoter",
                "start": 5,
                "end": 20,
                "strand": "reverse",
                "name": "lac promoter",
                "qualifiers": {
                    "label": "lac promoter",
                    "note": "a long note that wraps onto the next line",
                },
            },
            {
                # Across the origin of the circular plasmid.
                "type": "CDS",
                "start": 50,
                "end": 10,
                "strand": "forward",
                "name": "bla",
                "qualifiers": {
                    "gene": "bla",
                    "translation": "MSIQHFRVALIPFFAAF",
                    "location": "join(50..60,1..10)",
                },
            },
        ],
    }
    record = GENBANK.encode()
    assert first["origin"] == {
        "source": "GenBank file",
        "id": "pDemo",
        "version": "L09137.2",
        "file": FILE_ID,
        "sha256": hashlib.sha256(record).hexdigest(),
    }
    assert second["values"]["name"] == "pDemo2"
    assert output["metrics"] == {"records": 2}
    # The feature on another record is left out, and said so.
    assert output["messages"][0] == {
        "level": "warning",
        "text": "Record 1 (pDemo): a misc_feature on another record (J00194.1:100..202) "
        "was left out.",
    }
    assert result.stdout.splitlines()[-1] == "::progress 1.00 2 of 2"


def test_fasta_records_take_their_names_and_the_topology_given(tmp_path: Path) -> None:
    text = ">gb|L09137.2| pUC19 cloning vector\nACGTACGT\nacgtn\n\n>seq2\nGGCC\n"
    result, output = imported(tmp_path, text, name="vectors.fa", topology="circular")
    assert result.returncode == 0, result.stderr
    assert output is not None
    first, second = (c["values"] for c in output["creates"])
    assert first == {
        "name": "L09137.2",
        "description": "pUC19 cloning vector",
        "accession": "L09137.2",
        "bases": "ACGTACGTACGTN",
        "topology": "circular",
    }
    assert second == {"name": "seq2", "bases": "GGCC", "topology": "circular"}
    origin = output["creates"][1]["origin"]
    assert (origin["source"], origin["id"]) == ("FASTA file", "seq2")
    assert origin["sha256"] == hashlib.sha256(b">seq2\nGGCC\n").hexdigest()


NEITHER = "seqs.txt: It isn't FASTA (records start with '>') or GenBank (records start with LOCUS)."


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (">one\nACGU\n", "seqs.txt: Record 1 (one) has U at base 4: it's RNA, not DNA."),
        (
            ">one\nAC--GT\n",
            "seqs.txt: Record 1 (one) has a gap (-) at base 3: import unaligned sequences.",
        ),
        (
            ">a\nACGT\n>b\nACJT\n",
            "seqs.txt: Record 2 (b) has 'J' at base 3, which isn't an IUPAC base.",
        ),
        (">empty\n\n", "seqs.txt: Record 1 (empty) has no bases."),
        (
            "ACGT\n",
            NEITHER,
        ),
        (
            "",
            NEITHER,
        ),
        (
            GENBANK.replace("//\n", ""),
            "seqs.txt: Record 1 doesn't end with '//': is the file cut short?",
        ),
    ],
)
def test_a_record_that_cant_be_read_fails_the_import_saying_why(
    tmp_path: Path, text: str, why: str
) -> None:
    result, output = imported(tmp_path, text)
    assert result.returncode == 1
    assert output is None  # nothing is half imported
    assert result.stdout.splitlines()[-1] == f"::error {why}"


# --- Fetching from NCBI --------------------------------------------------------------------------

NCBI = SCRIPT.with_name("ncbi.py")


class FakeNcbi:
    """Answers efetch requests with GenBank records it holds, by accession."""

    def __init__(self, *failures: int) -> None:
        self.records = {
            "L09137.2": GENBANK,
            "NM_000546.6": GENBANK.replace("pDemo", "TP53")
            .replace("L09137", "NM_000546")
            .replace(".2\n", ".6\n"),
        }
        self.failures = list(failures)
        self.asked: list[dict[str, list[str]]] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        import io
        import urllib.error
        import urllib.parse

        # Nothing in the URL: a key would be in access logs.
        assert request.full_url == "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        assert timeout <= 120
        self.asked.append(urllib.parse.parse_qs(request.data.decode()))
        if self.failures:
            code = self.failures.pop(0)
            raise urllib.error.HTTPError(request.full_url, code, "Slow down", {}, io.BytesIO())  # type: ignore[arg-type]
        wanted = self.asked[-1]["id"][0].split(",")
        found = [
            text
            for accession, text in self.records.items()
            if accession in wanted or accession.split(".")[0] in wanted
        ]
        return io.BytesIO("\n".join(found).encode())


@pytest.fixture
def ncbi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """The fetch script, loaded here with NCBI faked."""
    import importlib
    import time
    import urllib.request

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    module = importlib.import_module("ncbi")
    fake = FakeNcbi()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setenv("IKI_INPUT", str(tmp_path / "input.json"))
    monkeypatch.setenv("IKI_OUTPUT", str(tmp_path / "out.json"))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    return SimpleNamespace(script=module, fake=fake, slept=slept)


def fetched(
    tmp_path: Path, ncbi: Any, accessions: str, email: str | None = "lab@example.com"
) -> dict[str, Any]:
    settings = {"ncbi_email": email} if email else {}
    run_input = {"params": {"accessions": accessions}, "settings": settings}
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    ncbi.script.main()
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output


def test_fetches_records_from_ncbi_and_says_which_version_and_when(
    tmp_path: Path, ncbi: Any
) -> None:
    output = fetched(tmp_path, ncbi, "nm_000546, L09137.2\nNM_000546")
    (asked,) = ncbi.fake.asked
    assert asked == {
        "db": ["nuccore"],
        "id": ["NM_000546,L09137.2"],
        "rettype": ["gbwithparts"],
        "retmode": ["text"],
        "tool": ["iki-dna"],
        "email": ["lab@example.com"],
    }
    tp53, demo = output["creates"]
    assert (tp53["values"]["name"], demo["values"]["name"]) == ("TP53", "pDemo")
    assert tp53["values"]["accession"] == "NM_000546.6"
    origin = tp53["origin"]
    assert {k: origin[k] for k in ("source", "id", "version", "url")} == {
        "source": "NCBI Nucleotide",
        "id": "NM_000546",
        "version": "NM_000546.6",
        "url": "https://www.ncbi.nlm.nih.gov/nuccore/NM_000546.6",
    }
    assert origin["retrieved_at"].endswith("+00:00")
    assert origin["sha256"] == hashlib.sha256(ncbi.fake.records["NM_000546.6"].encode()).hexdigest()
    assert output["metrics"] == {"records": 2}


def test_asks_ncbi_in_batches_no_faster_than_it_allows(
    tmp_path: Path, ncbi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ncbi.script, "BATCH", 1)
    fetched(tmp_path, ncbi, "L09137.2 NM_000546.6")
    assert [a["id"] for a in ncbi.fake.asked] == [["L09137.2"], ["NM_000546.6"]]
    assert ncbi.slept == [0.34]


def test_an_api_key_goes_in_the_body_and_lets_it_ask_faster(
    tmp_path: Path, ncbi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ncbi.script, "BATCH", 1)
    monkeypatch.setenv("NCBI_API_KEY", "k3y")
    fetched(tmp_path, ncbi, "L09137.2 NM_000546.6")
    assert [a["api_key"] for a in ncbi.fake.asked] == [["k3y"], ["k3y"]]
    assert ncbi.slept == [0.1]


def test_waits_and_asks_again_when_ncbi_says_to_slow_down(tmp_path: Path, ncbi: Any) -> None:
    ncbi.fake.failures = [429, 503]
    output = fetched(tmp_path, ncbi, "L09137")
    assert len(ncbi.fake.asked) == 3
    assert ncbi.slept == [2, 4]
    assert output["creates"][0]["origin"]["version"] == "L09137.2"


@pytest.mark.parametrize(
    ("accessions", "email", "error"),
    [
        ("L09137 X99999", "lab@example.com", "::error NCBI has no nucleotide record X99999."),
        (
            "L09137 not-one",
            "lab@example.com",
            "::error NOT-ONE isn't a nucleotide accession, such as NM_000546.6 or L09137.",
        ),
        (" , ", "lab@example.com", "::error Give at least one accession, such as NM_000546.6."),
        (
            "L09137",
            None,
            "::error The dna plugin's ncbi_email setting isn't set: NCBI asks automated "
            "clients for an address. Ask your Iki administrator to set it.",
        ),
    ],
)
def test_a_fetch_fails_whole_and_says_why(
    tmp_path: Path,
    ncbi: Any,
    capsys: pytest.CaptureFixture[str],
    accessions: str,
    email: str | None,
    error: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        fetched(tmp_path, ncbi, accessions, email)
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines()[-1] == error
    assert not (tmp_path / "out.json").exists()

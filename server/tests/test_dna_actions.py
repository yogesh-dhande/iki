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
        "files": {FILE_ID: {"name": name, "path": str(path), "size": path.stat().st_size}},
    }
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(IMPORTS)],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(out_dir),
        },
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
    assert first["type"] == "iki/dna.DNASequence"
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
                "parts": [
                    {"start": 50, "end": 60, "strand": "forward"},
                    {"start": 1, "end": 10, "strand": "forward"},
                ],
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
    assert result.stdout.splitlines()[-1] == "::progress 1.00 2 records"


def with_feature(location: str, *, length: int = 100, topology: str = "circular") -> str:
    """A GenBank record of `length` bases with one feature at `location`."""
    bases = ("acgt" * length)[:length]
    origin = "".join(
        f"{i + 1:>9} {' '.join(bases[j : j + 10] for j in range(i, min(i + 60, length), 10))}\n"
        for i in range(0, length, 60)
    )
    return (
        f"LOCUS       pLoc  {length} bp    DNA     {topology} SYN 11-NOV-2026\n"
        "FEATURES             Location/Qualifiers\n"
        f"     misc_feature    {location}\n"
        f"ORIGIN\n{origin}//\n"
    )


@pytest.mark.parametrize(
    ("location", "topology", "expected"),
    [
        # INSDC's feature table: a join covers its parts, not what lies between them.
        (
            "join(2..4,10..12)",
            "linear",
            {
                "start": 2,
                "end": 12,
                "strand": "forward",
                "parts": [
                    {"start": 2, "end": 4, "strand": "forward"},
                    {"start": 10, "end": 12, "strand": "forward"},
                ],
            },
        ),
        # The reverse strand's join across the origin: 16 bases, read 5 to 1 then 100 to 90.
        (
            "complement(join(90..100,1..5))",
            "circular",
            {
                "start": 90,
                "end": 5,
                "strand": "reverse",
                "parts": [
                    {"start": 1, "end": 5, "strand": "reverse"},
                    {"start": 90, "end": 100, "strand": "reverse"},
                ],
            },
        ),
        # A site between two bases covers neither.
        (
            "5^6",
            "linear",
            {
                "start": 5,
                "end": 6,
                "strand": "forward",
                "parts": [{"start": 5, "end": 6, "strand": "forward", "between": True}],
            },
        ),
        # Parts out of order on a circle don't make it cross the origin.
        (
            "join(10..12,2..4)",
            "circular",
            {
                "start": 2,
                "end": 12,
                "strand": "forward",
                "parts": [
                    {"start": 10, "end": 12, "strand": "forward"},
                    {"start": 2, "end": 4, "strand": "forward"},
                ],
            },
        ),
        # Parts on both strands, as trans-splicing has.
        (
            "join(1..5,complement(10..20))",
            "linear",
            {
                "start": 1,
                "end": 20,
                "strand": "mixed",
                "parts": [
                    {"start": 1, "end": 5, "strand": "forward"},
                    {"start": 10, "end": 20, "strand": "reverse"},
                ],
            },
        ),
        # A plain range, fuzzy or not, is its start and end alone.
        ("<1..>9", "linear", {"start": 1, "end": 9, "strand": "forward"}),
        ("complement(7)", "linear", {"start": 7, "end": 7, "strand": "reverse"}),
    ],
)
def test_a_features_parts_say_where_it_lies(
    tmp_path: Path, location: str, topology: str, expected: dict[str, Any]
) -> None:
    result, output = imported(tmp_path, with_feature(location, topology=topology))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (feature,) = output["creates"][0]["values"]["features"]
    feature.pop("qualifiers", None)
    assert feature == {"type": "misc_feature", **expected}


@pytest.mark.parametrize("location", ["5^7", "100^1", "1.5", "join(1..5,200..210)"])
def test_a_location_that_isnt_one_on_the_sequence_is_left_out_saying_so(
    tmp_path: Path, location: str
) -> None:
    # 5^7 isn't between neighbours; 100^1 is only on a circle; 1.5 (one base somewhere
    # in 1 to 5) is too vague to draw.
    result, output = imported(tmp_path, with_feature(location, topology="linear"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert "features" not in output["creates"][0]["values"]
    assert output["messages"][0]["text"] == (
        f"Record 1 (pLoc): a misc_feature at {location!r} was left out."
    )


@pytest.mark.parametrize(
    ("molecule", "strandedness"),
    [
        ("ss-DNA", "single"),
        ("ds-DNA", "double"),
        # GenBank writes an mRNA in the T alphabet: kept as its cDNA's bases, one strand.
        ("mRNA", "single"),
        ("RNA", "single"),
        ("ds-RNA", "double"),
        # Plain DNA, or mixed strands, doesn't say: the field's default stands.
        ("DNA", None),
        ("ms-DNA", None),
    ],
)
def test_locus_says_how_many_strands_the_molecule_has(
    tmp_path: Path, molecule: str, strandedness: str | None
) -> None:
    text = GENBANK.replace("60 bp    DNA     circular", f"60 bp    {molecule:<7} circular")
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert output["creates"][0]["values"].get("strandedness") == strandedness


#: A RefSeq model's COMMENT, as NCBI writes it (XM_090324667.1, cut down).
MODEL_COMMENT = """\
COMMENT     MODEL REFSEQ:  This record is predicted by automated computational
            analysis. This record is derived from a genomic sequence
            (NC_143544) annotated using gene prediction method: Gnomon.
            Also see:
                Documentation of NCBI's Annotation Process

            ##Genome-Annotation-Data-START##
            Annotation Provider         :: NCBI RefSeq
            Annotation Status           :: Full annotation
            Annotation Pipeline         :: NCBI Eukaryotic Genome Annotation
                                           Pipeline (EGAP)
            Annotation Software Version :: 10.6
            Annotation Method           :: Gnomon; cmsearch; tRNAscan-SE
            ##Genome-Annotation-Data-END##
"""
MODEL = GENBANK.replace("FEATURES ", MODEL_COMMENT + "FEATURES ")
#: What it says made it: NCBI's prediction.
PREDICTED = {
    "kind": "computational",
    "method": "gene prediction",
    "software": [
        {"name": "Gnomon"},
        {"name": "NCBI Eukaryotic Genome Annotation Pipeline (EGAP)", "version": "10.6"},
    ],
}


def test_a_refseq_models_record_says_ncbi_predicted_it(tmp_path: Path) -> None:
    result, output = imported(tmp_path, MODEL + "\n" + GENBANK)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    model, other = output["creates"]
    assert model["origin"]["made"] == PREDICTED
    # A record that doesn't say how its sequence was made gets nothing.
    assert "made" not in other["origin"]
    # A COMMENT that isn't a model's says nothing of how either.
    result, output = imported(
        tmp_path, GENBANK.replace("FEATURES ", "COMMENT     Sequenced by the lab.\nFEATURES ")
    )
    assert output is not None
    assert "made" not in output["creates"][0]["origin"]


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
    # RefSeq's accessions, NZ_ ones too (NCBI's RefSeq accession formats).
    for header, accession in [
        ("ref|NZ_CP009273.1|", "NZ_CP009273.1"),
        ("ref|NM_000546.6|", "NM_000546.6"),
        ("NZ_AAAA01000001.1", "NZ_AAAA01000001.1"),
    ]:
        _, output = imported(tmp_path, f">{header} a record\nACGT\n", name="vectors.fa")
        assert output is not None
        assert output["creates"][0]["values"]["accession"] == accession


NEITHER = "seqs.txt: It isn't FASTA (records start with '>') or GenBank (records start with LOCUS)."


def test_a_large_file_is_read_a_line_at_a_time_into_a_creates_file(tmp_path: Path) -> None:
    count = 2500
    text = "".join(f">seq{n} number {n}\n{BASES[: 10 + n % 40]}\n" for n in range(count))
    result, output = imported(tmp_path, text, name="many.fa")
    assert result.returncode == 0, result.stdout
    assert output is not None
    assert "creates" not in output
    assert output["creates_file"] == {"$file": "creates.ndjson"}
    assert output["metrics"] == {"records": count}
    lines = (tmp_path / "out" / "creates.ndjson").read_text().splitlines()
    assert len(lines) == count
    issues = [schemas.check("run-output", json.loads(line), part="create") for line in lines]
    assert not any(issues)
    last = json.loads(lines[-1])
    assert last["values"]["name"] == f"seq{count - 1}"
    assert last["values"]["description"] == f"number {count - 1}"
    assert last["origin"]["id"] == f"seq{count - 1}"
    # It says how far it's got as it goes, and the log stays short.
    assert "::progress 0.39 1,000 records" in result.stdout
    assert "… and 1,500 more records." in result.stdout


def test_a_few_records_go_in_output_json(tmp_path: Path) -> None:
    _, output = imported(tmp_path, ">a\nACGT\n>b\nGGCC\n")
    assert output is not None
    assert [c["values"]["name"] for c in output["creates"]] == ["a", "b"]
    assert not (tmp_path / "out" / "creates.ndjson").exists()


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
    assert "made" not in origin


def test_fetches_refseq_accessions_of_every_form(tmp_path: Path, ncbi: Any) -> None:
    # NCBI's RefSeq accession formats: NZ_ before an INSDC accession, versioned or not.
    ncbi.fake.records["NZ_CP009273.1"] = (
        GENBANK.replace("pDemo", "BW25113").replace("L09137", "NZ_CP009273").replace(".2\n", ".1\n")
    )
    output = fetched(tmp_path, ncbi, "NZ_CP009273.1 nz_cp009273")
    assert ncbi.fake.asked[0]["id"] == ["NZ_CP009273.1,NZ_CP009273"]
    assert output["creates"][0]["values"]["accession"] == "NZ_CP009273.1"


def test_a_fetched_refseq_model_says_ncbi_predicted_it(tmp_path: Path, ncbi: Any) -> None:
    ncbi.fake.records["L09137.2"] = MODEL
    output = fetched(tmp_path, ncbi, "L09137.2")
    assert output["creates"][0]["origin"]["made"] == PREDICTED


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

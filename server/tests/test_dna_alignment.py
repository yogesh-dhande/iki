"""The dna plugin's alignments of whole sequences: two, as EMBOSS needle and water align them,
and many, through MAFFT, each into an iki/core.Alignment of them.

The pairwise numbers are EMBOSS 6.6.0's own, from needle and water run on the same INSDC
records with their defaults (EDNAFULL, gap opening 10, extension 0.5):

    needle -asequence L09137.2.fa -bsequence J01749.1.fa -gapopen 10 -gapextend 0.5
    water  -asequence L09137.2.fa -bsequence J01749.1.fa -gapopen 10 -gapextend 0.5
    needle ... -sreverse1                # pUC19's reverse complement

MAFFT is in the plugin's image, not here: the multiple alignment's tests put a stand-in on
the PATH that replays what MAFFT 7.505 gave for the six vectors in
`data/plasmids/` (`six-vectors.mafft.fasta`, see that directory's README), or
changes bases, or fails, and records how it was called. The image's own check and the
dev cluster's end-to-end run the real one."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from Bio import SeqIO

from iki_spec import schemas

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "dna" / "scripts"
PLASMIDS = Path(__file__).parent / "data" / "plasmids"
COMPLEMENT = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")


def bases(accession: str) -> str:
    record = SeqIO.read(PLASMIDS / f"{accession}.gb", "genbank")  # type: ignore[no-untyped-call]
    return str(record.seq).upper()


def reverse_complement(sequence: str) -> str:
    return sequence.translate(COMPLEMENT)[::-1]


PUC19 = bases("L09137.2")
PBR322 = bases("J01749.1")
#: The six vectors, in the order MAFFT was given them.
VECTORS = ["L09137.2", "J01749.1", "U03450.1", "MN996867.1", "LT009457.1", "OL989372.1"]


def dna(i: int, sequence: str, name: str | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {"bases": sequence}
    if name:
        values["name"] = name
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"DNA-{i:06d}", "values": values}


def script(
    tmp_path: Path, name: str, run_input: dict[str, Any], env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "output.json"
    out.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-E", "-s", str(SCRIPTS / name)],
        env={
            "PATH": os.environ["PATH"],
            **(env or {}),
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if not out.exists():
        return result, None
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


def fasta_records(path: Path) -> list[tuple[str, str]]:
    records = path.read_text().split(">")[1:]
    return [(r.split("\n", 1)[0], "".join(r.split("\n")[1:])) for r in records]


# --- Two sequences ------------------------------------------------------------------------------

NEEDLE = {"mode": "global", "gap_open": 10, "gap_extend": 0.5, "strand": "forward"}


def pair(
    tmp_path: Path, query: str, target: str, **params: Any
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    return script(
        tmp_path,
        "align.py",
        {
            "run": {"hrid": "RUN-000007"},
            "params": {**NEEDLE, **params},
            "inputs": {"query": [dna(1, query, "pUC19")], "target": [dna(2, target, "pBR322")]},
        },
    )


def test_aligns_two_sequences_as_needle_and_keeps_the_alignment(tmp_path: Path) -> None:
    result, output = pair(tmp_path, PUC19, PBR322)

    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    # needle: Length 4806, Identity 2238/4806, Similarity 2238/4806, Gaps 2565/4806,
    # Score 10916.0.
    assert output["metrics"] == {
        "score": 10916.0,
        "length": 4806,
        "identity_percent": pytest.approx(100 * 2238 / 4806),
        "similarity_percent": pytest.approx(100 * 2238 / 4806),
        "gaps_percent": pytest.approx(100 * 2565 / 4806),
    }
    text = (tmp_path / "out" / "alignment.txt").read_text()
    assert "# Matrix: EDNAFULL" in text and "# Identity:   2238/4806 (46.6%)" in text
    assert "# Query_strand: as given" in text
    rows = fasta_records(tmp_path / "out" / "alignment.fasta")
    assert [h for h, _ in rows] == ["DNA-000001", "DNA-000002"]
    assert [r.replace("-", "") for _, r in rows] == [PUC19, PBR322]
    (created,) = output["creates"]
    assert created == {
        "type": "iki/core.Alignment",
        "values": {
            "name": "pUC19 and pBR322",
            "description": "46.6% identical and 46.6% similar over 4806 columns, scoring 10916.0.",
            "reference": dna(2, "")["id"],
            "members": [dna(1, "")["id"], dna(2, "")["id"]],
            "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"},
            "method": "Biopython PairwiseAligner, global as EMBOSS needle: EDNAFULL, gap opening "
            "10, extension 0.5",
        },
        "derived_from": [dna(1, "")["id"], dna(2, "")["id"]],
    }


def test_local_finds_the_part_they_share_as_water_does(tmp_path: Path) -> None:
    _, output = pair(tmp_path, PUC19, PBR322, mode="local")

    assert output is not None
    # water: Length 2727, Identity 2238/2727, Gaps 486/2727, Score 10916.0.
    assert output["metrics"] == {
        "score": 10916.0,
        "length": 2727,
        "identity_percent": pytest.approx(100 * 2238 / 2727),
        "similarity_percent": pytest.approx(100 * 2238 / 2727),
        "gaps_percent": pytest.approx(100 * 486 / 2727),
    }
    # pUC19's whole length lies along pBR322's 2,072-4,353: their shared origin and bla.
    rows = fasta_records(tmp_path / "out" / "alignment.fasta")
    assert [h for h, _ in rows] == ["DNA-000001", "DNA-000002/2072-4353"]
    assert rows[1][1].replace("-", "") == PBR322[2071:4353]


def test_the_query_aligns_as_its_reverse_complement_when_that_fits_better(
    tmp_path: Path,
) -> None:
    """pUC19 given as its other strand aligns to pBR322 just as well, both ways being
    tried; its row is headed HRID/end-start, as Rfam writes a reverse-strand row."""
    _, forward = pair(tmp_path, PUC19, PBR322)
    _, either = pair(tmp_path, reverse_complement(PUC19), PBR322, strand="either")

    assert forward is not None and either is not None
    assert either["metrics"] == forward["metrics"]
    rows = fasta_records(tmp_path / "out" / "alignment.fasta")
    assert rows[0][0] == f"DNA-000001/{len(PUC19)}-1"
    assert rows[0][1].replace("-", "") == PUC19
    values = either["creates"][0]["values"]
    assert values["method"].endswith(", the query's reverse complement")
    assert values["description"].endswith(", pUC19's reverse complement.")
    text = (tmp_path / "out" / "alignment.txt").read_text()
    assert "# Query_strand: reverse complement" in text
    # Its lines number down along the query, as needle numbers a reversed sequence.
    lines = [line.split() for line in text.splitlines() if line.startswith("DNA-000001 ")]
    first = next(words for words in lines if words[2].strip("-"))
    assert int(first[1]) == 2686
    assert int(first[3]) == 2686 - len(first[2].replace("-", "")) + 1
    assert int(lines[-1][3]) == 1


def test_reverse_aligns_the_reverse_complement_as_needle_sreverse1(tmp_path: Path) -> None:
    _, output = pair(tmp_path, PUC19, PBR322, strand="reverse")

    assert output is not None
    # needle -sreverse1: Score 2471.5. (Its 4,836 columns are one of several alignments
    # with that score; PairwiseAligner returns another.)
    assert output["metrics"]["score"] == 2471.5
    rows = fasta_records(tmp_path / "out" / "alignment.fasta")
    assert rows[0][0] == "DNA-000001/2686-1"
    assert rows[0][1].replace("-", "") == reverse_complement(PUC19)


def test_ambiguity_codes_score_by_the_bases_they_share(tmp_path: Path) -> None:
    """EDNAFULL scores R (A or G) against A as +1: similar, not identical."""
    _, output = pair(tmp_path, "ACGTRACGT", "ACGTAACGT")

    assert output is not None
    assert output["metrics"]["identity_percent"] == pytest.approx(100 * 8 / 9)
    assert output["metrics"]["similarity_percent"] == pytest.approx(100)
    assert output["metrics"]["score"] == 8 * 5 + 1
    text = (tmp_path / "out" / "alignment.txt").read_text()
    assert "||||:||||" in text


def test_a_local_alignment_with_nothing_above_zero_is_none(tmp_path: Path) -> None:
    result, output = pair(tmp_path, "AAAA", "CCCC", mode="local")

    assert result.returncode == 0
    assert output is not None
    assert output["creates"] == []
    assert output["metrics"]["length"] == 0
    assert "have no local alignment" in output["messages"][0]["text"]


@pytest.mark.parametrize(
    ("query", "target", "said"),
    [
        ("", "ACGT", "DNA-000001 has no bases to align."),
        ("ACGU", "ACGT", "DNA-000001 has 'U' at base 4, not an IUPAC base."),
        ("ACGT", "AC-T", "DNA-000002 has '-' at base 3, not an IUPAC base."),
        (
            "A" * 20_000,
            "A" * 10_001,
            "DNA-000001 and DNA-000002 are too long to align here: 20,000 by 10,001 bases is "
            "more than 200,000,000 cells.",
        ),
    ],
)
def test_what_cant_be_aligned_fails_its_run_saying_why(
    tmp_path: Path, query: str, target: str, said: str
) -> None:
    result, output = pair(tmp_path, query, target)

    assert output is None
    assert result.returncode == 1
    assert result.stdout.strip() == f"::error {said}"


# --- Many sequences, through MAFFT --------------------------------------------------------------

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
if mode == "killed":
    os.kill(os.getpid(), 9)
names, rows = [], []
for line in Path(sys.argv[-1]).read_text().splitlines():
    if line.startswith(">"):
        names.append(line[1:])
        rows.append("")
    else:
        rows[-1] += line
if os.environ.get("REPLAY"):
    sys.stdout.write(Path(os.environ["REPLAY"]).read_text())
    print("Strategy:\\n FFT-NS-i (Standard)\\n Iterative refinement method", file=sys.stderr)
    sys.exit(0)
width = max(len(r) for r in rows)
aligned = [r.lower().ljust(width, "-") for r in rows]
if mode == "changed":
    aligned[1] = "c" + aligned[1][1:]
if mode == "reversed":
    names[1] = "_R_" + names[1]
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


def multiple(
    tmp_path: Path,
    sequences: list[dict[str, Any]],
    reference: list[dict[str, Any]] | None = None,
    mode: str = "ok",
    replay: Path | None = None,
    **params: Any,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "mafft"
    fake.write_text(f"#!{sys.executable}\n{FAKE}")
    fake.chmod(0o755)
    return script(
        tmp_path,
        "multiple.py",
        {
            "run": {"hrid": "RUN-000009"},
            "params": {"strategy": "auto", **params},
            "inputs": {"sequences": sequences, "reference": reference or []},
        },
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CALLED": str(tmp_path / "called"),
            "MODE": mode,
            "REPLAY": str(replay or ""),
        },
    )


def called(tmp_path: Path) -> list[str]:
    found: list[str] = json.loads((tmp_path / "called").read_text())
    return found


def on_threads(tmp_path: Path) -> str:
    """What the method says of the threads MAFFT was given, as it was called."""
    args = called(tmp_path)
    n = int(args[args.index("--thread") + 1])
    return f"{n} thread" + ("" if n == 1 else "s")


def test_aligns_six_vectors_turning_two_round_as_mafft_did(tmp_path: Path) -> None:
    """MAFFT 7.505's own alignment of six vectors (--auto --nuc --adjustdirection, one
    thread): it chose FFT-NS-i, and took the reverse complement of pLenti6-V5-Puro and
    the pET-28a derivative, whose records run the other way round."""
    given = [dna(i, bases(acc), acc) for i, acc in enumerate(VECTORS, start=1)]

    result, output = multiple(tmp_path, given, replay=PLASMIDS / "six-vectors.mafft.fasta")

    assert output is not None, result.stdout
    assert called(tmp_path)[:3] == ["--auto", "--nuc", "--adjustdirection"]
    rows = fasta_records(tmp_path / "out" / "alignment.fasta")
    assert [h for h, _ in rows] == [
        "DNA-000001",
        "DNA-000002",
        "DNA-000003",
        "DNA-000004",
        "DNA-000005/8835-1",
        "DNA-000006/6835-1",
    ]
    assert {len(r) for _, r in rows} == {10165}
    for (_, row), entity, turned in zip(rows, given, [False] * 4 + [True] * 2, strict=True):
        own = entity["values"]["bases"]
        assert row.replace("-", "") == (reverse_complement(own) if turned else own)
    # Counted from MAFFT's own file: the columns identical in all six, and with a gap.
    replayed = [r.upper() for _, r in fasta_records(PLASMIDS / "six-vectors.mafft.fasta")]
    columns = list(zip(*replayed, strict=True))
    identical = sum(1 for c in columns if "-" not in c and len(set(c)) == 1)
    gapped = sum(1 for c in columns if "-" in c)
    assert output["metrics"] == {
        "rows": 6,
        "length": 10165,
        "identity_percent": round(100 * identical / 10165, 1),
        "gaps_percent": round(100 * gapped / 10165, 1),
        "reversed": 2,
    }
    assert (output["metrics"]["identity_percent"], output["metrics"]["gaps_percent"]) == (
        14.0,
        78.9,
    )
    values = output["creates"][0]["values"]
    assert values["method"] == (
        "MAFFT v7.505 (2022/Apr/10), FFT-NS-i (chosen by --auto), --adjustdirection, "
        + on_threads(tmp_path)
    )
    assert values["description"].endswith(
        "2 are aligned as their reverse complement: DNA-000005, DNA-000006."
    )
    assert values["aligned"] == {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"}
    assert values["name"] == "L09137.2, J01749.1 and 4 more"
    assert "MAFFT took the reverse complement of DNA-000005, DNA-000006." in result.stdout


def test_the_reference_is_the_first_row_once_and_names_the_alignment(tmp_path: Path) -> None:
    designed = dna(1, "ACGTACGTAC", "pUC19-GFP")
    given = [dna(2, "ACGTTCGTAC"), designed, dna(3, "ACGTACGTAA")]

    _, output = multiple(tmp_path, given, reference=[designed])

    assert output is not None
    values = output["creates"][0]["values"]
    assert values["reference"] == designed["id"]
    assert values["members"] == [designed["id"], given[0]["id"], given[2]["id"]]
    assert values["name"] == "3 sequences aligned to pUC19-GFP"
    headers = [h for h, _ in fasta_records(tmp_path / "out" / "alignment.fasta")]
    assert headers == ["DNA-000001", "DNA-000002", "DNA-000003"]


def test_strategies_and_directions_are_mafft_options(tmp_path: Path) -> None:
    given = [dna(i, "ACGTACGTAC") for i in range(1, 4)]

    _, output = multiple(
        tmp_path, given, strategy="G-INS-i", adjust_direction="accurate", name="  Clones  "
    )
    assert output is not None
    assert called(tmp_path)[:5] == [
        "--globalpair",
        "--maxiterate",
        "1000",
        "--nuc",
        "--adjustdirectionaccurately",
    ]
    assert output["creates"][0]["values"]["name"] == "Clones"
    assert output["creates"][0]["values"]["method"] == (
        "MAFFT v7.505 (2022/Apr/10), L-INS-i, --adjustdirectionaccurately, " + on_threads(tmp_path)
    )

    _, output = multiple(tmp_path, given, adjust_direction="off")
    assert output is not None
    assert "--adjustdirection" not in called(tmp_path)
    assert not any(c.startswith("--adjust") for c in called(tmp_path))
    assert output["creates"][0]["values"]["method"] == (
        "MAFFT v7.505 (2022/Apr/10), L-INS-i (chosen by --auto), " + on_threads(tmp_path)
    )


@pytest.mark.parametrize(
    ("sequences", "mode", "params", "said"),
    [
        ([dna(1, "ACGT")], "ok", {}, "Choose at least two sequences to align."),
        ([dna(1, "ACGT"), dna(2, "")], "ok", {}, "DNA-000002 has no bases to align."),
        (
            [dna(1, "ACGT"), dna(2, "AC-T")],
            "ok",
            {},
            "DNA-000002 has '-' at base 3, not an IUPAC base.",
        ),
        (
            [dna(1, "ACGT"), dna(2, "A" * 200_001)],
            "ok",
            {},
            "DNA-000002 is too long to align here: 200,001 bases is more than 200,000.",
        ),
        (
            [dna(i, "ACGT") for i in range(1, 202)],
            "ok",
            {"strategy": "L-INS-i"},
            "201 sequences are too many for this strategy, which MAFFT means for up to 200:"
            " choose Auto or FFT-NS-2.",
        ),
        (
            [dna(1, "ACGT"), dna(2, "ACGA")],
            "broken",
            {},
            "MAFFT failed (1): ERROR: Too few sequences",
        ),
        (
            [dna(1, "ACGT"), dna(2, "ACGA")],
            "killed",
            {},
            "MAFFT was stopped (signal 9), as it is when it runs out of memory: choose Auto or"
            " FFT-NS-2, or run it with more memory.",
        ),
        (
            [dna(1, "ACGT"), dna(2, "ACGA")],
            "changed",
            {},
            "MAFFT changed DNA-000002's bases, not only its gaps.",
        ),
        (
            # Named reversed, but its bases are as given: not its reverse complement.
            [dna(1, "ACGT"), dna(2, "AACC")],
            "reversed",
            {},
            "MAFFT changed DNA-000002's bases, not only its gaps.",
        ),
        (
            [dna(1, "ACGT"), dna(2, "AACC")],
            "reversed",
            {"adjust_direction": "off"},
            "MAFFT reversed a sequence it wasn't asked to.",
        ),
        (
            [dna(1, "ACGT"), dna(2, "ACGA")],
            "reordered",
            {},
            "MAFFT's alignment doesn't have the sequences given, in order.",
        ),
    ],
)
def test_what_mafft_cant_align_fails_its_run_saying_why(
    tmp_path: Path, sequences: list[dict[str, Any]], mode: str, params: dict[str, Any], said: str
) -> None:
    result, output = multiple(tmp_path, sequences, mode=mode, **params)

    assert output is None
    assert result.returncode == 1
    assert result.stdout.strip() == f"::error {said}"

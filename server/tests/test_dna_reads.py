"""The dna plugin's alignment of reads to a reference: trace, FASTQ and FASTA files, read
by their first bytes, trimmed, aligned on the better strand and round a circular
reference's origin, into a core.Alignment, with where they cover it and every
difference from it."""

import importlib.util
import json
import random
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "dna" / "scripts" / "reads.py"


def made_up(length: int, seed: int) -> str:
    """Bases at random, the same for the same seed."""
    rng = random.Random(seed)  # noqa: S311 - made-up test data, not secrets
    return "".join(rng.choice("ACGT") for _ in range(length))


#: A made-up plasmid of 600 bases, the same each time.
PLASMID = made_up(600, 7)
COMPLEMENT = str.maketrans("ACGT", "TGCA")


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def abif(bases: str, quality: list[int], sample: str = "sample") -> bytes:
    """A minimal ABIF (AB1) file, as a sequencer writes: the called bases (PBAS 2), their
    qualities (PCON 2) and the sample's name (SMPL 1)."""
    entries = [
        (b"PBAS", 2, 2, bases.encode()),
        (b"PCON", 2, 2, bytes(quality)),
        (b"SMPL", 1, 18, bytes([len(sample)]) + sample.encode()),
    ]
    data, directory, start = b"", b"", 128
    for name, number, kind, payload in entries:
        size = len(payload)
        if size <= 4:
            directory += struct.pack(
                ">4sihhii4si", name, number, kind, 1, size, size, payload.ljust(4, b"\0"), 0
            )
        else:
            directory += struct.pack(
                ">4sihhiiii", name, number, kind, 1, size, size, start + len(data), 0
            )
            data += payload
    header = struct.pack(
        ">4sh4sihhiiii",
        b"ABIF",
        101,
        b"tdir",
        1,
        1023,
        28,
        len(entries),
        len(directory),
        start + len(data),
        0,
    )
    return header.ljust(start, b"\0") + data + directory


def fastq(*reads: tuple[str, str, int]) -> bytes:
    """Records of (name, bases, quality for every base)."""
    return "".join(
        f"@{name}\n{bases}\n+\n{chr(33 + quality) * len(bases)}\n" for name, bases, quality in reads
    ).encode()


def script() -> Any:
    """The script itself, to call its functions."""
    spec = importlib.util.spec_from_file_location("dna_reads", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return module


def run(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    topology: str = "linear",
    bases: str = PLASMID,
    **params: Any,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    described = {}
    for n, (name, content) in enumerate(files.items(), start=1):
        path = tmp_path / name
        path.write_bytes(content)
        described[f"f{n}"] = {"name": name, "path": str(path)}
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    output = tmp_path / "output.json"
    output.unlink(missing_ok=True)
    reference = {
        "id": "00000000-0000-0000-0000-000000000001",
        "hrid": "DNA-000001",
        "values": {"name": "pTEST", "bases": bases, "topology": topology},
    }
    (tmp_path / "input.json").write_text(
        json.dumps(
            {
                "run": {"hrid": "RUN-000001"},
                "inputs": {"reference": [reference]},
                "params": {"reads": list(described), "trim": True, **params},
                "files": described,
            }
        )
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(SCRIPT)],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(output),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if not output.exists():
        return result, None
    found: dict[str, Any] = json.loads(output.read_text())
    assert not list(schemas.check("run-output", found))
    return result, found


def rows(tmp_path: Path) -> list[tuple[str, str]]:
    """The alignment's records: (header, row)."""
    out: list[tuple[str, str]] = []
    for line in (tmp_path / "out" / "alignment.fasta").read_text().splitlines():
        if line.startswith(">"):
            out.append((line[1:], ""))
        else:
            out[-1] = (out[-1][0], out[-1][1] + line)
    return out


def texts(output: dict[str, Any]) -> list[str]:
    return [m["text"] for m in output["messages"]]


def test_a_trace_is_trimmed_aligned_and_its_difference_named(tmp_path: Path) -> None:
    # Bases 101-300, with C/T at 151 if the plasmid has another base there, between ends
    # too poor to keep.
    read = list(PLASMID[100:300])
    swapped = "T" if read[50] != "T" else "C"
    read[50] = swapped
    called = "ACGTTGCA" + "".join(read) + "GGGGCCCC"
    quality = [5] * 8 + [40] * 200 + [5] * 8

    result, output = run(tmp_path, {"clone1_M13F.ab1": abif(called, quality)})

    assert output is not None, result.stdout
    (created,) = output["creates"]
    assert created["type"] == "core.Alignment"
    values = created["values"]
    assert values["reference"] == values["members"][0] == created["derived_from"][0]
    assert values["name"] == "1 read on pTEST"
    assert values["aligned"] == {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"}
    assert values["method"] == (
        "Biopython PairwiseAligner, local, on both strands: match 2, mismatch -3, gap opening "
        "5, extension 2; reads trimmed by Mott's algorithm, limit 0.05"
    )
    (ref_header, ref_row), (read_header, read_row) = rows(tmp_path)
    assert (ref_header, ref_row) == ("DNA-000001 pTEST", PLASMID)
    assert read_header == (
        "clone1_M13F forward, bases 1–200 of 200 (trimmed from 216), from clone1_M13F.ab1"
    )
    assert read_row == "-" * 100 + "".join(read) + "-" * 300
    assert texts(output)[:2] == [
        "The reads cover 101–300 of pTEST's 600 bases (33.3%). Not covered: 1–100, 301–600.",
        f"Differences from DNA-000001: {PLASMID[150]}151{swapped} (clone1_M13F Q40).",
    ]
    assert output["metrics"] == {
        "reads": 1,
        "aligned": 1,
        "coverage_percent": 33.3,
        "differences": 1,
    }


def lone(near: int, base: str | None = None) -> int:
    """A 0-based place from `near` on whose base differs from both neighbours (and from
    `base`), so a gap there can't move."""
    spot = near
    while PLASMID[spot] in (PLASMID[spot - 1], PLASMID[spot + 1]) or base in (
        PLASMID[spot - 1],
        PLASMID[spot],
    ):
        spot += 1
    return spot


def test_reads_on_the_other_strand_with_gaps_are_named_in_the_references_numbering(
    tmp_path: Path,
) -> None:
    # Bases 201-400 without one base, and with a T inserted later on.
    gone, added = lone(250), lone(300, "T")
    forward = PLASMID[200:gone] + PLASMID[gone + 1 : added] + "T" + PLASMID[added:400]
    seen = fastq(("rev1", reverse_complement(forward), 40))

    _, output = run(tmp_path, {"reads.fastq": seen})

    assert output is not None
    (_, ref_row), (header, row) = rows(tmp_path)
    assert header.startswith(f"rev1 reverse complement, bases 1–{len(forward)} of {len(forward)}")
    # The inserted T gets a column of its own, a gap in the reference's row.
    assert ref_row == PLASMID[:added] + "-" + PLASMID[added:]
    assert row == (
        "-" * 200
        + PLASMID[200:gone]
        + "-"
        + PLASMID[gone + 1 : added]
        + "T"
        + PLASMID[added:400]
        + "-" * 200
    )
    differences = texts(output)[1]
    assert f"{gone + 1}del{PLASMID[gone]} (rev1)" in differences
    assert f"{added}_{added + 1}insT (rev1 Q40)" in differences


@pytest.mark.parametrize(
    ("reference", "at", "bases", "named"),
    [
        # An A inserted anywhere in AAA is a duplication of its last.
        ("CGAAATC", 2, "A", "5dupA"),
        ("CGAAATC", 5, "A", "5dupA"),
        # Two bases repeating the two before them.
        ("CGTATAC", 6, "TA", "5_6dup"),
        # Inserted between two bases unlike it.
        ("CGAAATC", 1, "T", "1_2insT"),
    ],
)
def test_insertions_are_named_as_hgvs_names_them(
    reference: str, at: int, bases: str, named: str
) -> None:
    reads = script()
    assert reads.insertion(reference, at, bases) == named


def test_deletions_move_3_prime_in_repeated_bases() -> None:
    reads = script()
    assert reads.deletion("CGAAATC", 2, 2) == "5delA"
    # TA from TATA: the last two, 5 and 6.
    assert reads.deletion("CGTATAC", 2, 3) == "5_6del"
    assert reads.deletion("CGAAATC", 1, 1) == "2delG"


def test_a_read_across_a_circular_references_origin_aligns_in_one_piece(tmp_path: Path) -> None:
    across = PLASMID[-100:] + PLASMID[:100]

    _, output = run(tmp_path, {"origin.fasta": f">ori\n{across}\n".encode()}, topology="circular")

    assert output is not None
    (_, _), (_, row) = rows(tmp_path)
    assert row == PLASMID[:100] + "-" * 400 + PLASMID[-100:]
    assert texts(output)[:2] == [
        "The reads cover 1–100, 501–600 of pTEST's 600 bases (33.3%). Not covered: 101–500.",
        "No differences from DNA-000001 where the reads cover it.",
    ]
    assert ", round the origin" in output["creates"][0]["values"]["method"]


def test_reads_that_dont_align_or_have_nothing_good_are_left_out_saying_so(tmp_path: Path) -> None:
    stranger = made_up(200, 99)
    files = {
        "good.fastq": fastq(("good", PLASMID[300:500], 40)),
        "stranger.fastq": fastq(("stranger", stranger, 40)),
        "poor.fastq": fastq(("poor", PLASMID[0:200], 3)),
    }

    _, output = run(tmp_path, files)

    assert output is not None
    warnings = [m["text"] for m in output["messages"] if m["level"] == "warning"]
    assert warnings[0].startswith("stranger doesn't align to DNA-000001: ")
    assert warnings[1] == "poor has no bases good enough to keep: it's left out."
    assert [h.split()[0] for h, _ in rows(tmp_path)] == ["DNA-000001", "good"]
    assert output["metrics"]["reads"] == 3
    assert output["metrics"]["aligned"] == 1
    assert output["creates"][0]["values"]["name"] == "1 read on pTEST"


def test_untrimmed_reads_keep_their_ends(tmp_path: Path) -> None:
    read = PLASMID[100:300]

    _, output = run(tmp_path, {"r.fastq": fastq(("r", read, 5))}, trim=False, name=" Clone 7 ")

    assert output is not None
    assert output["creates"][0]["values"]["name"] == "Clone 7"
    assert "trimmed" not in output["creates"][0]["values"]["method"]
    assert rows(tmp_path)[1][0].startswith("r forward, bases 1–200 of 200, from r.fastq")


def test_names_are_made_unique(tmp_path: Path) -> None:
    files = {"a.fastq": fastq(("r", PLASMID[0:100], 40), ("r", PLASMID[100:200], 40))}

    _, output = run(tmp_path, files)

    assert output is not None
    assert [h.split()[0] for h, _ in rows(tmp_path)] == ["DNA-000001", "r", "r-2"]


@pytest.mark.parametrize(
    ("files", "said"),
    [
        ({"notes.txt": b"hello"}, "notes.txt isn't an AB1 trace, a FASTQ file or a FASTA file."),
        (
            {"x.fasta": b">x\nACGTXX\n"},
            "x in x.fasta has 'X', which isn't a base.",
        ),
        ({"empty.fasta": b">"}, "These files hold no reads."),
        (
            {"far.fasta": b">far\n" + b"ACGT" * 50 + b"\n"},
            "None of the reads align to DNA-000001.",
        ),
        (
            {"many.fasta": "".join(f">r{i}\nACGT\n" for i in range(1001)).encode()},
            "These files hold more than 1,000 reads: align them in parts.",
        ),
    ],
)
def test_what_cant_be_aligned_fails_its_run_saying_why(
    tmp_path: Path, files: dict[str, bytes], said: str
) -> None:
    result, output = run(tmp_path, files, bases="T" * 600 if "far" in str(files) else PLASMID)

    assert output is None
    assert result.returncode == 1, result.stderr
    assert result.stdout.strip().splitlines()[-1] == f"::error {said}"

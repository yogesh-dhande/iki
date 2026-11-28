"""The protein plugin's alignment of reads to a protein: reads translated in six frames,
the best kept, frameshifts found in the rest, into an iki/core.Alignment, with where they cover
the protein and every difference from it, named as HGVS names a protein's."""

import importlib.util
import inspect
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"
SCRIPT = PLUGINS / "protein" / "scripts" / "reads.py"
DNA_SCRIPT = PLUGINS / "dna" / "scripts" / "reads.py"

#: One codon for each residue, and a stop.
CODON = {
    "A": "GCT",
    "R": "CGT",
    "N": "AAC",
    "D": "GAT",
    "C": "TGC",
    "Q": "CAG",
    "E": "GAA",
    "G": "GGT",
    "H": "CAC",
    "I": "ATC",
    "L": "CTG",
    "K": "AAA",
    "M": "ATG",
    "F": "TTC",
    "P": "CCG",
    "S": "TCT",
    "T": "ACC",
    "W": "TGG",
    "Y": "TAC",
    "V": "GTT",
    "*": "TAA",
}
COMPLEMENT = str.maketrans("ACGT", "TGCA")


def made_up(length: int, seed: int, letters: str) -> str:
    """Letters at random, the same for the same seed."""
    rng = random.Random(seed)  # noqa: S311 - made-up test data, not secrets
    return "".join(rng.choice(letters) for _ in range(length))


#: A made-up protein of 250 residues, the same each time, and bases that code for it.
PROTEIN = "M" + made_up(249, 3, "ACDEFGHIKLMNPQRSTVWY")
#: Bases of a vector, before the protein's code.
VECTOR = made_up(120, 4, "ACGT")


def coding(protein: str) -> str:
    return "".join(CODON[a] for a in protein)


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def fastq(*reads: tuple[str, str, int]) -> bytes:
    """Records of (name, bases, quality for every base)."""
    return "".join(
        f"@{name}\n{bases}\n+\n{chr(33 + quality) * len(bases)}\n" for name, bases, quality in reads
    ).encode()


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return module


def run(
    tmp_path: Path, files: dict[str, bytes], *, sequence: str = PROTEIN, **params: Any
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
        "hrid": "PRT-000001",
        "values": {"name": "Kinase", "sequence": sequence},
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


def other(residue: str) -> str:
    return "W" if residue != "W" else "Y"


def test_a_read_through_vector_into_the_code_is_aligned_and_its_difference_named(
    tmp_path: Path,
) -> None:
    # Residues 1-200 after 120 bases of vector, residue 100 changed, poor bases at the end.
    changed = list(PROTEIN[:200])
    changed[99] = other(PROTEIN[99])
    bases = VECTOR + coding("".join(changed)) + "ACGTACGT"
    quality = [40] * (len(bases) - 8) + [4] * 8
    record = f"@clone_F1\n{bases}\n+\n{''.join(chr(33 + q) for q in quality)}\n"

    result, output = run(tmp_path, {"clone.fastq": record.encode()})

    assert output is not None, result.stdout
    (created,) = output["creates"]
    values = created["values"]
    assert values["reference"] == values["members"][0] == created["derived_from"][0]
    assert values["aligned"] == {"$file": "alignment.fasta", "media_type": "chemical/seq-aa-fasta"}
    assert values["method"] == (
        "Biopython PairwiseAligner, local, in six frames as tblastn: BLOSUM62, gap opening 11, "
        "extension 1, expected value under 0.001; reads trimmed by Mott's algorithm, limit 0.05"
    )
    (ref_header, ref_row), (read_header, read_row) = rows(tmp_path)
    assert (ref_header, ref_row) == ("PRT-000001 Kinase", PROTEIN)
    assert read_header == (
        "clone_F1 forward frame 1, bases 121–720 of 720 (trimmed from 728), from clone.fastq; "
        "on PRT-000001 1–200"
    )
    assert read_row == "".join(changed) + "." * 50
    # For other tools, `-` where the read has no observation, as aligned FASTA has it: a
    # `.` they'd take for a residue.
    standard = (tmp_path / "out" / "alignment-standard.fasta").read_text().splitlines()
    assert standard[0] == ">PRT-000001 Kinase"
    assert "." not in "".join(line for line in standard if not line.startswith(">"))
    assert "".join(line for line in standard[standard.index(">" + read_header) + 1 :]) == (
        "".join(changed) + "-" * 50
    )
    assert texts(output)[:2] == [
        "The reads cover 1–200 of Kinase's 250 residues (80.0%). Not covered: 201–250.",
        f"Differences from PRT-000001: p.({PROTEIN[99]}100{changed[99]}) (clone_F1 Q40).",
    ]
    assert output["metrics"] == {
        "reads": 1,
        "aligned": 1,
        "coverage_percent": 80.0,
        "differences": 1,
    }


def test_every_stop_at_the_proteins_end_is_left_out(tmp_path: Path) -> None:
    """A protein ending ** is the protein without them, as its identity is (ADR 0138)."""
    code = coding(PROTEIN[:200])

    _, output = run(tmp_path, {"r.fasta": f">r\n{code}\n".encode()}, sequence=PROTEIN + "**")

    assert output is not None
    assert rows(tmp_path)[0][1] == PROTEIN
    assert texts(output)[1] == "No differences from PRT-000001 where the reads cover it."


def test_a_frameshift_on_the_other_strand_is_found_and_named(tmp_path: Path) -> None:
    # Residues 101-250, a base lost from residue 151's codon, read on the other strand.
    bases = coding(PROTEIN)[300:]
    lost = 151 * 3 - 300 - 2
    shifted = bases[:lost] + bases[lost + 1 :]

    _, output = run(tmp_path, {"r.fasta": f">r\n{reverse_complement(shifted)}\n".encode()})

    assert output is not None
    (_, _), (header, row) = rows(tmp_path)
    assert header.startswith("r reverse complement frame 1, bases 1–150, frame 3, bases ")
    assert row[:100] == "." * 100
    assert row[100:150] == PROTEIN[100:150]
    assert row[151:] == PROTEIN[151:]
    assert f"p.({PROTEIN[150]}151fs) (r)" in texts(output)[1]


def test_stops_and_in_frame_gaps_are_named(tmp_path: Path) -> None:
    # A stop at residue 30, residue 60 lost and a residue added after 90.
    code = (
        coding(PROTEIN[:29])
        + CODON["*"]
        + coding(PROTEIN[30:59])
        + coding(PROTEIN[60:90])
        + CODON[other(PROTEIN[90])]
        + coding(PROTEIN[90:200])
    )

    _, output = run(tmp_path, {"r.fasta": f">r\n{code}\n".encode()})

    assert output is not None
    # The made-up protein has G30, Q60 and E90-C91, none repeated.
    assert (PROTEIN[29], PROTEIN[59], PROTEIN[89:91]) == ("G", "Q", "EC")
    assert texts(output)[1] == (
        "Differences from PRT-000001: p.(G30*) (r); p.(Q60del) (r); p.(E90_C91insW) (r)."
    )


def test_consecutive_changes_in_one_read_are_one_deletion_insertion(tmp_path: Path) -> None:
    """HGVS (recommendations, protein, deletion-insertion): changes to two or more
    consecutive residues on one allele are one delins, as p.(Arg21_Cys22delinsSerTrp) is,
    and a change predicted from DNA is in parentheses. Changes with a residue between them
    are two."""
    together, apart = list(PROTEIN[:200]), list(PROTEIN[:200])
    for i in (20, 21):
        together[i] = other(PROTEIN[i])
    for i in (20, 22):
        apart[i] = other(PROTEIN[i])
    # And a residue changed beside one lost: one delins, naming only what's left.
    beside = PROTEIN[:63] + other(PROTEIN[63]) + PROTEIN[65:200]

    _, output = run(tmp_path, {"r.fasta": f">r\n{coding(''.join(together))}\n".encode()})
    assert output is not None
    p, t = PROTEIN, together
    named = f"p.({p[20]}21_{p[21]}22delins{t[20]}{t[21]})"
    assert texts(output)[1] == f"Differences from PRT-000001: {named} (r)."
    assert output["metrics"]["differences"] == 1

    _, output = run(tmp_path, {"r.fasta": f">r\n{coding(''.join(apart))}\n".encode()})
    assert output is not None
    a = apart
    assert texts(output)[1] == (
        f"Differences from PRT-000001: p.({p[20]}21{a[20]}) (r); p.({p[22]}23{a[22]}) (r)."
    )
    assert output["metrics"]["differences"] == 2

    _, output = run(tmp_path, {"r.fasta": f">r\n{coding(beside)}\n".encode()})
    assert output is not None
    assert texts(output)[1] == (
        f"Differences from PRT-000001: p.({p[63]}64_{p[64]}65delins{other(p[63])}) (r)."
    )


@pytest.mark.parametrize(
    ("protein", "at", "residues", "named"),
    [
        ("MKLAG", 2, "W", "K2_L3insW"),
        # A K before K2 is a duplication of it.
        ("MKLAG", 1, "K", "K2dup"),
        ("MKLAG", 3, "KL", "K2_L3dup"),
        # In KKK, an added K is named at the last.
        ("MKKKA", 1, "K", "K4dup"),
    ],
)
def test_insertions_are_named_as_hgvs_names_them(
    protein: str, at: int, residues: str, named: str
) -> None:
    assert load(SCRIPT, "protein_reads").insertion(protein, at, residues) == named


def test_deletions_move_c_terminal_in_repeated_residues() -> None:
    reads = load(SCRIPT, "protein_reads")
    assert reads.deletion("MKKKA", 1, 1) == "K4del"
    assert reads.deletion("MKLAG", 1, 2) == "K2_L3del"
    # KL from KLKL: the last two.
    assert reads.deletion("MKLKLA", 1, 2) == "K4_L5del"


def test_ambiguous_and_unusual_residues_differ_only_from_what_they_cant_be(
    tmp_path: Path,
) -> None:
    # The protein has X at 40, B at 41, U (selenocysteine, coded by TGA) at 42 and O at 43;
    # the read codes A, D, a stop and K there, and E where the protein has Z at 44.
    protein = PROTEIN[:39] + "XBUOZ" + PROTEIN[44:]
    code = coding(PROTEIN[:39]) + "GCTGATTGAAAAGAA" + coding(PROTEIN[44:200])

    _, output = run(tmp_path, {"r.fasta": f">r\n{code}\n".encode()}, sequence=protein)

    assert output is not None
    assert texts(output)[1] == "Differences from PRT-000001: p.(O43K) (r)."


@pytest.mark.parametrize(
    ("site", "codon", "named"),
    [
        # UGA is selenocysteine's codon; UAA and UAG are stops in the standard code.
        ("U", "TGA", None),
        ("U", "TAA", "U42*"),
        ("U", "TAG", "U42*"),
        # UAG is pyrrolysine's; UGA is a stop.
        ("O", "TAG", None),
        ("O", "TGA", "O42*"),
    ],
)
def test_a_stop_codon_is_selenocysteine_or_pyrrolysine_only_where_it_recodes_one(
    tmp_path: Path, site: str, codon: str, named: str | None
) -> None:
    protein = PROTEIN[:41] + site + PROTEIN[42:]
    code = coding(PROTEIN[:41]) + codon + coding(PROTEIN[42:200])

    _, output = run(tmp_path, {"r.fasta": f">r\n{code}\n".encode()}, sequence=protein)

    assert output is not None
    said = (
        f"Differences from PRT-000001: p.({named}) (r)."
        if named
        else ("No differences from PRT-000001 where the reads cover it.")
    )
    assert texts(output)[1] == said
    # The read's row says what it codes for there: the recoded residue, or a stop.
    assert rows(tmp_path)[1][1][41] == (site if named is None else "*")


def test_the_consensus_of_reads_with_aan_counts_each_as_k_or_n(tmp_path: Path) -> None:
    """Two reads with AAN at residue 42 (K or N, by NCBI's standard code) and one with
    AAA (K): the mapper writes each AAN as X and says what it could be, and the
    consensus of those rows, run on the alignment the mapper wrote, has K there."""
    protein = PROTEIN[:41] + "A" + PROTEIN[42:]
    reads = [
        coding(PROTEIN[:41]) + "AAN" + coding(PROTEIN[42:200]),
        coding(PROTEIN[:41]) + "AAN" + coding(PROTEIN[42:200]),
        coding(PROTEIN[:41]) + "AAA" + coding(PROTEIN[42:200]),
    ]
    fasta = "".join(f">r{i}\n{bases}\n" for i, bases in enumerate(reads, start=1))
    _, output = run(tmp_path, {"r.fasta": fasta.encode()}, sequence=protein)
    assert output is not None
    aligned = rows(tmp_path)
    assert [h.rsplit(" ", 1)[-1] for h, _ in aligned[1:3]] == ["possible=42:KN"] * 2
    reference = {
        "id": "00000000-0000-0000-0000-000000000001",
        "hrid": "PRT-000001",
        "type": "iki/protein.Protein",
        "schema_version": 1,
        "revision": 1,
        "values": {},
    }
    alignment = {
        "id": "00000000-0000-0000-0000-000000000002",
        "hrid": "ALN-000001",
        "type": "iki/protein.ProteinAlignment",
        "schema_version": 1,
        "revision": 1,
        "values": {"name": "Clone 4 reads", "aligned": "f1"},
        "included": {"reference": [reference]},
    }
    (tmp_path / "consensus.json").write_text(
        json.dumps(
            {
                "run": {"hrid": "RUN-000002"},
                "inputs": {"alignment": [alignment]},
                "params": {"include_reference": False},
                "files": {
                    "f1": {
                        "name": "alignment.fasta",
                        "path": str(tmp_path / "out" / "alignment.fasta"),
                    }
                },
            }
        )
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(PLUGINS / "protein" / "scripts" / "consensus.py"), "protein"],
        env={
            "IKI_INPUT": str(tmp_path / "consensus.json"),
            "IKI_OUTPUT": str(tmp_path / "consensus-output.json"),
            "IKI_OUT_DIR": str(tmp_path / "out"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    made = json.loads((tmp_path / "consensus-output.json").read_text())
    (created,) = made["creates"]
    assert created["values"]["sequence"] == PROTEIN[:41] + "K" + PROTEIN[42:200]
    assert made["metrics"]["ambiguous"] == 0


@pytest.mark.parametrize(
    ("codon", "named"),
    [
        # IUBMB: RAY is GAC, GAT, AAC or AAT, so Asp or Asn (B), never Ala.
        ("RAY", "B"),
        ("SAR", "Z"),
        ("WTA", "J"),
    ],
)
def test_an_ambiguous_codon_that_cant_be_the_residue_is_a_difference(
    tmp_path: Path, codon: str, named: str
) -> None:
    protein = PROTEIN[:41] + "A" + PROTEIN[42:]
    code = coding(PROTEIN[:41]) + codon + coding(PROTEIN[42:200])

    _, output = run(tmp_path, {"r.fasta": f">r\n{code}\n".encode()}, sequence=protein)

    assert output is not None
    assert texts(output)[1] == f"Differences from PRT-000001: p.(A42{named}) (r)."


def test_a_reads_row_says_what_its_x_could_be(tmp_path: Path) -> None:
    """IUBMB and NCBI's standard code: AAN is AAA, AAC, AAG or AAT, so Lys or Asn, which
    no one letter names; TGN is Cys, Trp or a stop. The read's row keeps each as X, and its
    header lists what each could be, by column, so the row says what the read found."""
    # An Ala at 42, which AAN can't be, and a Cys at 60, which TGN can.
    protein = PROTEIN[:41] + "A" + PROTEIN[42:59] + "C" + PROTEIN[60:]
    code = coding(PROTEIN[:41]) + "AAN" + coding(PROTEIN[42:59]) + "TGN" + coding(PROTEIN[60:200])

    _, output = run(tmp_path, {"r.fasta": f">r\n{code}\n".encode()}, sequence=protein)

    assert output is not None
    header, row = rows(tmp_path)[1]
    assert (row[41], row[59]) == ("X", "X")
    assert header.endswith(" possible=42:KN,60:*CW")
    assert texts(output)[1] == "Differences from PRT-000001: p.(A42X) (r)."
    # B, Z and J name theirs, and NNN could be anything: none needs listing.
    _, output = run(
        tmp_path,
        {"r.fasta": f">r\n{coding(PROTEIN[:41])}RAYNNN{coding(PROTEIN[43:200])}\n".encode()},
        sequence=protein,
    )
    assert "possible=" not in rows(tmp_path)[1][0]


def test_codons_too_ambiguous_to_tell_cover_nothing(tmp_path: Path) -> None:
    # Residues 1-100, with residues 41-50 read as NNN: they say nothing of 41-50.
    code = coding(PROTEIN[:40]) + "NNN" * 10 + coding(PROTEIN[50:100])

    _, output = run(tmp_path, {"r.fasta": f">r\n{code}\n".encode()})

    assert output is not None
    assert texts(output)[:3] == [
        "The reads cover 1–40, 51–100 of Kinase's 250 residues (36.0%). Not covered: 41–50, "
        "101–250.",
        "No differences from PRT-000001 where the reads cover it.",
        "Codons too ambiguous to tell (such as NNN) say nothing there, so they don't cover it: "
        "41–50 (r).",
    ]


def test_reads_that_dont_code_for_it_are_left_out_saying_so(tmp_path: Path) -> None:
    files = {
        "good.fastq": fastq(("good", coding(PROTEIN[50:250]), 40)),
        "stranger.fastq": fastq(("stranger", made_up(600, 99, "ACGT"), 40)),
        "poor.fastq": fastq(("poor", coding(PROTEIN[:100]), 3)),
    }

    _, output = run(tmp_path, files)

    assert output is not None
    warnings = [m["text"] for m in output["messages"] if m["level"] == "warning"]
    assert warnings == [
        "stranger doesn't code for any of PRT-000001 in any frame. It's left out.",
        "poor has no bases good enough to keep: it's left out.",
    ]
    assert [h.split()[0] for h, _ in rows(tmp_path)] == ["PRT-000001", "good"]
    assert output["metrics"]["aligned"] == 1


def test_many_reads_left_out_are_said_in_a_warning_for_each_reason(tmp_path: Path) -> None:
    strangers = [(f"stranger-{i:03}", made_up(300, 1000 + i, "ACGT"), 40) for i in range(120)]
    files = {
        "good.fastq": fastq(("good", coding(PROTEIN[50:250]), 40)),
        "strangers.fastq": fastq(*strangers),
        "poor.fastq": fastq(*[(f"poor-{i}", coding(PROTEIN[:100]), 3) for i in range(3)]),
    }

    _, output = run(tmp_path, files)

    assert output is not None  # its messages fit the run-output schema
    warnings = [m["text"] for m in output["messages"] if m["level"] == "warning"]
    assert len(warnings) == 2
    assert warnings[0].startswith(
        "120 reads don't code for any of PRT-000001 in any frame, so they're left out: "
        "stranger-000, stranger-001, "
    )
    assert warnings[0].endswith(", stranger-099, and 20 more. left_out.tsv lists each, with why.")
    assert warnings[1] == (
        "3 reads have no bases good enough to keep, so they're left out: poor-0, poor-1, poor-2. "
        "left_out.tsv lists each, with why."
    )
    left = (tmp_path / "out" / "left_out.tsv").read_text().splitlines()
    assert len(left) == 1 + 123
    assert output["metrics"]["aligned"] == 1


@pytest.mark.parametrize(
    ("files", "sequence", "said"),
    [
        (
            {"far.fasta": b">far\n" + made_up(600, 98, "ACGT").encode() + b"\n"},
            PROTEIN,
            "None of the reads code for any of PRT-000001.",
        ),
        ({"r.fasta": b">r\nACGT\n"}, "", "PRT-000001 has no residues to align to."),
        ({"r.fasta": b">r\nACGT\n"}, "MK1L", "PRT-000001 has '1', which isn't a residue."),
        ({"r.fasta": b">r\nACGT\n"}, "MK*L*", "PRT-000001 has a stop inside it, at residue 3."),
        (
            {"notes.txt": b"hello"},
            PROTEIN,
            "notes.txt isn't an AB1 trace, a FASTQ file or a FASTA file.",
        ),
    ],
)
def test_what_cant_be_aligned_fails_its_run_saying_why(
    tmp_path: Path, files: dict[str, bytes], sequence: str, said: str
) -> None:
    result, output = run(tmp_path, files, sequence=sequence)

    assert output is None
    assert result.returncode == 1, result.stderr
    assert result.stdout.strip().splitlines()[-1] == f"::error {said}"


@pytest.mark.parametrize(
    "name",
    [
        "Read",
        "mott",
        "unique",
        "read_file",
        "ranges",
        "spans",
        "percent",
        "Left",
        "short",
        "fitted",
        "left_out",
        "fail",
    ],
)
def test_reading_reads_is_kept_as_the_dna_plugin_has_it(name: str) -> None:
    # Each plugin ships its own scripts, so the protein plugin has a copy of these.
    mine = load(SCRIPT, "protein_reads")
    theirs = load(DNA_SCRIPT, "dna_reads")
    assert inspect.getsource(getattr(mine, name)) == inspect.getsource(getattr(theirs, name))
    assert (mine.LIMIT, mine.MAX_READS, mine.MAX_READ, mine.MAX_LISTED, mine.MAX_TEXT) == (
        theirs.LIMIT,
        theirs.MAX_READS,
        theirs.MAX_READ,
        theirs.MAX_LISTED,
        theirs.MAX_TEXT,
    )

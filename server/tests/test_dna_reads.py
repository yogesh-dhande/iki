"""The dna plugin's alignment of reads to a reference: trace, FASTQ and FASTA files, read
by their first bytes, trimmed, aligned on the better strand and round a circular
reference's origin, into an iki/core.Alignment, with where they cover it and every
difference from it."""

import importlib.util
import json
import random
import re
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


def rows(tmp_path: Path, name: str = "alignment.fasta") -> list[tuple[str, str]]:
    """The alignment's records: (header, row)."""
    out: list[tuple[str, str]] = []
    for line in (tmp_path / "out" / name).read_text().splitlines():
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
    assert created["type"] == "iki/core.Alignment"
    values = created["values"]
    assert values["reference"] == values["members"][0] == created["derived_from"][0]
    assert values["name"] == "1 read on pTEST"
    assert values["aligned"] == {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"}
    assert values["method"] == (
        "Biopython PairwiseAligner, local, on both strands: match 2, mismatch -3, a gap of n "
        "bases costing 5 + 2(n - 1); reads trimmed by Mott's algorithm, limit 0.05"
    )
    (ref_header, ref_row), (read_header, read_row) = rows(tmp_path)
    assert (ref_header, ref_row) == ("DNA-000001 pTEST", PLASMID)
    assert read_header == (
        "clone1_M13F forward, bases 1–200 of 200 (trimmed from 216), from clone1_M13F.ab1; "
        "on DNA-000001 101–300"
    )
    # `.` where the read has no observation, beyond its ends.
    assert read_row == "." * 100 + "".join(read) + "." * 300
    assert texts(output)[:2] == [
        "The reads cover 101–300 of pTEST's 600 bases (33.3%). Not covered: 1–100, 301–600.",
        f"Differences from DNA-000001: g.151{PLASMID[150]}>{swapped} (clone1_M13F Q40).",
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
    # The deletion is `-`; beyond the read's ends it has no observation, `.`.
    assert row == (
        "." * 200
        + PLASMID[200:gone]
        + "-"
        + PLASMID[gone + 1 : added]
        + "T"
        + PLASMID[added:400]
        + "." * 200
    )
    differences = texts(output)[1]
    assert f"g.{gone + 1}del (rev1)" in differences
    assert f"g.{added}_{added + 1}insT (rev1 Q40)" in differences


@pytest.mark.parametrize(
    ("reference", "at", "bases", "circular", "named"),
    [
        # HGVS (hgvs-nomenclature.org, DNA duplication and insertion): the 3' rule, and a
        # duplication named without its bases (g.5dup, not g.5dupA). An A inserted anywhere
        # in AAA is a duplication of its last.
        ("CGAAATC", 2, "A", False, "g.5dup"),
        ("CGAAATC", 5, "A", False, "g.5dup"),
        # Two bases repeating the two before them.
        ("CGTATAC", 6, "TA", False, "g.5_6dup"),
        # Inserted between two bases unlike it: the two adjacent places, then the bases.
        ("CGAAATC", 1, "T", False, "g.1_2insT"),
        # At a linear sequence's end: a copy of its last base is a duplication; anything
        # else stays inside it, before the last base, not beyond it.
        ("CCGA", 3, "A", False, "g.4dup"),
        ("CCGA", 3, "AT", False, "g.3_4insAT"),
        # A circular genomic reference is `o.` (HGVS's reference sequence types), and its
        # first base follows its last: an A before the first after ...GA is a duplication
        # of the first, the 3'-most A; a T there lies between the last and the first.
        ("ACGA", 3, "A", True, "o.1dup"),
        ("ACGA", 3, "A", False, "g.4dup"),
        ("GACC", 0, "T", True, "o.4_1insT"),
    ],
)
def test_insertions_are_named_as_hgvs_names_them(
    reference: str, at: int, bases: str, circular: bool, named: str
) -> None:
    reads = script()
    assert reads.insertion(reference, at, bases, circular) == named


def test_deletions_move_3_prime_in_repeated_bases() -> None:
    reads = script()
    # HGVS (DNA deletion): named without the bases deleted, g.5del, not g.5delA.
    assert reads.deletion("CGAAATC", 2, 2) == "g.5del"
    # TA from TATA: the last two, 5 and 6.
    assert reads.deletion("CGTATAC", 2, 3) == "g.5_6del"
    assert reads.deletion("CGAAATC", 1, 1) == "g.2del"
    # On a circular reference, one with the last base and the first is named from the one
    # to the other, as HGVS's deletion recommendation allows for o. and m.
    assert reads.deletion("GACT", 3, 0, circular=True) == "o.4_1del"
    # And it moves on round the origin: an A from the AA across it is the first.
    assert reads.deletion("ACGA", 3, 3, circular=True) == "o.1del"


def test_a_read_across_a_circular_references_origin_aligns_in_one_piece(tmp_path: Path) -> None:
    across = PLASMID[-100:] + PLASMID[:100]

    _, output = run(tmp_path, {"origin.fasta": f">ori\n{across}\n".encode()}, topology="circular")

    assert output is not None
    (_, _), (_, row) = rows(tmp_path)
    # No observation between the read's ends is `.`, not a 400-base deletion.
    assert row == PLASMID[:100] + "." * 400 + PLASMID[-100:]
    assert texts(output)[:2] == [
        "The reads cover 1–100, 501–600 of pTEST's 600 bases (33.3%). Not covered: 101–500.",
        "No differences from DNA-000001 where the reads cover it.",
    ]
    assert ", round the origin" in output["creates"][0]["values"]["method"]
    # Aligned FASTA as other tools read it has no `.`: Biopython 1.88 takes one for a
    # residue, so Iki's file reads as 400 mismatches. The standard file has `-` there, and
    # the read's header says where on the plasmid it lies, which a `-` can't.
    from Bio import Align

    def counts(name: str) -> tuple[int, int, int]:
        found = Align.read(tmp_path / "out" / name, "fasta").counts()  # type: ignore[no-untyped-call]
        return found.identities, found.mismatches, found.gaps

    assert counts("alignment.fasta") == (200, 400, 0)
    assert counts("alignment-standard.fasta") == (200, 0, 400)
    (_, _), (header, standard) = rows(tmp_path, "alignment-standard.fasta")
    assert standard == PLASMID[:100] + "-" * 400 + PLASMID[-100:]
    assert header.endswith("; on DNA-000001 1–100, 501–600")
    assert texts(output)[-1].startswith("In alignment.fasta, the alignment's file, a read's `.`")


#: pUC19 as GenBank L09137.2 has it (NCBI nuccore, fetched 2026-11-19, FASTA): 2,686 bases,
#: circular (its LOCUS says so), from TCGCGCGTTT to ...CCCTTTCGTC.
PUC19 = "".join((Path(__file__).parent / "data" / "L09137.2.fasta").read_text().splitlines()[1:])


def test_differences_from_puc19_are_named_as_hgvs_names_them(tmp_path: Path) -> None:
    """A read of pUC19's bases 151-450 with one change of each kind, each named by HGVS's
    DNA recommendations (hgvs-nomenclature.org): `o.` for a plasmid, a circular genomic
    reference; deletions and duplications without their bases; the 3' rule; and two changed
    bases side by side as one deletion-insertion, not two substitutions."""
    assert len(PUC19) == 2686
    # The bases at the places changed, counting from 1, as the record has them.
    assert PUC19[200] == "C"  # 201
    assert PUC19[219:225] == "GAAAAT"  # 221-224 are AAAA
    assert PUC19[250:252] == "CA"  # 251-252
    assert PUC19[316:323] == "AGGGGGA"  # 318-322 are GGGGG
    assert PUC19[395:401] == "GAATTC"  # 396-401, its EcoRI site
    read = (
        PUC19[150:200]
        + "T"  # 201 C to T
        + PUC19[201:220]
        + PUC19[221:250]  # one of 221-224's As gone
        + "TT"  # 251-252 CA to TT, two changed bases side by side
        + PUC19[252:322]
        + "G"  # a sixth G after 318-322's five
        + PUC19[322:400]
        + "G"  # a G inside EcoRI's site, between its T (400) and C (401)
        + PUC19[400:450]
    )

    _, output = run(
        tmp_path, {"r1.fasta": f">r1\n{read}\n".encode()}, bases=PUC19, topology="circular"
    )

    assert output is not None
    assert texts(output)[1] == (
        "Differences from DNA-000001: o.201C>T (r1); o.224del (r1); o.251_252delinsTT (r1); "
        "o.322dup (r1); o.400_401insG (r1)."
    )
    assert output["metrics"]["differences"] == 5


def names_for(reference: str, read: str, *, circular: bool = False) -> list[str]:
    """What the module names a read's differences, from the placement itself: the naming
    on its own, with no aligner choosing where the read lies."""
    module = script()
    placed = module.Placed(
        module.Read("r", read, None, "r.fasta"),
        False,
        0,
        len(read),
        dict(enumerate(read)),
        span=(0, len(read)),
    )
    return [name for _, name, _ in module.differences(placed, reference, circular)]


def test_bases_replaced_by_their_reverse_complement_are_an_inversion() -> None:
    """HGVS's inversion recommendation: a change where more than one nucleotide replacing
    the original sequence is the reverse complement of that sequence is an inversion,
    g.3_6inv, not a deletion-insertion. A single base's reverse complement stays a
    substitution, as that recommendation disallows a one-base inversion, and a
    replacement that isn't the reverse complement stays a deletion-insertion."""
    # The shape of HGVS's own example, NC_000023.10:g.32361330_32361333inv: four bases
    # for their reverse complement, with the flanks unchanged.
    assert names_for("CATCAGCCT", "CACTGACCT") == ["g.3_6inv"]
    assert names_for("GGCAGG", "GGTGGG") == ["g.3_4inv"]
    assert names_for("GGCAGG", "GGTTGG") == ["g.3_4delinsTT"]
    assert names_for("GGCAGG", "GGGAGG") == ["g.3C>G"]
    # pUC19's 251-252 is CA, so TG there is an inversion and TT a deletion-insertion.
    assert PUC19[250:252] == "CA"
    assert names_for(PUC19, PUC19[:250] + "TG" + PUC19[252:], circular=True) == ["o.251_252inv"]
    assert names_for(PUC19, PUC19[:250] + "TT" + PUC19[252:], circular=True) == [
        "o.251_252delinsTT"
    ]


def test_a_deletion_and_a_change_in_the_same_repeat_are_one_change(tmp_path: Path) -> None:
    """pUC19's 221-224 are AAAA, with a G before and a T after. A read with AAC there has
    lost one A and changed another, and the aligner places the gap and the mismatch apart.
    Named on their own, the 3' rule carries the deletion onto the very base the
    substitution names: no sequence has both a base deleted and that base changed. They
    are one change over the stretch that differs, o.223_224delinsC, which spells the
    read's bases back exactly."""
    assert PUC19[219:225] == "GAAAAT"
    read = PUC19[150:220] + "AAC" + PUC19[224:450]

    _, output = run(
        tmp_path, {"r.fasta": f">r\n{read}\n".encode()}, bases=PUC19, topology="circular"
    )

    assert output is not None
    assert texts(output)[1] == "Differences from DNA-000001: o.223_224delinsC (r)."
    # Applying that change to the reference gives the read's bases back: 223-224 (AA) out,
    # C in, so pUC19's GAAAAT reads GAACT.
    assert PUC19[219:222] + "C" + PUC19[224] == "GAACT"
    assert read[69:74] == "GAACT"
    # A deletion or a duplication alone in the same repeat still normalises as before.
    for bases, want in (("AAA", "o.224del"), ("AAAAA", "o.224dup")):
        _, output = run(
            tmp_path,
            {"r.fasta": f">r\n{PUC19[150:220] + bases + PUC19[224:450]}\n".encode()},
            bases=PUC19,
            topology="circular",
        )
        assert output is not None, want
        assert texts(output)[1] == f"Differences from DNA-000001: {want} (r)."


def test_a_read_that_lies_equally_well_in_two_places_is_left_out(tmp_path: Path) -> None:
    """A read inside one of two identical copies of a stretch lies as well on either, and
    bowtie2 gives such a read a mapping quality of 0. Neither the coverage nor a difference
    in it belongs to one copy rather than the other, so the read is left out with that
    reason instead of being attributed to the copy the aligner happened to return."""
    copy = made_up(30, 3)
    reference = copy + made_up(10, 4) + copy
    changed = "G" if copy[14] != "G" else "T"
    tied = copy[:14] + changed + copy[15:]
    # A read over the join lies in one place only, so the run has something to align.
    only = reference[20:55]

    _, output = run(
        tmp_path,
        {"r.fasta": f">tied\n{tied}\n>only\n{only}\n".encode()},
        bases=reference,
        topology="linear",
    )

    assert output is not None
    assert output["metrics"]["aligned"] == 1
    assert output["metrics"]["differences"] == 0
    warnings = [m["text"] for m in output["messages"] if m["level"] == "warning"]
    assert warnings == [
        "tied lies equally well in 2 places on DNA-000001, so nothing it shows belongs to "
        "one of them rather than another: it's left out."
    ]
    left = (tmp_path / "out" / "left_out.tsv").read_text().splitlines()
    assert left[0] == "read\tfile\twhy"
    assert "lies equally well in 2 places on DNA-000001" in left[1]


def test_a_read_round_a_circular_reference_twice_is_left_out(tmp_path: Path) -> None:
    """A read longer than a circular reference passes each base more than once, and the
    alignment holds one base per place: two passes that disagree can't both be shown, so
    one observation would be lost and the other claimed twice. Such a read is left out,
    with what to do about it."""
    small = PUC19[:80]
    read = small + small
    good = small[:60]

    _, output = run(
        tmp_path,
        {"r.fasta": f">twice\n{read}\n>once\n{good}\n".encode()},
        bases=small,
        topology="circular",
    )

    assert output is not None
    assert output["metrics"]["aligned"] == 1
    warnings = [m["text"] for m in output["messages"] if m["level"] == "warning"]
    assert len(warnings) == 1
    assert "twice reads round DNA-000001 more than once (160 bases of its 80)" in warnings[0]
    assert "Align it in parts." in warnings[0]


def test_a_read_across_the_origin_lies_across_anothers_insertion_there(tmp_path: Path) -> None:
    """Two reads round pUC19's origin, its last 35 bases and its first 35: one as the
    plasmid has them, one with an A between its last base (2686, C) and its first (1, T).
    The first reads on from 2686 to 1, so where the other has the A it has nothing, `-`,
    which says the A isn't there; `.`, no observation, would hide that."""
    plain = PUC19[-35:] + PUC19[:35]
    extra = PUC19[-35:] + "A" + PUC19[:35]

    _, output = run(
        tmp_path,
        {"ori.fasta": f">plain\n{plain}\n>extra\n{extra}\n".encode()},
        bases=PUC19,
        topology="circular",
    )

    assert output is not None
    middle = "." * (2686 - 70)
    assert [row for _, row in rows(tmp_path)] == [
        "-" + PUC19,
        "-" + PUC19[:35] + middle + PUC19[-35:],
        "A" + PUC19[:35] + middle + PUC19[-35:],
    ]
    # HGVS's insertion takes two adjacent places, and on a circle the last and the first are.
    assert texts(output)[1] == "Differences from DNA-000001: o.2686_1insA (extra)."


def test_uncalled_bases_cover_nothing_and_hide_no_difference(tmp_path: Path) -> None:
    # Twenty N where the clone may carry a mutation: they say nothing of bases 141-160, so
    # those aren't covered, and no difference is claimed for or against them.
    read = PLASMID[100:140] + "N" * 20 + PLASMID[160:200]

    _, output = run(tmp_path, {"n.fasta": f">n\n{read}\n".encode()})

    assert output is not None
    assert texts(output)[:3] == [
        "The reads cover 101–140, 161–200 of pTEST's 600 bases (13.3%). Not covered: 1–100, "
        "141–160, 201–600.",
        "No differences from DNA-000001 where the reads cover it.",
        "Ambiguous bases (such as N) say nothing there, so they don't cover it: 141–160 (n).",
    ]
    assert output["metrics"]["coverage_percent"] == 13.3


def test_bases_compare_as_the_sets_their_iupac_codes_stand_for() -> None:
    reads = script()
    # IUBMB: R is A or G, Y is C or T, N any.
    assert reads.call("Y", "R") == "differs"
    assert reads.call("A", "R") == "resolves"
    assert reads.call("R", "A") == "unresolved"
    assert reads.call("N", "C") == "unresolved"
    assert reads.call("A", "A") == "same"
    assert reads.call("T", "A") == "differs"


def test_an_ambiguous_read_base_that_cant_be_the_references_is_a_difference(
    tmp_path: Path,
) -> None:
    reference = PLASMID[:30] + "R" + PLASMID[31:]
    disjoint = reference[:30] + "Y" + reference[31:120]
    resolving = reference[:30] + "A" + reference[31:120]

    _, output = run(
        tmp_path,
        {"r.fasta": f">disjoint\n{disjoint}\n>resolving\n{resolving}\n".encode()},
        bases=reference,
    )

    assert output is not None
    assert texts(output)[1:3] == [
        "Differences from DNA-000001: g.31R>Y (disjoint).",
        "Where DNA-000001 is ambiguous, the reads have: R31A (resolving).",
    ]


def test_a_read_with_nothing_scoring_above_zero_is_left_out_and_the_others_kept(
    tmp_path: Path,
) -> None:
    files = {"r.fasta": f">blank\n{'N' * 24}\n>good\n{PLASMID[0:100]}\n".encode()}

    _, output = run(tmp_path, files)

    assert output is not None
    warnings = [m["text"] for m in output["messages"] if m["level"] == "warning"]
    assert warnings == [
        "blank doesn't align to DNA-000001: no stretch of it scores above zero. It's left out."
    ]
    assert output["metrics"]["aligned"] == 1


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


def test_a_thousand_reads_mostly_left_out_still_align_the_rest(tmp_path: Path) -> None:
    # The most reads a run takes: one good, the rest each left out for one of two reasons.
    blanks = "".join(f">blank-{i:03}\n{'N' * 24}\n" for i in range(600))
    poor = [(f"poor-{i:03}", PLASMID[0:200], 3) for i in range(399)]
    files = {
        "blank.fasta": blanks.encode(),
        "poor.fastq": fastq(*poor),
        "good.fastq": fastq(("good", PLASMID[100:400], 40)),
    }

    _, output = run(tmp_path, files)

    assert output is not None  # its messages fit the run-output schema
    assert output["metrics"] == {
        "reads": 1000,
        "aligned": 1,
        "coverage_percent": 50.0,
        "differences": 0,
    }
    warnings = [m["text"] for m in output["messages"] if m["level"] == "warning"]
    assert len(warnings) == 2
    assert warnings[0].startswith(
        "600 reads don't align to DNA-000001, so they're left out: "
        "blank-000 (no stretch of it scores above zero), "
        "blank-001 (no stretch of it scores above zero), "
    )
    assert warnings[0].endswith(" more. left_out.tsv lists each, with why.")
    assert warnings[1].startswith(
        "399 reads have no bases good enough to keep, so they're left out: poor-000, poor-001, "
    )
    assert all(len(w) <= 2000 for w in warnings)
    left = (tmp_path / "out" / "left_out.tsv").read_text().splitlines()
    assert left[0] == "read\tfile\twhy"
    assert len(left) == 1 + 999
    assert left[1] == (
        "blank-000\tblank.fasta\tblank-000 doesn't align to DNA-000001: "
        "no stretch of it scores above zero. It's left out."
    )
    assert (
        left[-1]
        == "poor-398\tpoor.fastq\tpoor-398 has no bases good enough to keep: it's left out."
    )


def test_a_long_list_of_differences_fits_in_its_message(tmp_path: Path) -> None:
    # Every fourth base changed, in reads with long names: more than a message holds.
    changed = "".join(
        ("A" if b != "A" else "C") if i % 4 == 2 else b for i, b in enumerate(PLASMID)
    )
    name = "clone-7-primer-M13-forward-plate-3-well-H12-sequenced-2026-11-02"
    files = {"r.fastq": fastq((name, changed[0:300], 40), (name, changed[300:600], 40))}

    _, output = run(tmp_path, files, trim=False)

    assert output is not None  # its messages fit the run-output schema
    count = output["metrics"]["differences"]
    assert count > 100
    listed = next(t for t in texts(output) if t.startswith("Differences from DNA-000001: "))
    assert len(listed) <= 2000
    first = f"g.3{PLASMID[2]}>{changed[2]} ({name} Q40)"
    assert listed.startswith(f"Differences from DNA-000001: {first}; ")
    shown = re.fullmatch(r"Differences from DNA-000001: (.*); and (\d+) more\.", listed)
    assert shown is not None
    assert len(shown[1].split("; ")) + int(shown[2]) == count


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

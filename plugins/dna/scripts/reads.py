"""`align_reads`: aligns sequencing reads to a reference, such as Sanger reads of a
construct to the plasmid it should be, into an `iki/core.Alignment` (ADR 0088).

Reads come from the run's files: AB1 traces, one read each, named by their file, or FASTQ
or FASTA files of any number of reads, named by their records. Each file's format is
read from its first bytes, so a file's extension or media type doesn't matter.

Each read with qualities (AB1 and FASTQ) is first trimmed of its poor ends by Mott's
algorithm, as phred and Biopython do: the stretch where the sum of (limit - error
probability) is greatest, with a limit of 0.05. Then it's aligned locally to the
reference on both strands, and the better is kept, with BLASTN's match and mismatch
scores (2 and -3), a gap of n bases costing 5 + 2(n - 1), and N matching anything at no
score. (BLASTN's gap opening 5 and extension 2 would charge 5 + 2n: here the opening
cost is the first base's, as in EMBOSS and Biopython.) A circular reference is aligned
as if it went round again, so a read across its origin aligns in one piece. A read
aligns if at least half its trimmed bases (and 20) do; others are left out and the run
says so, as is one with no stretch that scores above zero: a warning for each reason,
naming the reads, and a row for each read, with why, in the artifact `left_out.tsv`.
A read that lies equally well on another stretch, or as well on both strands, is left
out too: nothing it shows belongs to the place it was put on rather than the other
(bowtie2 gives such a read a mapping quality of 0). So is one that reads round a
circular reference more than once, which passes a place twice where the alignment holds
one base per place.

The alignment's first row is the reference, headed by its HRID; each read's row follows,
headed by its name, as it lies on the reference (its reverse complement if it read the
other strand). Bases a read has that the reference doesn't get columns of their own.

The run reports where the reads cover the reference, and each difference from it, in
the reference's numbering. A base covers its place when it says what's there: a base
of A, C, G or T, a deletion, or an ambiguity code whose bases can't be the reference's.
N, or another ambiguity code that could be the reference's base, resolves nothing, so
it covers nothing, and the run lists where reads have only such bases. Bases are
compared as the sets of bases their IUPAC codes stand for: a difference is a read base
none of whose bases the reference's code allows. Where the reference itself is
ambiguous (R, N, ...), the base a read has there is listed apart, as resolving it.
Differences are named as HGVS's DNA recommendations name them, with the reference as the
reference sequence, numbered from its first base: `o.` for a circular one (a plasmid),
`g.` for a linear one. They are substitutions (g.1234C>T), deletions (g.1240del,
g.1240_1242del), insertions (g.1250_1251insG), duplications (g.1250dup), inversions, where
more than one base is replaced by its own reverse complement (g.3_6inv), and, for other
changes side by side, deletion-insertions (g.1260_1261delinsTA), with the reads that show
each and their least quality there. A deletion or insertion in repeated bases is named at
its 3'-most place, as HGVS asks; on a circular reference, its origin is no end, so one may
move on round it, and one across it is named from its last place to its first
(o.2686_1insA). Where that rule would carry a deletion or an insertion onto a base another
change names, the two are one change over the whole stretch that differs: no sequence has
both a base deleted and that base changed. The rows show them where the aligner put them.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from Bio import Align, SeqIO
from Bio.Seq import Seq

#: The most reads a run aligns.
MAX_READS = 1000
#: The longest read, after trimming: Sanger reads are about 1,000 bases.
MAX_READ = 20_000
#: The most cells one read's alignment may fill: its bases times the reference's.
MAX_CELLS = 100_000_000
#: Mott's limit on a base's error probability, as phred's default.
LIMIT = 0.05
#: The least a read must align: this share of its trimmed bases, and this many.
LEAST_SHARE, LEAST_BASES = 0.5, 20
#: The most differences the run lists.
MAX_LISTED = 100
#: The most of a read's equally scoring placements looked at, to see whether more than one
#: stretch of the reference explains it as well as the stretch it was put on.
MOST_PLACEMENTS = 50
#: The longest message a run may give (the run-output schema's limit).
MAX_TEXT = 2000
#: The bases each IUPAC code stands for (IUBMB, 1985).
IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T", "U": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip


def fail(why: str) -> NoReturn:
    print(f"::error {why}", flush=True)
    sys.exit(1)


@dataclass
class Read:
    name: str
    bases: str
    #: Phred qualities, one per base, or None for FASTA.
    quality: list[int] | None
    source: str
    #: How long it was before trimming.
    length: int = 0


@dataclass
class Placed:
    """A read as it lies on the reference."""

    read: Read
    reverse: bool
    #: The trimmed read's bases that aligned, 0-based, end exclusive.
    start: int
    end: int
    #: Reference position (0-based) → the read's base there, or "-" where it has none.
    at: dict[int, str] = field(default_factory=dict)
    #: Reference position → the read's quality there, where it has a base.
    quality_at: dict[int, int] = field(default_factory=dict)
    #: Reference position → bases the read has before it that the reference doesn't.
    inserted: dict[int, str] = field(default_factory=dict)
    inserted_quality: dict[int, int] = field(default_factory=dict)
    #: Where it lies on the aligned target, 0-based, end exclusive: past the reference's
    #: length, it's gone round a circular one's origin.
    span: tuple[int, int] = (0, 0)
    #: Reference positions the read lies across the place just before: it reads on from
    #: the base before (round the origin, on a circular reference) without ending there.
    across: set[int] = field(default_factory=set)
    #: How many stretches of the reference the read lies on equally well, counting at most
    #: MOST_PLACEMENTS of them: more than one, and nothing it shows belongs to any of them.
    places: int = 1
    #: Whether it scores the same on both strands, which says nothing of which it read.
    either_strand: bool = False


def mott(quality: list[int]) -> tuple[int, int]:
    """The best stretch by Mott's algorithm: start and end (exclusive), or (0, 0)."""
    best = running = 0.0
    start = end = here = 0
    for index, q in enumerate(quality):
        running += LIMIT - 10 ** (-q / 10)
        if running < 0:
            running, here = 0.0, index + 1
        elif running > best:
            best, start, end = running, here, index + 1
    return start, end


def unique(name: str, taken: set[str]) -> str:
    name = re.sub(r"\s+", "_", name.strip()) or "read"
    found, n = name, 2
    while found in taken:
        found, n = f"{name}-{n}", n + 1
    taken.add(found)
    return found


def read_file(path: Path, shown: str, taken: set[str]) -> list[Read]:
    """The reads in one file, by its format."""
    head = path.open("rb").read(4)
    out: list[Read] = []
    try:
        if head == b"ABIF":
            record = SeqIO.read(path, "abi")
            stem = Path(shown).stem or record.id
            out.append(
                Read(
                    unique(stem, taken),
                    str(record.seq).upper(),
                    list(record.letter_annotations["phred_quality"]),
                    shown,
                )
            )
        elif head[:1] == b"@":
            out.extend(
                Read(
                    unique(record.id, taken),
                    str(record.seq).upper(),
                    list(record.letter_annotations["phred_quality"]),
                    shown,
                )
                for record in SeqIO.parse(path, "fastq")
            )
        elif head[:1] == b">":
            out.extend(
                Read(unique(record.id, taken), str(record.seq).upper(), None, shown)
                for record in SeqIO.parse(path, "fasta")
            )
        else:
            fail(f"{shown} isn't an AB1 trace, a FASTQ file or a FASTA file.")
    except (ValueError, KeyError, AssertionError) as error:
        fail(f"{shown} couldn't be read: {error}.")
    # A record with no bases (a lone `>`) is no read.
    out = [each for each in out if each.bases]
    for each in out:
        each.length = len(each.bases)
        bad = re.search(r"[^ACGTURYSWKMBDHVN]", each.bases)
        if bad:
            fail(f"{each.name} in {shown} has {bad.group()!r}, which isn't a base.")
        each.bases = each.bases.replace("U", "T")
    return out


def aligner() -> Align.PairwiseAligner:
    found = Align.PairwiseAligner(
        mode="local",
        match_score=2,
        mismatch_score=-3,
        open_gap_score=-5,
        extend_gap_score=-2,
    )
    found.wildcard = "N"
    return found


def place(
    read: Read, reference: str, circular: bool, scorer: Align.PairwiseAligner
) -> Placed | None:
    """Where the read lies on the reference, on its better strand; None if no stretch of it
    scores above zero there (all N, say), when a local alignment has nothing to give."""
    size = len(reference)
    target = reference + reference[: len(read.bases) - 1] if circular else reference
    if len(target) * len(read.bases) > MAX_CELLS:
        raise ValueError(
            f"it's too long to align here: {len(read.bases):,} bases by {len(target):,} "
            f"is more than {MAX_CELLS:,} cells"
        )
    forward = read.bases
    backward = str(Seq(read.bases).reverse_complement())
    scores = scorer.score(target, forward), scorer.score(target, backward)
    if max(scores) <= 0:
        return None
    reverse = scores[1] > scores[0]
    query = backward if reverse else forward
    quality = (read.quality[::-1] if reverse else read.quality) if read.quality else None
    found = scorer.align(target, query)
    best = found[0]
    coordinates = best.coordinates
    placed = Placed(read, reverse, int(coordinates[1][0]), int(coordinates[1][-1]))
    # Where else it lies as well. A read inside repeated bases lies equally on each copy,
    # and then no base it has belongs to one of them rather than another; bowtie2 gives
    # such a read a mapping quality of 0. Alignments differing only in where their gaps
    # fall are one placement, so the stretches are counted, not the alignments.
    stretches = set()
    for n, each in enumerate(found):
        if n >= MOST_PLACEMENTS:
            break
        where = each.coordinates
        stretches.add((int(where[0][0]) % size, int(where[0][-1]) % size))
    placed.places = len(stretches)
    placed.either_strand = scores[0] == scores[1]
    # Where it begins it doesn't lie across: the place before its first base, even when
    # that's the reference's last (round the origin), is one it has no observation of.
    first, last = int(coordinates[0][0]), int(coordinates[0][-1])
    placed.span = (first, last)
    placed.across = {spot % size for spot in range(first + 1, last)}
    for (t0, q0), (t1, q1) in zip(coordinates.T[:-1], coordinates.T[1:], strict=True):
        t0, q0, t1, q1 = int(t0), int(q0), int(t1), int(q1)
        if t1 > t0 and q1 > q0:
            for offset in range(t1 - t0):
                spot = (t0 + offset) % size
                placed.at[spot] = query[q0 + offset]
                if quality:
                    placed.quality_at[spot] = quality[q0 + offset]
        elif t1 > t0:
            for spot in range(t0, t1):
                placed.at[spot % size] = "-"
        else:
            spot = t0 % size
            placed.inserted[spot] = placed.inserted.get(spot, "") + query[q0:q1]
            if quality:
                placed.inserted_quality[spot] = min(quality[q0:q1])
    # On the other strand, which of the read's own bases aligned, in its own numbering.
    if reverse:
        placed.start, placed.end = len(query) - placed.end, len(query) - placed.start
    return placed


def numbered(circular: bool) -> str:
    """HGVS's prefix for the reference's numbering, counting from its first base: `o.` for a
    circular genomic reference sequence (a plasmid, say), `g.` for a linear one."""
    return "o." if circular else "g."


def deletion(reference: str, first: int, last: int, circular: bool = False) -> str:
    """A deletion's name, 0-based `first` to `last` gone, moved as far 3' as the bases
    allow, as HGVS names them: `g.5del`, `g.5_6del` (without the bases deleted). On a
    circular reference it moves on round the origin, and one across it is named from its
    first place to its last (`o.2686_1del`), as HGVS allows there."""
    size = len(reference)
    for _ in range(size):
        after = last + 1
        if circular:
            after %= size
        elif after >= size:
            break
        if reference[after] != reference[first]:
            break
        first, last = (first + 1) % size, after
    where = f"{first + 1}" if first == last else f"{first + 1}_{last + 1}"
    return f"{numbered(circular)}{where}del"


def insertion(reference: str, spot: int, bases: str, circular: bool = False) -> str:
    """An insertion's name, `bases` inserted before 0-based `spot`, moved as far 3' as the
    bases allow, as HGVS names them (`g.1_2insT`); one that copies the bases just before it
    is a duplication of them (`g.5dup`, `g.5_6dup`). On a circular reference it moves on
    round the origin, and one before the first base is after the last (`o.2686_1insA`)."""
    size = len(reference)
    found = numbered(circular)
    for _ in range(size):
        if not circular and spot >= size:
            break
        if bases[0] != reference[spot % size]:
            break
        bases, spot = bases[1:] + reference[spot % size], spot + 1
    n = len(bases)
    if n <= size and (circular or spot >= n):
        before = "".join(reference[i % size] for i in range(spot - n, spot))
        if before == bases:
            first, last = (spot - n) % size + 1, (spot - 1) % size + 1
            return f"{found}{first}dup" if n == 1 else f"{found}{first}_{last}dup"
    if not circular and spot >= size:
        # Moved past the last base, it's beyond the reference: one place back, it's inside.
        spot, bases = spot - 1, reference[spot - 1] + bases[:-1]
    spot %= size
    return f"{found}{spot if spot else size}_{spot + 1}ins{bases}"


#: One change the read has: the reference's places it replaces, 0-based and end exclusive
#: (past the reference's length, round a circular origin; the two equal for an insertion,
#: which replaces nothing), and the bases the read has there instead (none for a deletion).
Change = tuple[int, int, str]


def stretch(reference: str, start: int, stop: int) -> str:
    """The reference's bases from 0-based ``start`` to ``stop``, round the origin."""
    size = len(reference)
    return "".join(reference[k % size] for k in range(start, stop))


def shifted(reference: str, change: Change, circular: bool) -> Change:
    """A change moved as far 3' as the bases allow, which is where HGVS names it. Only a
    deletion or an insertion alone moves: where bases replace bases, the bases themselves
    say which places changed."""
    start, stop, bases = change
    size = len(reference)
    if bool(bases) == (stop > start):
        return change
    for _ in range(size):
        if not circular and stop >= size:
            break
        after = reference[stop % size]
        if bases:
            if bases[0] != after:
                break
            bases = bases[1:] + after
        elif after != reference[start % size]:
            break
        start, stop = start + 1, stop + 1
    return start, stop, bases


def trimmed(reference: str, change: Change) -> Change:
    """A change with the places it leaves as they were taken off either end of it, so that
    what it names is what actually changed."""
    start, stop, bases = change
    while bases and stop > start and bases[0] == reference[start % len(reference)]:
        start, bases = start + 1, bases[1:]
    while bases and stop > start and bases[-1] == reference[(stop - 1) % len(reference)]:
        stop, bases = stop - 1, bases[:-1]
    return start, stop, bases


def merged(
    reference: str, changes: list[Change], qualities: list[list[int]], circular: bool
) -> tuple[list[Change], list[list[int]]]:
    """Changes that would name the same places, as one change, each with its qualities.

    A deletion or an insertion alone is named at its 3'-most place, which in repeated
    bases can be where the next change is: then the two name the same base, and no
    sequence has both -- a deletion of base 30 and a substitution at base 30 can't both
    be applied. What the read has is one change over the whole stretch, named from the
    reference's bases there and the read's (HGVS's deletion-insertion).
    """
    out, found = list(changes), [list(each) for each in qualities]
    index = 0
    while index < len(out) - 1:
        here, after = out[index], out[index + 1]
        _, reach, _ = shifted(reference, here, circular)
        if reach < after[0]:
            index += 1
            continue
        between = stretch(reference, here[1], after[0])
        out[index : index + 2] = [
            trimmed(reference, (here[0], after[1], here[2] + between + after[2]))
        ]
        found[index : index + 2] = [found[index] + found[index + 1]]
    return out, found


def named(reference: str, change: Change, circular: bool) -> str:
    """A change's HGVS name: a substitution, a deletion, an insertion or duplication, an
    inversion, or a deletion-insertion for anything else."""
    start, stop, bases = change
    size = len(reference)
    if not bases:
        return deletion(reference, start % size, (stop - 1) % size, circular)
    if start == stop:
        return insertion(reference, start % size, bases, circular)
    was = stretch(reference, start, stop)
    first, last = start % size, (stop - 1) % size
    if len(was) == 1 and len(bases) == 1:
        return f"{numbered(circular)}{first + 1}{was}>{bases}"
    where = f"{first + 1}" if first == last else f"{first + 1}_{last + 1}"
    # More than one base replaced by its own reverse complement is an inversion, which
    # HGVS names in its own right; a single base's reverse complement is a substitution.
    if len(bases) == len(was) > 1 and bases == str(Seq(was).reverse_complement()):
        return f"{numbered(circular)}{where}inv"
    return f"{numbered(circular)}{where}delins{bases}"


def call(base: str, reference: str) -> str:
    """How a read's base compares with the reference's there, by the bases their IUPAC codes
    stand for: `same`; `differs`, where none of the read's bases is one the reference's
    allows; `resolves`, a single base the reference's ambiguous code allows; or `unresolved`,
    an ambiguous base that could be the reference's, which says nothing of it."""
    mine, theirs = set(IUPAC.get(base, "ACGT")), set(IUPAC.get(reference, "ACGT"))
    if not mine & theirs:
        return "differs"
    if base == reference and len(mine) == 1:
        return "same"
    if len(mine) == 1:
        return "resolves"
    return "unresolved"


#: What the run says of its two files.
TWO_FILES = (
    "In alignment.fasta, the alignment's file, a read's `.` is a place it has no observation "
    "of, beyond its ends, and `-` a deletion. Other tools take `.` for a residue: give them "
    "alignment-standard.fasta, which has `-` for both, and in each read's header where it "
    "lies on the reference."
)


def write_alignments(out_dir: Path, rows: list[tuple[str, str]]) -> None:
    """The alignment twice. ``alignment.fasta`` is Iki's, the alignment's file: in a read's
    row, `.` is a place it has no observation of (beyond its ends), apart from `-`, a
    deletion, which the alignment viewer and the run's coverage tell apart. Other tools
    reading aligned FASTA take `.` for a residue, so ``alignment-standard.fasta`` has `-`
    for both, as they expect, and each read's header says where it lies on the reference,
    which is all a `-` there can't say."""
    for name, written in (
        ("alignment.fasta", rows),
        ("alignment-standard.fasta", [(h, row.replace(".", "-")) for h, row in rows]),
    ):
        (out_dir / name).write_text(
            "".join(
                f">{header}\n" + "".join(f"{row[i : i + 60]}\n" for i in range(0, len(row), 60))
                for header, row in written
            ),
            encoding="utf-8",
        )


def lies_across(each: Placed, k: int) -> bool:
    """Whether a read lies across the place just before position k, where others may have
    inserted: if not, it has no observation there, `.`, not a gap. On a circular reference,
    the place before the first base is after the last."""
    return k in each.across


def covered_by(placed: Placed, reference: str) -> set[int]:
    """The reference's places the read says what's at: a deletion, or a base that isn't
    unresolved there (an N covers nothing)."""
    return {
        spot
        for spot, base in placed.at.items()
        if base == "-" or call(base, reference[spot]) != "unresolved"
    }


def differences(
    placed: Placed, reference: str, circular: bool = False
) -> list[tuple[int, str, int | None]]:
    """The read's differences from the reference, as HGVS names them: (where, its name, the
    read's least quality in it). Read along the read, so one across a circular reference's
    origin is one. A run of changed places (bases that differ, and deletions) with what's
    inserted between them is one change, named from the reference's bases there and the
    read's: a substitution (`g.151C>T`) for one base, a deletion, an insertion or
    duplication, an inversion where the bases are their own reverse complement, and
    otherwise a deletion-insertion (`g.10_11delinsTA`), as HGVS asks for consecutive
    changes. Changes the 3' rule would carry into one another are one change too."""
    size = len(reference)
    first, last = placed.span
    changes: list[Change] = []
    quality_of: list[list[int]] = []
    # The run of changes open: where it starts and stops in the reference, what the read
    # has there instead, and the read's qualities in it.
    start: int | None = None
    stop = 0
    instead: list[str] = []
    qualities: list[int] = []

    def close() -> None:
        nonlocal start
        if start is None:
            return
        changes.append((start, stop, "".join(instead)))
        quality_of.append(list(qualities))
        start = None
        instead.clear()
        qualities.clear()

    for t in range(first, last):
        spot = t % size
        base = placed.at.get(spot, ".")
        changed = base == "-" or call(base, reference[spot]) == "differs"
        added = placed.inserted.get(spot, "") if t > first else ""
        if added:
            # Inserted before this place, so the run starts here if it hasn't.
            if start is None:
                start = stop = t
            instead.append(added)
            if spot in placed.inserted_quality:
                qualities.append(placed.inserted_quality[spot])
        if not changed:
            close()
            continue
        if start is None:
            start = t
        stop = t + 1
        if base != "-":
            instead.append(base)
            if spot in placed.quality_at:
                qualities.append(placed.quality_at[spot])
    close()

    changes, quality_of = merged(reference, changes, quality_of, circular)
    out: list[tuple[int, str, int | None]] = []
    for change, found in zip(changes, quality_of, strict=True):
        least = min(found) if found and change[2] else None
        out.append((change[0] % size, named(reference, change, circular), least))
    return out


def resolved(placed: Placed, reference: str) -> list[tuple[int, str]]:
    """Where the reference is ambiguous and the read has one of the bases it allows: (where,
    such as R31A)."""
    return [
        (spot, f"{reference[spot]}{spot + 1}{base}")
        for spot, base in sorted(placed.at.items())
        if base != "-" and call(base, reference[spot]) == "resolves"
    ]


def unresolved(placed: Placed, reference: str) -> list[int]:
    """Where the read's base is ambiguous (N, ...) and could be the reference's."""
    return [
        spot
        for spot, base in sorted(placed.at.items())
        if base != "-" and call(base, reference[spot]) == "unresolved"
    ]


def listing(found: dict[str, tuple[int, list[str]]]) -> list[str]:
    """Names, each with the reads that show it, in the reference's order."""
    listed = sorted(found.items(), key=lambda item: (item[1][0], item[0]))
    return [f"{name} ({', '.join(showing)})" for name, (_, showing) in listed]


def ranges(spots: list[int]) -> list[tuple[int, int]]:
    """Runs of consecutive positions (0-based), as 1-based inclusive ranges."""
    out: list[tuple[int, int]] = []
    for spot in sorted(spots):
        # The last range ends at 1-based `spot` when this one follows it.
        if out and spot == out[-1][1]:
            out[-1] = (out[-1][0], spot + 1)
        else:
            out.append((spot + 1, spot + 1))
    return out


def spans(found: list[tuple[int, int]]) -> str:
    shown = [f"{a:,}" if a == b else f"{a:,}–{b:,}" for a, b in found[:10]]
    more = f" and {len(found) - 10} more" if len(found) > 10 else ""
    return ", ".join(shown) + more


def percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


@dataclass
class Left:
    """A read left out, and why: as said of it alone, and as said of several left out for
    the same reason (``reason``), with what's particular to it among them (``detail``)."""

    read: Read
    alone: str
    reason: str
    detail: str = ""


def short(text: str) -> str:
    """``text``, cut to fit in one of a run's messages."""
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def fitted(head: str, items: list[str], tail: str = ".", sep: str = "; ") -> str:
    """``head``, then as many of ``items`` as fit in one message (and at most MAX_LISTED),
    then how many more there are, then ``tail``."""
    reserve = len(f"{sep}and {len(items):,} more")
    room = MAX_TEXT - len(head) - len(tail) - reserve
    shown: list[str] = []
    for item in items[:MAX_LISTED]:
        if len(sep.join([*shown, item])) > room:
            break
        shown.append(item)
    if items and not shown:
        shown = [items[0][: max(room - 1, 0)] + "…"]
    text = head + sep.join(shown)
    if len(shown) < len(items):
        text += f"{sep}and {len(items) - len(shown):,} more"
    return text + tail


def left_out(left: list[Left], out_dir: Path) -> list[str]:
    """What the run says of the reads it left out: a warning for each reason, however many
    reads it left out for it, and each read, with why, in ``left_out.tsv``."""
    if not left:
        return []
    with (out_dir / "left_out.tsv").open("w", newline="", encoding="utf-8") as kept:
        table = csv.writer(kept, delimiter="\t", lineterminator="\n")
        table.writerow(["read", "file", "why"])
        table.writerows([each.read.name, each.read.source, each.alone] for each in left)
    by_reason: dict[str, list[Left]] = {}
    for each in left:
        by_reason.setdefault(each.reason, []).append(each)
    said: list[str] = []
    for reason, those in by_reason.items():
        if len(those) == 1:
            said.append(short(those[0].alone))
            continue
        names = [e.read.name + (f" ({e.detail})" if e.detail else "") for e in those]
        head = f"{len(those):,} reads {reason}, so they're left out: "
        said.append(fitted(head, names, ". left_out.tsv lists each, with why.", ", "))
    return said


def align(run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    (entity,) = run["inputs"]["reference"]
    reference = entity["values"]["bases"].upper().replace("U", "T")
    circular = entity["values"].get("topology") == "circular"
    hrid = entity["hrid"]
    if not reference:
        fail(f"{hrid} has no bases to align to.")
    trim = bool(run["params"].get("trim", True))
    taken = {hrid}
    reads: list[Read] = []
    for file_id in run["params"]["reads"]:
        described = run["files"][file_id]
        reads.extend(read_file(Path(described["path"]), described["name"], taken))
        if len(reads) > MAX_READS:
            fail(f"These files hold more than {MAX_READS:,} reads: align them in parts.")
    if not reads:
        fail("These files hold no reads.")
    messages: list[dict[str, Any]] = []
    left: list[Left] = []

    scorer = aligner()
    placed: list[Placed] = []
    for index, read in enumerate(reads, start=1):
        print(f"::progress {index - 1}/{len(reads)} Aligning {read.name}", flush=True)
        if trim and read.quality:
            start, end = mott(read.quality)
            read.bases, read.quality = read.bases[start:end], read.quality[start:end]
            if not read.bases:
                alone = f"{read.name} has no bases good enough to keep: it's left out."
                left.append(Left(read, alone, "have no bases good enough to keep"))
                continue
        if len(read.bases) > MAX_READ:
            alone = f"{read.name} has {len(read.bases):,} bases, over {MAX_READ:,}: it's left out."
            count = f"{len(read.bases):,} bases"
            left.append(Left(read, alone, f"have over {MAX_READ:,} bases", count))
            continue
        try:
            found = place(read, reference, circular, scorer)
        except ValueError as error:
            left.append(
                Left(read, f"{read.name} is left out: {error}.", "can't be aligned", str(error))
            )
            continue
        if found is None:
            alone = (
                f"{read.name} doesn't align to {hrid}: no stretch of it scores above zero. "
                "It's left out."
            )
            reason = f"don't align to {hrid}"
            left.append(Left(read, alone, reason, "no stretch of it scores above zero"))
            continue
        aligned = found.end - found.start
        if aligned < max(LEAST_BASES, LEAST_SHARE * len(read.bases)):
            best = f"{aligned} of its {len(read.bases)} bases do at best"
            alone = f"{read.name} doesn't align to {hrid}: {best}. It's left out."
            left.append(Left(read, alone, f"don't align to {hrid}", best))
            continue
        # A read that lies as well somewhere else says nothing of the place it was put
        # on: a difference in it may be at either, and the coverage belongs to neither.
        if found.places > 1 or found.either_strand:
            where = (
                "equally well on both strands"
                if found.either_strand
                else f"equally well in {found.places} places on {hrid}"
                + (" or more" if found.places >= MOST_PLACEMENTS else "")
            )
            alone = (
                f"{read.name} lies {where}, so nothing it shows belongs to one of them "
                "rather than another: it's left out."
            )
            left.append(Left(read, alone, f"lie in more than one place on {hrid}", where))
            continue
        # A read longer than a circular reference passes a place more than once, and the
        # alignment holds one base per place: the two passes can't both be shown, and a
        # difference on one of them would be lost or claimed twice.
        span = found.span[1] - found.span[0]
        if span > len(reference):
            over = f"{span:,} bases of its {len(reference):,}"
            alone = (
                f"{read.name} reads round {hrid} more than once ({over}), which an "
                "alignment can't show place by place: it's left out. Align it in parts."
            )
            left.append(Left(read, alone, f"read round {hrid} more than once", over))
            continue
        placed.append(found)
    if not placed:
        fail(f"None of the reads align to {hrid}.")
    messages.extend({"level": "warning", "text": text} for text in left_out(left, out_dir))

    size = len(reference)
    most = [0] * size
    for each in placed:
        for spot, bases in each.inserted.items():
            most[spot] = max(most[spot], len(bases))
    rows = [
        (
            f"{hrid} {entity['values'].get('name') or ''}".strip(),
            "".join("-" * most[k] + reference[k] for k in range(size)),
        )
    ]
    for each in placed:
        row = "".join(
            each.inserted.get(k, "").ljust(most[k], "-" if lies_across(each, k) else ".")
            + each.at.get(k, ".")
            for k in range(size)
        )
        read = each.read
        kept = f"bases {each.start + 1}–{each.end} of {len(read.bases)}"
        trimmed = f" (trimmed from {read.length})" if len(read.bases) != read.length else ""
        strand = "reverse complement" if each.reverse else "forward"
        on = f"on {hrid} {spans(ranges(list(each.at)))}"
        rows.append((f"{read.name} {strand}, {kept}{trimmed}, from {read.source}; {on}", row))
    write_alignments(out_dir, rows)

    covered = sorted({spot for each in placed for spot in covered_by(each, reference)})
    found_ranges = ranges(covered)
    missing = sorted(set(range(size)) - set(covered))
    title = entity["values"].get("name") or hrid
    coverage = (
        f"The reads cover {spans(found_ranges)} of {title}'s {size:,} bases "
        f"({percent(len(covered), size)}%)."
    )
    if missing:
        coverage += f" Not covered: {spans(ranges(missing))}."
    messages.insert(0, {"level": "info", "text": short(coverage)})

    seen: dict[str, tuple[int, list[str]]] = {}
    for each in placed:
        for spot, name, quality in differences(each, reference, circular):
            _, shown = seen.setdefault(name, (spot, []))
            shown.append(each.read.name + (f" Q{quality}" if quality is not None else ""))
    named = listing(seen)
    count = len(named)
    if count:
        messages.insert(1, {"level": "info", "text": fitted(f"Differences from {hrid}: ", named)})
    else:
        none = f"No differences from {hrid} where the reads cover it."
        messages.insert(1, {"level": "info", "text": none})
    unsure = [
        (f"{spans(ranges(spots))}", each.read.name)
        for each in placed
        if (spots := unresolved(each, reference))
    ]
    if unsure:
        messages.insert(
            2,
            {
                "level": "info",
                "text": fitted(
                    "Ambiguous bases (such as N) say nothing there, so they don't cover it: ",
                    [f"{where} ({name})" for where, name in unsure],
                ),
            },
        )
    settled: dict[str, tuple[int, list[str]]] = {}
    for each in placed:
        for spot, name in resolved(each, reference):
            settled.setdefault(name, (spot, []))[1].append(each.read.name)
    if settled:
        text = fitted(f"Where {hrid} is ambiguous, the reads have: ", listing(settled))
        messages.insert(2 + bool(unsure), {"level": "info", "text": text})
    messages.append({"level": "info", "text": TWO_FILES})
    print(f"::progress {len(reads)}/{len(reads)} Aligned {len(placed)} of {len(reads)}", flush=True)

    name = (run["params"].get("name") or "").strip() or (
        f"{len(placed)} read{'s' if len(placed) != 1 else ''} on {title}"
    )
    return {
        "creates": [
            {
                "type": "iki/core.Alignment",
                "values": {
                    "name": name[:200],
                    "description": (
                        f"{len(placed)} of {len(reads)} reads aligned to {hrid}, covering "
                        f"{percent(len(covered), size)}% of it, with {count} "
                        f"difference{'s' if count != 1 else ''}."
                    ),
                    "reference": entity["id"],
                    "members": [entity["id"]],
                    "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"},
                    "method": (
                        "Biopython PairwiseAligner, local, on both strands"
                        + (", round the origin" if circular else "")
                        + ": match 2, mismatch -3, a gap of n bases costing 5 + 2(n - 1)"
                        + (f"; reads trimmed by Mott's algorithm, limit {LIMIT}" if trim else "")
                    ),
                },
                "derived_from": [entity["id"]],
            }
        ],
        "metrics": {
            "reads": len(reads),
            "aligned": len(placed),
            "coverage_percent": percent(len(covered), size),
            "differences": count,
        },
        "messages": messages,
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        **align(run, Path(os.environ["IKI_OUT_DIR"])),
    }
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()

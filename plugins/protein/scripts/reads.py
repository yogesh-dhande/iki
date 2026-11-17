"""`align_reads`: aligns sequencing reads to a protein, such as Sanger reads of an
expression construct to the protein it should make, into an `iki/core.Alignment` (ADR 0088).

Reads come from the run's files, as the dna plugin's `align_reads` reads them: AB1
traces, one read each, named by their file, or FASTQ or FASTA files of any number of
reads, named by their records, each file's format read from its first bytes. Reads with
qualities are trimmed of their poor ends by Mott's algorithm, limit 0.05.

Each read is translated in its six frames, with the standard genetic code, and aligned
locally to the protein as tblastn does: BLOSUM62, a gap opening penalty of 11 and an
extension penalty of 1. Its best frame is kept if the alignment's expected value, by
BLASTP's statistics for those scores (lambda 0.267, K 0.041), is under 0.001: a read
that's mostly vector still aligns where it holds the protein's code, and one that
doesn't hold it is left out, saying so. What's left of the read on either side is then
aligned in the other frames of its strand: a part that aligns there means the read's
frame shifted, and the run names where.

The alignment's first row is the protein, headed by its HRID; each read's row follows,
headed by its name, its strand and frame, as its translation lies on the protein.
Residues a read codes for that the protein doesn't have get columns of their own.

The run reports where the reads cover the protein, and each difference from it in the
protein's numbering, as HGVS names them in one-letter codes: substitutions (T315I),
stops (Q45*), deletions (K12del, K12_L14del), insertions (K12_L13insG), duplications
(K12dup) and frameshifts (K12fs), with the reads that show each and their quality there
(the least of its codon's bases). A deletion or insertion in repeated residues is named
at its C-terminal-most place, as HGVS asks.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from Bio import Align, SeqIO
from Bio.Align import substitution_matrices
from Bio.Seq import Seq

#: The most reads a run aligns.
MAX_READS = 1000
#: The longest read, after trimming: Sanger reads are about 1,000 bases.
MAX_READ = 20_000
#: The longest protein.
MAX_LENGTH = 40_000
#: Mott's limit on a base's error probability, as phred's default.
LIMIT = 0.05
#: BLASTP's statistics for BLOSUM62 with gaps costing 11, plus 1 a residue.
LAMBDA, K = 0.267, 0.041
#: The most an alignment's expected value may be for it to count.
MAX_EXPECT = 0.001
#: The most cells one alignment may fill: codons times residues.
MAX_CELLS = 100_000_000
#: The most parts of a read, in different frames, that are aligned.
MAX_PARTS = 4
#: The most differences the run lists.
MAX_LISTED = 100
#: What a read's residue may be where the protein has an ambiguous or unusual one, and be
#: no different: selenocysteine's codon is a stop's, TGA.
SAME = {"B": "DN", "Z": "EQ", "J": "IL", "X": "ACDEFGHIKLMNPQRSTVWY*", "U": "U*"}


def fail(why: str) -> NoReturn:
    print(f"::error {why}", flush=True)
    sys.exit(1)


# Kept as the dna plugin's reads.py has them; a test checks they stay the same.


@dataclass
class Read:
    name: str
    bases: str
    #: Phred qualities, one per base, or None for FASTA.
    quality: list[int] | None
    source: str
    #: How long it was before trimming.
    length: int = 0


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


# The protein's own.


@dataclass
class Part:
    """A stretch of a read, in one frame, as it aligned to the protein."""

    frame: int
    #: The read's bases it covers, 0-based on the strand it's read on, end exclusive.
    start: int
    end: int
    #: The protein's residues it covers, 0-based, end exclusive.
    first: int
    last: int


@dataclass
class Placed:
    """A read's translation as it lies on the protein."""

    read: Read
    reverse: bool
    parts: list[Part] = field(default_factory=list)
    #: Protein position (0-based) → the read's residue there, or "-" where it has none.
    at: dict[int, str] = field(default_factory=dict)
    #: Protein position → the read's quality there (its codon's least), where it has one.
    quality_at: dict[int, int] = field(default_factory=dict)
    #: Protein position → residues the read codes for before it that the protein doesn't.
    inserted: dict[int, str] = field(default_factory=dict)
    inserted_quality: dict[int, int] = field(default_factory=dict)


@dataclass
class Found:
    """A part's alignment: its score, the part, and Biopython's alignment of the protein's
    residues from `offset` on to the read's translation from base `start` on."""

    score: float
    part: Part
    alignment: Any
    start: int
    offset: int


def aligner() -> Align.PairwiseAligner:
    # BLAST's gap of n costs 11 + n; Biopython's opening score is its first residue's.
    return Align.PairwiseAligner(
        mode="local",
        substitution_matrix=substitution_matrices.load("BLOSUM62"),
        open_gap_score=-12,
        extend_gap_score=-1,
    )


def translated(bases: str) -> str:
    """The bases' translation by the standard code, whole codons only, `*` for a stop and
    `X` for a codon whose bases are too unsure to tell."""
    whole = bases[: len(bases) - len(bases) % 3]
    protein = str(Seq(whole).translate()) if whole else ""
    # Ambiguous codons can translate to B, Z or J; only X is in every matrix's sense "any".
    return re.sub(r"[BZJ]", "X", protein)


def expect(score: float, protein: int, residues: int) -> float:
    return K * protein * residues * math.exp(-LAMBDA * score)


def best_part(
    query: str,
    frames: list[int],
    within: tuple[int, int],
    protein: str,
    span: tuple[int, int],
    scorer: Align.PairwiseAligner,
) -> Found | None:
    """The best alignment, in any of `frames`, of the query's bases `within` to the
    protein's residues in `span`, if its expected value is low enough."""
    found: Found | None = None
    # BLOSUM62 has no U, O or J: they're scored as any residue.
    target = re.sub("[UOJ]", "X", protein[span[0] : span[1]])
    for frame in frames:
        # The whole codons in `within` that are in this frame.
        start = within[0] + (frame - within[0]) % 3
        coded = translated(query[start : within[1]])
        if len(coded) < 2 or not target:
            continue
        if len(coded) * len(target) > MAX_CELLS:
            raise ValueError(
                f"it's too long to align here: {len(coded):,} codons by {len(target):,} "
                f"residues is more than {MAX_CELLS:,} cells"
            )
        score = scorer.score(target, coded)
        if expect(score, len(protein), len(coded)) > MAX_EXPECT:
            continue
        if found is None or score > found.score:
            best = scorer.align(target, coded)[0]
            coordinates = best.coordinates
            part = Part(
                frame,
                start + 3 * int(coordinates[1][0]),
                start + 3 * int(coordinates[1][-1]),
                span[0] + int(coordinates[0][0]),
                span[0] + int(coordinates[0][-1]),
            )
            found = Found(score, part, best, start, span[0])
    return found


def lay(placed: Placed, query: str, quality: list[int] | None, found: Found) -> None:
    """Fills in where a part's residues lie on the protein."""
    coded = translated(query[found.start :])

    def codon_quality(index: int) -> int | None:
        if not quality:
            return None
        at = found.start + 3 * index
        return min(quality[at : at + 3])

    coordinates = found.alignment.coordinates
    offset = found.offset
    for (t0, q0), (t1, q1) in zip(coordinates.T[:-1], coordinates.T[1:], strict=True):
        t0, q0, t1, q1 = int(t0) + offset, int(q0), int(t1) + offset, int(q1)
        if t1 > t0 and q1 > q0:
            for step in range(t1 - t0):
                placed.at[t0 + step] = coded[q0 + step]
                got = codon_quality(q0 + step)
                if got is not None:
                    placed.quality_at[t0 + step] = got
        elif t1 > t0:
            for spot in range(t0, t1):
                placed.at[spot] = "-"
        else:
            placed.inserted[t0] = placed.inserted.get(t0, "") + coded[q0:q1]
            got = [q for q in (codon_quality(i) for i in range(q0, q1)) if q is not None]
            if got:
                placed.inserted_quality[t0] = min(got)


def place(read: Read, protein: str, scorer: Align.PairwiseAligner) -> Placed | None:
    """Where the read's translation lies on the protein: its best frame on either strand,
    then what's left either side in its strand's other frames, where its frame shifted."""
    everything = (0, len(protein))
    chosen: tuple[bool, str, Found] | None = None
    for reverse in (False, True):
        query = str(Seq(read.bases).reverse_complement()) if reverse else read.bases
        found = best_part(query, [0, 1, 2], (0, len(query)), protein, everything, scorer)
        if found and (chosen is None or found.score > chosen[2].score):
            chosen = (reverse, query, found)
    if chosen is None:
        return None
    reverse, query, found = chosen
    quality = (read.quality[::-1] if reverse else read.quality) if read.quality else None
    placed = Placed(read, reverse, [found.part])
    lay(placed, query, quality, found)
    left = right = True
    while (left or right) and len(placed.parts) < MAX_PARTS:
        if left:
            first = placed.parts[0]
            frames = [f for f in (0, 1, 2) if f != first.frame]
            more = best_part(query, frames, (0, first.start), protein, (0, first.first), scorer)
            if more:
                placed.parts.insert(0, more.part)
                lay(placed, query, quality, more)
            left = more is not None
        if right and len(placed.parts) < MAX_PARTS:
            last = placed.parts[-1]
            frames = [f for f in (0, 1, 2) if f != last.frame]
            more = best_part(
                query, frames, (last.end, len(query)), protein, (last.last, len(protein)), scorer
            )
            if more:
                placed.parts.append(more.part)
                lay(placed, query, quality, more)
            right = more is not None
    return placed


def deletion(protein: str, first: int, last: int) -> str:
    """A deletion's name, moved as far C-terminal as the residues allow."""
    size = len(protein)
    while last + 1 < size and protein[last + 1] == protein[first]:
        first, last = first + 1, last + 1
    if first == last:
        return f"{protein[first]}{first + 1}del"
    return f"{protein[first]}{first + 1}_{protein[last]}{last + 1}del"


def insertion(protein: str, spot: int, residues: str) -> str:
    """An insertion's name, `residues` inserted before 0-based `spot`, moved as far
    C-terminal as the residues allow; one that repeats the residues before it is a
    duplication."""
    # A local alignment has residues either side of an insertion, so it stays between two.
    while spot < len(protein) - 1 and residues[0] == protein[spot]:
        residues, spot = residues[1:] + protein[spot], spot + 1
    if spot >= len(residues) and protein[spot - len(residues) : spot] == residues:
        if len(residues) == 1:
            return f"{protein[spot - 1]}{spot}dup"
        a = spot - len(residues)
        return f"{protein[a]}{a + 1}_{protein[spot - 1]}{spot}dup"
    return f"{protein[spot - 1]}{spot}_{protein[spot]}{spot + 1}ins{residues}"


def differences(placed: Placed, protein: str) -> list[tuple[int, str, int | None]]:
    """The read's differences from the protein: (where, as HGVS names it, its quality
    there)."""
    out: list[tuple[int, str, int | None]] = []
    deleted: list[int] = []

    def close_deletion() -> None:
        if deleted:
            out.append((deleted[0], deletion(protein, deleted[0], deleted[-1]), None))
            deleted.clear()

    for spot in sorted(placed.at):
        residue = placed.at[spot]
        if residue == "-":
            if deleted and spot != deleted[-1] + 1:
                close_deletion()
            deleted.append(spot)
            continue
        close_deletion()
        if residue != "X" and residue not in SAME.get(protein[spot], protein[spot]):
            out.append((spot, f"{protein[spot]}{spot + 1}{residue}", placed.quality_at.get(spot)))
    close_deletion()
    for spot, residues in placed.inserted.items():
        out.append((spot, insertion(protein, spot, residues), placed.inserted_quality.get(spot)))
    # Where the frame shifted: the first residue the next part codes in its new frame.
    for before, after in zip(placed.parts[:-1], placed.parts[1:], strict=True):
        if before.frame != after.frame:
            spot = min(before.last, len(protein) - 1)
            out.append((spot, f"{protein[spot]}{spot + 1}fs", None))
    return out


def align(run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    (entity,) = run["inputs"]["reference"]
    protein = entity["values"]["sequence"].upper().removesuffix("*")
    hrid = entity["hrid"]
    if not protein:
        fail(f"{hrid} has no residues to align to.")
    if len(protein) > MAX_LENGTH:
        fail(f"{hrid} is too long to align here: {len(protein):,} residues is over {MAX_LENGTH:,}.")
    if "*" in protein:
        fail(f"{hrid} has a stop inside it, at residue {protein.index('*') + 1}.")
    odd = re.search(r"[^ACDEFGHIKLMNPQRSTVWYUOBZJX]", protein)
    if odd:
        fail(f"{hrid} has {odd.group()!r}, which isn't a residue.")
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

    def warn(text: str) -> None:
        messages.append({"level": "warning", "text": text})

    scorer = aligner()
    placed: list[Placed] = []
    for index, read in enumerate(reads, start=1):
        print(f"::progress {index - 1}/{len(reads)} Aligning {read.name}", flush=True)
        if trim and read.quality:
            start, end = mott(read.quality)
            read.bases, read.quality = read.bases[start:end], read.quality[start:end]
            if not read.bases:
                warn(f"{read.name} has no bases good enough to keep: it's left out.")
                continue
        if len(read.bases) > MAX_READ:
            warn(f"{read.name} has {len(read.bases):,} bases, over {MAX_READ:,}: it's left out.")
            continue
        try:
            found = place(read, protein, scorer)
        except ValueError as error:
            warn(f"{read.name} is left out: {error}.")
            continue
        if found is None:
            warn(f"{read.name} doesn't code for any of {hrid} in any frame. It's left out.")
            continue
        placed.append(found)
    if not placed:
        fail(f"None of the reads code for any of {hrid}.")

    size = len(protein)
    most = [0] * size
    for each in placed:
        for spot, residues in each.inserted.items():
            most[spot] = max(most[spot], len(residues))
    rows = [
        (
            f"{hrid} {entity['values'].get('name') or ''}".strip(),
            "".join("-" * most[k] + protein[k] for k in range(size)),
        )
    ]
    for each in placed:
        row = "".join(
            each.inserted.get(k, "").ljust(most[k], "-") + each.at.get(k, "-") for k in range(size)
        )
        read = each.read
        frames = ", ".join(
            f"frame {part.frame + 1}, bases {part.start + 1}–{part.end}" for part in each.parts
        )
        trimmed = f" (trimmed from {read.length})" if len(read.bases) != read.length else ""
        strand = "reverse complement" if each.reverse else "forward"
        rows.append(
            (
                f"{read.name} {strand} {frames} of {len(read.bases)}{trimmed}, from {read.source}",
                row,
            )
        )
    (out_dir / "alignment.fasta").write_text(
        "".join(
            f">{header}\n" + "".join(f"{row[i : i + 60]}\n" for i in range(0, len(row), 60))
            for header, row in rows
        ),
        encoding="utf-8",
    )

    covered = sorted({spot for each in placed for spot in each.at})
    missing = sorted(set(range(size)) - set(covered))
    title = entity["values"].get("name") or hrid
    coverage = (
        f"The reads cover {spans(ranges(covered))} of {title}'s {size:,} residues "
        f"({percent(len(covered), size)}%)."
    )
    if missing:
        coverage += f" Not covered: {spans(ranges(missing))}."
    messages.insert(0, {"level": "info", "text": coverage})

    seen: dict[str, tuple[int, list[str]]] = {}
    for each in placed:
        for spot, name, quality in differences(each, protein):
            _, shown = seen.setdefault(name, (spot, []))
            shown.append(each.read.name + (f" Q{quality}" if quality is not None else ""))
    listed = sorted(seen.items(), key=lambda item: (item[1][0], item[0]))
    if listed:
        text = "; ".join(
            f"{name} ({', '.join(showing)})" for name, (_, showing) in listed[:MAX_LISTED]
        )
        if len(listed) > MAX_LISTED:
            text += f"; and {len(listed) - MAX_LISTED} more"
        messages.insert(1, {"level": "info", "text": f"Differences from {hrid}: {text}."})
    else:
        none = f"No differences from {hrid} where the reads cover it."
        messages.insert(1, {"level": "info", "text": none})
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
                        f"{len(placed)} of {len(reads)} reads' translations aligned to {hrid}, "
                        f"covering {percent(len(covered), size)}% of it, with {len(listed)} "
                        f"difference{'s' if len(listed) != 1 else ''}."
                    ),
                    "reference": entity["id"],
                    "members": [entity["id"]],
                    "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-aa-fasta"},
                    "method": (
                        "Biopython PairwiseAligner, local, in six frames as tblastn: BLOSUM62, "
                        f"gap opening 11, extension 1, expected value under {MAX_EXPECT}"
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
            "differences": len(listed),
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

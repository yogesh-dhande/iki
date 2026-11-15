"""`align_reads`: aligns sequencing reads to a reference, such as Sanger reads of a
construct to the plasmid it should be, into a `core.Alignment` (ADR 0088).

Reads come from the run's files: AB1 traces, one read each, named by their file, or FASTQ
or FASTA files of any number of reads, named by their records. Each file's format is
read from its first bytes, so a file's extension or media type doesn't matter.

Each read with qualities (AB1 and FASTQ) is first trimmed of its poor ends by Mott's
algorithm, as phred and Biopython do: the stretch where the sum of (limit - error
probability) is greatest, with a limit of 0.05. Then it's aligned locally to the
reference on both strands, and the better is kept, with BLASTN's scores (match 2,
mismatch -3, gap opening 5, extension 2) and N matching anything at no score. A circular
reference is aligned as if it went round again, so a read across its origin aligns in
one piece. A read aligns if at least half its trimmed bases (and 20) do; others are left
out and the run says so.

The alignment's first row is the reference, headed by its HRID; each read's row follows,
headed by its name, as it lies on the reference (its reverse complement if it read the
other strand). Bases a read has that the reference doesn't get columns of their own.

The run reports where the reads cover the reference, and each difference from it, in
the reference's numbering: substitutions (C1234T), deletions (1240delA, 1240_1242del)
and insertions (1250_1251insG), with the reads that show it and their quality there. As
HGVS asks, a deletion or insertion in repeated bases is named at its 3'-most place, and
an insertion that repeats the bases before it is a duplication (1250dupG). The rows show
them where the aligner put them.
"""

from __future__ import annotations

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


def place(read: Read, reference: str, circular: bool, scorer: Align.PairwiseAligner) -> Placed:
    """Where the read lies on the reference, on its better strand."""
    size = len(reference)
    target = reference + reference[: len(read.bases) - 1] if circular else reference
    if len(target) * len(read.bases) > MAX_CELLS:
        raise ValueError(
            f"it's too long to align here: {len(read.bases):,} bases by {len(target):,} "
            f"is more than {MAX_CELLS:,} cells"
        )
    forward = read.bases
    backward = str(Seq(read.bases).reverse_complement())
    reverse = scorer.score(target, backward) > scorer.score(target, forward)
    query = backward if reverse else forward
    quality = (read.quality[::-1] if reverse else read.quality) if read.quality else None
    best = scorer.align(target, query)[0]
    coordinates = best.coordinates
    placed = Placed(read, reverse, int(coordinates[1][0]), int(coordinates[1][-1]))
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


def deletion(reference: str, first: int, last: int) -> str:
    """A deletion's name, moved as far 3' as the bases allow, as HGVS names them."""
    size = len(reference)
    while last + 1 < size and reference[last + 1] == reference[first]:
        first, last = first + 1, last + 1
    if first == last:
        return f"{first + 1}del{reference[first]}"
    return f"{first + 1}_{last + 1}del"


def insertion(reference: str, spot: int, bases: str) -> str:
    """An insertion's name, `bases` inserted before 0-based `spot`, moved as far 3' as the
    bases allow; one that repeats the bases before it is a duplication, as HGVS names
    them."""
    size = len(reference)
    while spot < size and bases[0] == reference[spot]:
        bases, spot = bases[1:] + reference[spot], spot + 1
    if spot >= len(bases) and reference[spot - len(bases) : spot] == bases:
        if len(bases) == 1:
            return f"{spot}dup{bases}"
        return f"{spot - len(bases) + 1}_{spot}dup"
    before = spot if spot else size
    return f"{before}_{spot % size + 1}ins{bases}"


def differences(placed: Placed, reference: str) -> list[tuple[int, str, int | None]]:
    """The read's differences from the reference: (where, as HGVS names it, its quality
    there)."""
    out: list[tuple[int, str, int | None]] = []
    deleted: list[int] = []

    def close_deletion() -> None:
        if deleted:
            out.append((deleted[0], deletion(reference, deleted[0], deleted[-1]), None))
            deleted.clear()

    for spot in sorted(placed.at):
        base = placed.at[spot]
        if base == "-":
            if deleted and spot != deleted[-1] + 1:
                close_deletion()
            deleted.append(spot)
            continue
        close_deletion()
        if base != reference[spot] and base in "ACGT":
            out.append((spot, f"{reference[spot]}{spot + 1}{base}", placed.quality_at.get(spot)))
    close_deletion()
    for spot, bases in placed.inserted.items():
        out.append((spot, insertion(reference, spot, bases), placed.inserted_quality.get(spot)))
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
            found = place(read, reference, circular, scorer)
        except ValueError as error:
            warn(f"{read.name} is left out: {error}.")
            continue
        aligned = found.end - found.start
        if aligned < max(LEAST_BASES, LEAST_SHARE * len(read.bases)):
            warn(
                f"{read.name} doesn't align to {hrid}: {aligned} of its {len(read.bases)} "
                "bases do at best. It's left out."
            )
            continue
        placed.append(found)
    if not placed:
        fail(f"None of the reads align to {hrid}.")

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
            each.inserted.get(k, "").ljust(most[k], "-") + each.at.get(k, "-") for k in range(size)
        )
        read = each.read
        kept = f"bases {each.start + 1}–{each.end} of {len(read.bases)}"
        trimmed = f" (trimmed from {read.length})" if len(read.bases) != read.length else ""
        strand = "reverse complement" if each.reverse else "forward"
        rows.append((f"{read.name} {strand}, {kept}{trimmed}, from {read.source}", row))
    (out_dir / "alignment.fasta").write_text(
        "".join(
            f">{header}\n" + "".join(f"{row[i : i + 60]}\n" for i in range(0, len(row), 60))
            for header, row in rows
        ),
        encoding="utf-8",
    )

    covered = sorted({spot for each in placed for spot in each.at})
    found_ranges = ranges(covered)
    missing = sorted(set(range(size)) - set(covered))
    title = entity["values"].get("name") or hrid
    coverage = (
        f"The reads cover {spans(found_ranges)} of {title}'s {size:,} bases "
        f"({percent(len(covered), size)}%)."
    )
    if missing:
        coverage += f" Not covered: {spans(ranges(missing))}."
    messages.insert(0, {"level": "info", "text": coverage})

    seen: dict[str, tuple[int, list[str]]] = {}
    for each in placed:
        for spot, name, quality in differences(each, reference):
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
                "type": "core.Alignment",
                "values": {
                    "name": name[:200],
                    "description": (
                        f"{len(placed)} of {len(reads)} reads aligned to {hrid}, covering "
                        f"{percent(len(covered), size)}% of it, with {len(listed)} "
                        f"difference{'s' if len(listed) != 1 else ''}."
                    ),
                    "reference": entity["id"],
                    "members": [entity["id"]],
                    "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"},
                    "method": (
                        "Biopython PairwiseAligner, local, on both strands"
                        + (", round the origin" if circular else "")
                        + ": match 2, mismatch -3, gap opening 5, extension 2"
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

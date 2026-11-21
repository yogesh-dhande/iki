"""`align_pair`: aligns two DNA sequences as EMBOSS needle (global) or water (local) does,
with Biopython's PairwiseAligner.

Scores come from EDNAFULL, EMBOSS's nucleotide matrix (NCBI's NUC.4.4): +5 for a match,
-4 for a mismatch, and between, for IUPAC's ambiguity codes, what the bases they stand
for share. A gap of n bases costs the opening penalty plus n - 1 extension penalties, as
in EMBOSS. In a global alignment, gaps at either end are free, as in needle's defaults.

A double-stranded sequence is either of its strands, so the query can be aligned as
given, as its reverse complement (needle's -sreverse1), or both ways, keeping the better.
A row that's the query's other strand is headed HRID/end-start, its bases numbered down
from end to start along the query, as Rfam and Pfam write a hit on the reverse strand,
and its EMBOSS lines number down as needle numbers a reversed sequence.

It keeps the alignment as artifacts (ADR 0068): `alignment.txt`, in EMBOSS's pair
format, and `alignment.fasta`, aligned, gaps as `-`, each record headed by its sequence's
HRID, or HRID/start-end when it's part of the sequence (Jalview's way). That file is
also an `iki/core.Alignment` it creates (ADR 0088), of the two sequences, the target its
reference, so Iki's alignment viewer shows it and lineage leads back to both. Its
metrics are the score, the alignment's length, and its identity, similarity and gaps as
percentages of that length, as EMBOSS reports them: identical columns have the same
base, similar ones a pair the matrix scores above zero (A with R, say), and gapped ones
a gap. The percentages are kept unrounded, so a workflow's threshold compares the true
share; pages and the EMBOSS file round them.

A local alignment with nothing scoring above zero is none: the run says so, creates no
alignment, and its metrics are zero.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

from Bio import Align
from Bio.Align import substitution_matrices

#: The most cells an alignment may fill: bases of one times the other's. Two plasmids of
#: 14 kb fill some 200 million, in about 450 MB.
MAX_CELLS = 200_000_000
#: Bases a line of the alignment shows.
WIDTH = 50
#: EMBOSS's EDNAFULL, as NCBI publishes it.
MATRIX = "NUC.4.4"
#: The bases and IUPAC codes the matrix scores.
BASES = re.compile("[^ACGTRYSWKMBDHVN]")
COMPLEMENT = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")


def fail(why: str) -> NoReturn:
    print(f"::error {why}", flush=True)
    sys.exit(1)


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def aligner(mode: str, gap_open: float, gap_extend: float) -> Align.PairwiseAligner:
    found = Align.PairwiseAligner(
        mode=mode,
        substitution_matrix=substitution_matrices.load(MATRIX),
        open_gap_score=-gap_open,
        extend_gap_score=-gap_extend,
    )
    if mode == "global":
        found.end_gap_score = 0
    return found


def marks(matrix: Any, top: str, bottom: str) -> tuple[str, int, int]:  # noqa: ANN401 - Array
    """EMBOSS's line between the two: `|` identical, `:` similar (scoring above zero, as A
    with R does), `.` other pairs, and nothing at a gap; and how many columns are
    identical, and similar."""
    out = []
    identical = similar = 0
    for a, b in zip(top, bottom, strict=True):
        if "-" in (a, b):
            out.append(" ")
            continue
        positive = float(matrix[a][b]) > 0
        identical += a == b
        similar += positive
        out.append("|" if a == b else ":" if positive else ".")
    return "".join(out), identical, similar


def emboss(
    names: tuple[str, str],
    rows: tuple[str, str],
    starts: tuple[int, int],
    steps: tuple[int, int],
    line: str,
    header: list[str],
) -> str:
    """EMBOSS's pair format: a header of what was done and found, then the alignment, each
    line numbered by the base it starts and ends at, counting down on a reversed row."""
    width = max(len(n) for n in names)
    out = ["#" * 40, *(f"# {h}" if h else "#" for h in header), "#" * 40, ""]
    at = list(starts)
    for offset in range(0, len(rows[0]), WIDTH):
        chunk = [r[offset : offset + WIDTH] for r in rows]
        counted = [sum(c != "-" for c in chunk[i]) for i in (0, 1)]
        ends = [at[i] + steps[i] * (counted[i] - 1) for i in (0, 1)]
        shown = [(at[i] if counted[i] else ends[i]) for i in (0, 1)]
        out.append(f"{names[0]:<{width}} {shown[0]:>6} {chunk[0]} {ends[0]:>6}")
        out.append(f"{'':<{width}} {'':>6} {line[offset : offset + WIDTH]}")
        out.append(f"{names[1]:<{width}} {shown[1]:>6} {chunk[1]} {ends[1]:>6}")
        out.append("")
        at = [ends[0] + steps[0], ends[1] + steps[1]]
    return "\n".join(out) + "\n"


def percent(part: int, whole: int) -> float:
    """The share, unrounded: rounding is for showing it."""
    return 100 * part / whole if whole else 0.0


def shown(value: float) -> str:
    return f"{value:.1f}"


def rows_of(a: str, b: str, coordinates: Any) -> tuple[str, str]:  # noqa: ANN401 - an array
    """The two aligned rows, gaps as `-`, from the alignment's coordinates."""
    top, bottom = [], []
    for (a0, b0), (a1, b1) in zip(coordinates.T[:-1], coordinates.T[1:], strict=True):
        a0, b0, a1, b1 = int(a0), int(b0), int(a1), int(b1)
        width = max(a1 - a0, b1 - b0)
        top.append(a[a0:a1] if a1 > a0 else "-" * width)
        bottom.append(b[b0:b1] if b1 > b0 else "-" * width)
    return "".join(top), "".join(bottom)


def bases_of(entity: dict[str, Any]) -> str:
    bases = str(entity["values"].get("bases") or "").upper()
    if not bases:
        fail(f"{entity['hrid']} has no bases to align.")
    odd = BASES.search(bases)
    if odd:
        fail(f"{entity['hrid']} has {odd.group()!r} at base {odd.start() + 1}, not an IUPAC base.")
    return bases


def best_of(found: Align.PairwiseAligner, a: str, b: str) -> Any:  # noqa: ANN401 - Alignment
    try:
        return next(iter(found.align(a, b)), None)
    except ValueError as error:
        fail(f"Couldn't align them: {error}.")


def align(run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    (query,) = run["inputs"]["query"]
    (target,) = run["inputs"]["target"]
    params = run["params"]
    mode, strand = params["mode"], params.get("strand", "either")
    gap_open, gap_extend = float(params["gap_open"]), float(params["gap_extend"])
    given, b = bases_of(query), bases_of(target)
    if len(given) * len(b) > MAX_CELLS:
        fail(
            f"{query['hrid']} and {target['hrid']} are too long to align here: "
            f"{len(given):,} by {len(b):,} bases is more than {MAX_CELLS:,} cells."
        )
    found = aligner(mode, gap_open, gap_extend)
    # The query as given first, so a tie keeps it as given.
    tries = {"forward": [False], "reverse": [True], "either": [False, True]}[strand]
    candidates = []
    for reverse in tries:
        a = reverse_complement(given) if reverse else given
        candidates.append((best_of(found, a, b), reverse, a))
    best, reverse, a = max(
        candidates, key=lambda c: c[0].score if c[0] is not None else float("-inf")
    )
    if best is None or (mode == "local" and best.score <= 0):
        return {
            "creates": [],
            "metrics": {
                "score": 0.0,
                "length": 0,
                "identity_percent": 0.0,
                "similarity_percent": 0.0,
                "gaps_percent": 0.0,
            },
            "messages": [
                {
                    "level": "warning",
                    "text": f"{query['hrid']} and {target['hrid']} have no local alignment: no "
                    "part of one scores above zero against the other.",
                }
            ],
        }
    coordinates = best.coordinates
    top, bottom = rows_of(a, b, coordinates)
    line, identical, similar = marks(found.substitution_matrix, top, bottom)
    length = len(top)
    gaps = sum("-" in pair for pair in zip(top, bottom, strict=True))
    # Where each row starts and ends, counting from 1 along the row's own bases, then
    # along the query itself: its reverse complement's base i is its base n - i + 1.
    first = (int(coordinates[0][0]) + 1, int(coordinates[1][0]) + 1)
    last = (int(coordinates[0][-1]), int(coordinates[1][-1]))
    n = len(given)
    spans = [
        (n - first[0] + 1, n - last[0] + 1) if reverse else (first[0], last[0]),
        (first[1], last[1]),
    ]
    names = (query["hrid"], target["hrid"])
    strand_said = "reverse complement" if reverse else "as given"
    header = [
        "Aligned by Iki's dna.align_pair, with Biopython's PairwiseAligner",
        f"Run: {run['run']['hrid']}",
        f"Mode: {mode} ({'needle' if mode == 'global' else 'water'})",
        "Matrix: EDNAFULL",
        f"Gap_penalty: {gap_open}",
        f"Extend_penalty: {gap_extend}",
        f"Query_strand: {strand_said}",
        "",
        f"1: {names[0]} {query['values'].get('name') or ''}".rstrip(),
        f"2: {names[1]} {target['values'].get('name') or ''}".rstrip(),
        "",
        f"Length: {length}",
        f"Identity:   {identical}/{length} ({shown(percent(identical, length))}%)",
        f"Similarity: {similar}/{length} ({shown(percent(similar, length))}%)",
        f"Gaps:       {gaps}/{length} ({shown(percent(gaps, length))}%)",
        f"Score: {best.score:.1f}",
    ]
    (out_dir / "alignment.txt").write_text(
        emboss(
            names,
            (top, bottom),
            (spans[0][0], spans[1][0]),
            (-1 if reverse else 1, 1),
            line,
            header,
        ),
        encoding="utf-8",
    )
    # A row that's part of its sequence, as a local alignment's are, or its other strand,
    # says which part as Jalview, Pfam and Rfam write it: HRID/start-end, or end-start on
    # the reverse strand, so its bases are numbered as its own.
    headed = [
        name if (start, end) == (1, len(whole)) and not backwards else f"{name}/{start}-{end}"
        for name, (start, end), whole, backwards in zip(
            names, spans, (given, b), (reverse, False), strict=True
        )
    ]
    (out_dir / "alignment.fasta").write_text(
        "".join(
            f">{h}\n" + "".join(f"{row[i : i + 60]}\n" for i in range(0, len(row), 60))
            for h, row in zip(headed, (top, bottom), strict=True)
        ),
        encoding="utf-8",
    )
    print(
        f"{names[0]} ({strand_said}) vs {names[1]}: score {best.score:.1f}, "
        f"{shown(percent(identical, length))}% identical"
    )
    titles = [e["values"].get("name") or e["hrid"] for e in (query, target)]
    return {
        "creates": [
            {
                "type": "iki/core.Alignment",
                "values": {
                    "name": f"{titles[0]} and {titles[1]}"[:200],
                    "description": (
                        f"{shown(percent(identical, length))}% identical and "
                        f"{shown(percent(similar, length))}% similar over {length} columns, "
                        f"scoring {best.score:.1f}"
                        + (f", {titles[0]}'s reverse complement" if reverse else "")
                        + "."
                    ),
                    "reference": target["id"],
                    "members": [query["id"], target["id"]],
                    "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"},
                    "method": (
                        f"Biopython PairwiseAligner, {mode} as EMBOSS "
                        f"{'needle' if mode == 'global' else 'water'}: EDNAFULL, gap opening "
                        f"{gap_open:g}, extension {gap_extend:g}"
                        + (", the query's reverse complement" if reverse else "")
                    ),
                },
                "derived_from": [query["id"], target["id"]],
            }
        ],
        "metrics": {
            "score": round(best.score, 1),
            "length": length,
            "identity_percent": percent(identical, length),
            "similarity_percent": percent(similar, length),
            "gaps_percent": percent(gaps, length),
        },
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

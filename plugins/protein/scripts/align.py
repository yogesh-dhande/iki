"""`align_pair`: aligns two proteins' sequences as EMBOSS needle (global) or water (local)
does, with Biopython's PairwiseAligner.

Scores come from the substitution matrix chosen; a gap of n residues costs the opening
penalty plus n - 1 extension penalties, as in EMBOSS. In a global alignment, gaps at
either end are free, as in needle's defaults.

It keeps the alignment as artifacts (ADR 0068): `alignment.txt`, in EMBOSS's pair
format, and `alignment.fasta`, aligned, gaps as `-`. Its metrics are the score, the
alignment's length, and its identity, similarity and gaps as percentages of that length,
as EMBOSS reports them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

from Bio import Align
from Bio.Align import substitution_matrices

#: The most cells an alignment may fill: residues of one times the other's.
MAX_CELLS = 100_000_000
#: Residues a line of the alignment shows.
WIDTH = 50


def fail(why: str) -> NoReturn:
    print(f"::error {why}", flush=True)
    sys.exit(1)


def aligner(mode: str, matrix: str, gap_open: float, gap_extend: float) -> Align.PairwiseAligner:
    found = Align.PairwiseAligner(
        mode=mode,
        substitution_matrix=substitution_matrices.load(matrix),
        open_gap_score=-gap_open,
        extend_gap_score=-gap_extend,
    )
    if mode == "global":
        found.end_gap_score = 0
    return found


def marks(matrix: Any, top: str, bottom: str) -> str:  # noqa: ANN401 - Biopython's Array
    """EMBOSS's line between the two: `|` identical, `:` similar (scoring above zero), `.`
    other pairs, and nothing at a gap."""
    out = []
    for a, b in zip(top, bottom, strict=True):
        if "-" in (a, b):
            out.append(" ")
        elif a == b:
            out.append("|")
        else:
            out.append(":" if matrix[a][b] > 0 else ".")
    return "".join(out)


def emboss(
    names: tuple[str, str],
    rows: tuple[str, str],
    starts: tuple[int, int],
    line: str,
    header: list[str],
) -> str:
    """EMBOSS's pair format: a header of what was done and found, then the alignment, each
    line numbered by the residue it starts and ends at."""
    width = max(len(n) for n in names)
    out = ["#" * 40, *(f"# {h}" if h else "#" for h in header), "#" * 40, ""]
    at = list(starts)
    for offset in range(0, len(rows[0]), WIDTH):
        chunk = [r[offset : offset + WIDTH] for r in rows]
        ends = [at[i] + sum(c != "-" for c in chunk[i]) - 1 for i in (0, 1)]
        shown = [(at[i] if any(c != "-" for c in chunk[i]) else ends[i]) for i in (0, 1)]
        out.append(f"{names[0]:<{width}} {shown[0]:>6} {chunk[0]} {ends[0]:>6}")
        out.append(f"{'':<{width}} {'':>6} {line[offset : offset + WIDTH]}")
        out.append(f"{names[1]:<{width}} {shown[1]:>6} {chunk[1]} {ends[1]:>6}")
        out.append("")
        at = [ends[0] + 1, ends[1] + 1]
    return "\n".join(out) + "\n"


def percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def align(run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    (query,) = run["inputs"]["query"]
    (target,) = run["inputs"]["target"]
    params = run["params"]
    mode, matrix = params["mode"], params["matrix"]
    gap_open, gap_extend = float(params["gap_open"]), float(params["gap_extend"])
    a, b = query["values"]["sequence"].upper(), target["values"]["sequence"].upper()
    for entity, sequence in ((query, a), (target, b)):
        if not sequence.removesuffix("*"):
            fail(f"{entity['hrid']} has no residues to align.")
    a, b = a.removesuffix("*"), b.removesuffix("*")
    if len(a) * len(b) > MAX_CELLS:
        fail(
            f"{query['hrid']} and {target['hrid']} are too long to align here: "
            f"{len(a):,} by {len(b):,} residues is more than {MAX_CELLS:,} cells."
        )
    found = aligner(mode, matrix, gap_open, gap_extend)
    try:
        best = found.align(a, b)[0]
    except ValueError as error:
        fail(f"Couldn't align them: {error}.")
    top, bottom = best[0], best[1]
    scores = found.substitution_matrix
    line = marks(scores, top, bottom)
    length = len(top)
    identical = line.count("|")
    similar = identical + line.count(":")
    gaps = sum("-" in pair for pair in zip(top, bottom, strict=True))
    starts = (int(best.coordinates[0][0]) + 1, int(best.coordinates[1][0]) + 1)
    names = (query["hrid"], target["hrid"])
    header = [
        "Aligned by Iki's protein.align_pair, with Biopython's PairwiseAligner",
        f"Run: {run['run']['hrid']}",
        f"Mode: {mode} ({'needle' if mode == 'global' else 'water'})",
        f"Matrix: {matrix}",
        f"Gap_penalty: {gap_open}",
        f"Extend_penalty: {gap_extend}",
        "",
        f"1: {names[0]} {query['values'].get('name') or ''}".rstrip(),
        f"2: {names[1]} {target['values'].get('name') or ''}".rstrip(),
        "",
        f"Length: {length}",
        f"Identity:   {identical}/{length} ({percent(identical, length)}%)",
        f"Similarity: {similar}/{length} ({percent(similar, length)}%)",
        f"Gaps:       {gaps}/{length} ({percent(gaps, length)}%)",
        f"Score: {best.score:.1f}",
    ]
    (out_dir / "alignment.txt").write_text(
        emboss(names, (top, bottom), starts, line, header), encoding="utf-8"
    )
    (out_dir / "alignment.fasta").write_text(
        "".join(
            f">{n}\n" + "".join(f"{row[i : i + 60]}\n" for i in range(0, len(row), 60))
            for n, row in zip(names, (top, bottom), strict=True)
        ),
        encoding="utf-8",
    )
    print(
        f"{names[0]} vs {names[1]}: score {best.score:.1f}, {percent(identical, length)}% identical"
    )
    return {
        "metrics": {
            "score": round(best.score, 1),
            "length": length,
            "identity_percent": percent(identical, length),
            "similarity_percent": percent(similar, length),
            "gaps_percent": percent(gaps, length),
        }
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

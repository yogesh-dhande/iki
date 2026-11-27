"""`consensus`: registers an alignment's consensus as a sequence of its own, derived from
the alignment.

The DNA and protein plugins each ship this script, the same file (a test checks it): its
argument says which alphabet it reads, `nucleotide` (registering an `iki/dna.DNASequence`)
or `protein` (an `iki/protein.Protein`).

Each column of the alignment gives at most one residue, as Geneious calls a consensus:

- Rows count where they have a residue or a gap. Iki's `.`, a place a row has no
  observation of (beyond a read's ends), counts as neither, so reads say nothing of what
  they didn't read. The reference row counts too unless the run leaves it out, as it
  should for reads, whose consensus says what was sequenced, not what was designed.
- A column where more of those rows have a gap than a residue is left out: the
  consensus has no residue there. A column where every row has a gap is too.
- Otherwise its residue is the commonest one, if at least the threshold's share of the
  rows with a residue have it. If none has, it's the fewest commonest residues that
  together reach the threshold, written as their ambiguity code (IUPAC's for bases;
  B, Z and J for D or N, E or Q, and I or L; X for any other set of amino acids), as
  Geneious calls one. Residues tied at the threshold's edge are taken together or not at
  all, so the call never depends on which letter comes first. Without ambiguity codes,
  such a column is N (or X).
- A row's own ambiguity code counts as an equal share of each residue it stands for,
  as Geneious counts one: R as half an A and half a G, N as a quarter of each base, X
  as a twentieth of each standard amino acid.
- A column that no row observes (where reads don't reach) is N (or X) inside the
  consensus, so the stretches either side stay apart and in place, and is left out at
  either end of a linear one: nothing there was observed. A circular one has no ends.

Shares are counted exactly, as fractions, so a column at the threshold's edge isn't
decided by rounding. RNA rows (U) give DNA (T), as the DNA plugin keeps RNA in GenBank's
T alphabet.
"""

from __future__ import annotations

import re
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any, NoReturn

import iki_action as iki

#: What each IUPAC base code stands for.
BASES: dict[str, str] = {
    "A": "A",
    "C": "C",
    "G": "G",
    "T": "T",
    "R": "AG",
    "Y": "CT",
    "S": "CG",
    "W": "AT",
    "K": "GT",
    "M": "AC",
    "B": "CGT",
    "D": "AGT",
    "H": "ACT",
    "V": "ACG",
    "N": "ACGT",
}
#: Each set of bases' IUPAC code.
BASE_CODES = {frozenset(stands): code for code, stands in BASES.items()}
#: The standard amino acids, and those Iki's proteins also hold: selenocysteine (U),
#: pyrrolysine (O) and a stop (*).
STANDARD = "ACDEFGHIKLMNPQRSTVWY"
RESIDUES: dict[str, str] = {
    **{r: r for r in STANDARD + "UO*"},
    "B": "DN",
    "Z": "EQ",
    "J": "IL",
    "X": STANDARD,
}
RESIDUE_CODES = {frozenset(stands): code for code, stands in RESIDUES.items() if code != "X"}
#: Gaps, and Iki's mark for a place a row has no observation of.
GAP, UNSEEN = "-", "."
#: The most columns it reads, as the alignment viewer does.
MAX_COLUMNS = 1_000_000
#: The most residues and gaps it reads, rows times columns: a minute or so of counting.
MAX_CELLS = 100_000_000


def fail(why: str) -> NoReturn:
    raise iki.Refused(why)


def read_fasta(path: Path) -> list[tuple[str, str]]:
    records: list[tuple[str, list[str]]] = []
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if line.startswith(">"):
                records.append((line[1:].strip(), []))
            elif records:
                records[-1][1].append(line.strip())
    return [(header, "".join(parts).upper()) for header, parts in records]


def counted(n: int, thing: str) -> str:
    return f"{n:,} {thing}" + ("" if n == 1 else "s")


def first_word(header: str) -> str:
    """A header's first word, without the `/start-end` that says which part it is."""
    word = header.split(maxsplit=1)[0] if header.strip() else ""
    return re.sub(r"/\d+-\d+$", "", word)


def called(
    counts: Counter[str], codes: dict[str, str], threshold: Fraction, ambiguity: bool
) -> str:
    """The column's residue from what its rows have, as the module says."""
    shares: dict[str, Fraction] = {}
    for letter, count in counts.items():
        stands = codes[letter]
        for residue in stands:
            shares[residue] = shares.get(residue, Fraction(0)) + Fraction(count, len(stands))
    total = sum(shares.values(), Fraction(0))
    ranked = sorted(shares.items(), key=lambda kv: -kv[1])
    taken: list[str] = []
    reached = Fraction(0)
    index = 0
    while index < len(ranked) and reached < threshold * total:
        # Every residue tied with this one comes too.
        share = ranked[index][1]
        while index < len(ranked) and ranked[index][1] == share:
            taken.append(ranked[index][0])
            reached += share
            index += 1
    unknown = "N" if codes is BASES else "X"
    if len(taken) == 1:
        return taken[0]
    if not ambiguity:
        return unknown
    table = BASE_CODES if codes is BASES else RESIDUE_CODES
    return table.get(frozenset(taken), unknown)


def consensus(run: dict[str, Any], alphabet: str) -> dict[str, Any]:
    (alignment,) = run["inputs"]["alignment"]
    params = run["params"]
    threshold = Fraction(str(params.get("threshold", 50))) / 100
    ambiguity = bool(params.get("ambiguity", True))
    with_reference = bool(params.get("include_reference", True))
    nucleotide = alphabet == "nucleotide"
    codes = BASES if nucleotide else RESIDUES
    unknown = "N" if nucleotide else "X"
    hrid = alignment["hrid"]
    file_id = alignment["values"].get("aligned")
    if not file_id or file_id not in run.get("files", {}):
        fail(f"{hrid} has no aligned file to read.")
    rows = read_fasta(Path(run["files"][file_id]["path"]))
    if not rows:
        fail(f"{hrid}'s file has no rows.")
    length = len(rows[0][1])
    if any(len(row) != length for _, row in rows):
        fail(f"{hrid}'s rows aren't all the same length, so it isn't an alignment.")
    if length > MAX_COLUMNS:
        fail(f"{hrid} is too long to read here: {length:,} columns is more than {MAX_COLUMNS:,}.")
    if length * len(rows) > MAX_CELLS:
        fail(
            f"{hrid} is too big to read here: {len(rows):,} rows of {length:,} columns is more "
            f"than {MAX_CELLS:,} places."
        )
    reference = (alignment.get("included") or {}).get("reference") or []
    left_out = ""
    if not with_reference and reference:
        reference_hrid = reference[0]["hrid"]
        kept = [(h, r) for h, r in rows if first_word(h) != reference_hrid]
        if len(kept) < len(rows):
            left_out = reference_hrid
        rows = kept
        if not rows:
            fail(f"{hrid} has no rows but its reference, {reference_hrid}.")
    allowed = set(codes) | {GAP, UNSEEN} | ({"U"} if nucleotide else set())
    for header, row in rows:
        odd = next((i for i, c in enumerate(row) if c not in allowed), None)
        if odd is not None:
            kind = "an IUPAC base" if nucleotide else "an amino acid"
            fail(
                f"{hrid} isn't an alignment of {'nucleotides' if nucleotide else 'proteins'}: "
                f"{first_word(header) or 'a row'} has {row[odd]!r} at column {odd + 1}, "
                f"not {kind}."
            )
    calls: list[str | None] = []  # a residue, None for a column no row observes
    dropped = ambiguous = 0
    for column in zip(*(row for _, row in rows), strict=True):
        counts = Counter(c for c in column if c not in (GAP, UNSEEN))
        if nucleotide and "U" in counts:
            counts["T"] += counts.pop("U")
        gaps = sum(1 for c in column if c == GAP)
        residues = sum(counts.values())
        if not residues and not gaps:
            calls.append(None)
            continue
        if gaps > residues:
            dropped += 1
            continue
        residue = called(counts, codes, threshold, ambiguity)
        if len(codes[residue]) > 1:
            ambiguous += 1
        calls.append(residue)
    topology = params.get("topology", "linear") if nucleotide else "linear"
    if topology == "linear":
        while calls and calls[0] is None:
            calls.pop(0)
        while calls and calls[-1] is None:
            calls.pop()
    unseen = sum(1 for c in calls if c is None)
    sequence = "".join(c or unknown for c in calls)
    if not sequence:
        fail(f"{hrid} has no column a consensus has a residue in.")
    title = alignment["values"].get("name") or hrid
    name = (params.get("name") or "").strip() or f"Consensus of {title}"
    said = [
        f"The consensus of {hrid}'s {len(rows):,} rows"
        + (f" (its reference, {left_out}, left out)" if left_out else "")
        + f", each column's residue that at least {float(threshold * 100):g}% of its rows have"
        + (", or the ambiguity code of those that together do" if ambiguity else "")
        + ". "
        + (f"{counted(dropped, 'column')} mostly gaps left out. " if dropped else "")
        + (f"{counted(unseen, 'place')} no row observes written {unknown}. " if unseen else "")
    ]
    values: dict[str, Any] = {"name": name[:255], "description": "".join(said).strip()[:10000]}
    if nucleotide:
        values.update(
            {
                "bases": sequence,
                "topology": topology,
                "strandedness": params.get("strandedness", "double"),
            }
        )
        created_type = "iki/dna.DNASequence"
    else:
        values["sequence"] = sequence
        created_type = "iki/protein.Protein"
    print(
        f"{hrid}: a consensus of {len(sequence):,} residues from {len(rows):,} rows, "
        f"{ambiguous:,} ambiguous, {unseen:,} unobserved, {dropped:,} columns left out."
    )
    messages = []
    if left_out:
        messages.append(
            {"level": "info", "text": f"{left_out}, the reference, is left out of the consensus."}
        )
    if unseen:
        messages.append(
            {
                "level": "warning",
                "text": f"No row observes {unseen:,} of the consensus's places, so they're "
                f"{unknown}.",
            }
        )
    return {
        "creates": [{"type": created_type, "values": values, "derived_from": [alignment["id"]]}],
        "metrics": {
            "rows": len(rows),
            "length": len(sequence),
            "ambiguous": ambiguous,
            "unobserved": unseen,
            "columns_left_out": dropped,
        },
        "messages": messages,
    }


if __name__ == "__main__":
    iki.main(
        {
            "nucleotide": lambda run: consensus(run, "nucleotide"),
            "protein": lambda run: consensus(run, "protein"),
        }
    )

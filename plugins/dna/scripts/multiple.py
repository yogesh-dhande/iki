"""`align_multiple`: aligns many DNA sequences with MAFFT, into an `iki/core.Alignment` of
them.

The rows are in the order the sequences were given, the reference first if there is one,
each headed by its sequence's HRID, so the alignment viewer links each row to its sequence
(ADR 0088). MAFFT's strategy is the run's choice: `auto` lets MAFFT pick by the number and
length of the sequences (L-INS-i for a few, FFT-NS-2 for thousands); the alignment's
method records the one it used, with MAFFT's version and the threads it ran on: its
alignment can differ with their number.

Sequences from different sources are often on different strands. MAFFT can adjust their
direction (`--adjustdirection`, or `--adjustdirectionaccurately`), as Benchling's MAFFT
alignments do by default: it takes a sequence's reverse complement where that fits the
first sequence better. Such a row is headed HRID/end-start, as Rfam and Pfam write a
sequence on its reverse strand, so the viewer numbers its bases down from its last.

MAFFT may only add gaps, and reverse a sequence when it was asked to: a row whose bases
aren't its sequence's, or their reverse complement, in order, fails the run. The
alignment is kept as an artifact (ADR 0068), `alignment.fasta`, which is also the file
of the alignment the run creates.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, NoReturn

import iki_action as iki

#: The longest sequence it takes: a large plasmid, a BAC or a phage genome.
MAX_LENGTH = 200_000
#: The most bases of all the sequences together.
MAX_BASES = 10_000_000
#: The most sequences MAFFT's iterative strategies are meant for; above it, `auto` or
#: FFT-NS-2.
MAX_ITERATIVE = 200
#: MAFFT's options for each strategy, as its manual gives them.
STRATEGIES: dict[str, list[str]] = {
    "auto": ["--auto"],
    "L-INS-i": ["--localpair", "--maxiterate", "1000"],
    "G-INS-i": ["--globalpair", "--maxiterate", "1000"],
    "E-INS-i": ["--genafpair", "--maxiterate", "1000"],
    "FFT-NS-2": ["--retree", "2"],
}
#: MAFFT's options for adjusting direction.
DIRECTIONS: dict[str, list[str]] = {
    "fast": ["--adjustdirection"],
    "accurate": ["--adjustdirectionaccurately"],
    "off": [],
}
#: What MAFFT puts before the name of a sequence it reversed.
REVERSED = "_R_"
#: IUPAC's bases and ambiguity codes.
BASES = re.compile("[^ACGTRYSWKMBDHVN]")
COMPLEMENT = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")


def fail(why: str) -> NoReturn:
    raise iki.Refused(why)


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def threads() -> int:
    """As many threads as the container may use: its cgroup's CPU limit, rounded up, but
    no more than the machine's CPUs."""
    cpus = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
    except (OSError, ValueError):
        return cpus
    if quota == "max":
        return cpus
    return max(1, min(cpus, math.ceil(int(quota) / int(period))))


def given_sequences(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The sequences to align, in order, the reference first and each once."""
    given = [*run["inputs"].get("reference", []), *run["inputs"]["sequences"]]
    seen: set[str] = set()
    out = []
    for entity in given:
        if entity["id"] not in seen:
            seen.add(entity["id"])
            out.append(entity)
    if len(out) < 2:
        fail("Choose at least two sequences to align.")
    return out


def bases_of(entities: list[dict[str, Any]]) -> list[str]:
    out = []
    for entity in entities:
        bases = str(entity["values"].get("bases") or "").upper()
        if not bases:
            fail(f"{entity['hrid']} has no bases to align.")
        odd = BASES.search(bases)
        if odd:
            fail(
                f"{entity['hrid']} has {odd.group()!r} at base {odd.start() + 1}, "
                "not an IUPAC base."
            )
        if len(bases) > MAX_LENGTH:
            fail(
                f"{entity['hrid']} is too long to align here: {len(bases):,} bases is more "
                f"than {MAX_LENGTH:,}."
            )
        out.append(bases)
    total = sum(len(s) for s in out)
    if total > MAX_BASES:
        fail(f"These are too many bases to align here: {total:,} is more than {MAX_BASES:,}.")
    return out


def read_fasta(text: str) -> list[tuple[str, str]]:
    records: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith(">"):
            records.append((line[1:].strip(), []))
        elif records:
            records[-1][1].append(line.strip())
    return [(name, "".join(parts).upper()) for name, parts in records]


def mafft(strategy: str, direction: str, given: Path, cpus: int) -> tuple[str, str]:
    """MAFFT's alignment on `cpus` threads, and what it says about the strategy it used."""
    options = [*STRATEGIES[strategy], "--nuc", *DIRECTIONS[direction], "--thread", str(cpus)]
    done = subprocess.run(  # noqa: S603 - MAFFT from the image, on a file made here
        ["mafft", *options, str(given)],  # noqa: S607 - the image's
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode < 0:
        fail(
            f"MAFFT was stopped (signal {-done.returncode}), as it is when it runs out of "
            "memory: choose Auto or FFT-NS-2, or run it with more memory."
        )
    if done.returncode != 0:
        said = [line.strip() for line in done.stderr.splitlines() if line.strip()]
        fail(f"MAFFT failed ({done.returncode}): {said[-1][:1000] if said else 'it said nothing'}")
    used = re.search(r"^Strategy:\s*\n\s*(\S[^(\n]*?)\s*(?:\(|$)", done.stderr, re.MULTILINE)
    return done.stdout, used.group(1) if used else ""


def version() -> str:
    found = subprocess.run(
        ["mafft", "--version"],  # noqa: S607 - the image's
        capture_output=True,
        text=True,
        check=False,
    )
    return (found.stderr or found.stdout).strip().splitlines()[0] if found.returncode == 0 else ""


def percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def align(run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    entities = given_sequences(run)
    bases = bases_of(entities)
    strategy = run["params"]["strategy"]
    direction = run["params"].get("adjust_direction", "fast")
    if strategy != "auto" and STRATEGIES[strategy][-1] == "1000" and len(entities) > MAX_ITERATIVE:
        fail(
            f"{len(entities):,} sequences are too many for this strategy, which MAFFT means "
            f"for up to {MAX_ITERATIVE}: choose Auto or FFT-NS-2."
        )
    hrids = [e["hrid"] for e in entities]
    with tempfile.TemporaryDirectory() as tmp:
        given = Path(tmp, "given.fasta")
        given.write_text(
            "".join(f">{h}\n{s}\n" for h, s in zip(hrids, bases, strict=True)),
            encoding="ascii",
        )
        # MAFFT's alignment can differ with the number of threads it runs on, so the
        # method says how many it had.
        cpus = threads()
        text, used = mafft(strategy, direction, given, cpus)
    rows = read_fasta(text)
    reversed_rows = [name.startswith(REVERSED) for name, _ in rows]
    names = [name.removeprefix(REVERSED) for name, _ in rows]
    if names != hrids:
        fail("MAFFT's alignment doesn't have the sequences given, in order.")
    if any(reversed_rows) and direction == "off":
        fail("MAFFT reversed a sequence it wasn't asked to.")
    headed = []
    for (name, row), sequence, backwards in zip(rows, bases, reversed_rows, strict=True):
        hrid = name.removeprefix(REVERSED)
        expected = reverse_complement(sequence) if backwards else sequence
        if row.replace("-", "") != expected:
            fail(f"MAFFT changed {hrid}'s bases, not only its gaps.")
        # Its other strand, numbered down from its last base, as Rfam writes one.
        headed.append(f"{hrid}/{len(sequence)}-1" if backwards else hrid)
    length = len(rows[0][1])
    if any(len(row) != length for _, row in rows):
        fail("MAFFT's rows aren't all the same length.")
    columns = list(zip(*(row for _, row in rows), strict=True))
    identical = sum(1 for c in columns if "-" not in c and len(set(c)) == 1)
    gapped = sum(1 for c in columns if "-" in c)
    (out_dir / "alignment.fasta").write_text(
        "".join(
            f">{h}\n" + "".join(f"{row[i : i + 60]}\n" for i in range(0, len(row), 60))
            for h, (_, row) in zip(headed, rows, strict=True)
        ),
        encoding="utf-8",
    )
    picked = used or (strategy if strategy != "auto" else "MAFFT's choice")
    flipped = [h for h, backwards in zip(hrids, reversed_rows, strict=True) if backwards]
    print(f"Aligned {len(rows)} sequences over {length} columns with MAFFT's {picked}.")
    if flipped:
        print(f"MAFFT took the reverse complement of {', '.join(flipped)}.")
    reference = run["inputs"].get("reference") or []
    titles = [e["values"].get("name") or e["hrid"] for e in entities]
    name = (run["params"].get("name") or "").strip() or (
        f"{len(entities)} sequences aligned to {titles[0]}"
        if reference
        else f"{', '.join(titles[:2])} and {len(titles) - 2} more"
        if len(titles) > 3
        else " and ".join([", ".join(titles[:-1]), titles[-1]])
    )
    mafft_version = version()
    method = (
        f"MAFFT {mafft_version}".strip()
        + f", {picked}"
        + (" (chosen by --auto)" if strategy == "auto" and used else "")
        + (f", {' '.join(DIRECTIONS[direction])}" if direction != "off" else "")
        + f", {cpus} thread{'' if cpus == 1 else 's'}"
    )
    values: dict[str, Any] = {
        "name": name[:200],
        "description": (
            f"{len(rows)} sequences over {length} columns: {percent(identical, length)}% of "
            f"columns identical, {percent(gapped, length)}% with gaps."
            + (
                f" {len(flipped)} {'is' if len(flipped) == 1 else 'are'} aligned as "
                f"{'its' if len(flipped) == 1 else 'their'} reverse complement: "
                f"{', '.join(flipped)}."
                if flipped
                else ""
            )
        ),
        "members": [e["id"] for e in entities],
        "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"},
        "method": method,
    }
    if reference:
        values["reference"] = reference[0]["id"]
    return {
        "creates": [
            {
                "type": "iki/core.Alignment",
                "values": values,
                "derived_from": [e["id"] for e in entities],
            }
        ],
        "metrics": {
            "rows": len(rows),
            "length": length,
            "identity_percent": percent(identical, length),
            "gaps_percent": percent(gapped, length),
            "reversed": len(flipped),
        },
    }


if __name__ == "__main__":
    iki.main(lambda run: align(run, run.out_dir))

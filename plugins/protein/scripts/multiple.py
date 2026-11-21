"""`align_multiple`: aligns many proteins with MAFFT, into an `iki/core.Alignment` of them.

The rows are in the order the proteins were given, the reference first if there is one,
each headed by its protein's HRID, so the alignment viewer links each row to its protein
(ADR 0088). MAFFT's strategy is the run's choice: `auto` lets MAFFT pick by the number and
length of the sequences (L-INS-i for a few, FFT-NS-2 for thousands); the alignment's
method records the one it used, with MAFFT's version and the threads it ran on: its
alignment can differ with their number.

MAFFT may only add gaps: a row whose residues aren't its protein's, in order, fails the
run. The alignment is kept as an artifact (ADR 0068), `alignment.fasta`, which is also
the file of the alignment the run creates.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

#: The longest protein it takes: titin, the longest known, is 34,350 residues.
MAX_LENGTH = 40_000
#: The most residues of all the proteins together.
MAX_RESIDUES = 5_000_000
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
#: The residues MAFFT scores without --anysymbol.
STANDARD = set("ACDEFGHIKLMNPQRSTVWYBZX")


def fail(why: str) -> NoReturn:
    print(f"::error {why}", flush=True)
    sys.exit(1)


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


def proteins(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The proteins to align, in order, the reference first and each once."""
    given = [*run["inputs"].get("reference", []), *run["inputs"]["proteins"]]
    seen: set[str] = set()
    out = []
    for entity in given:
        if entity["id"] not in seen:
            seen.add(entity["id"])
            out.append(entity)
    if len(out) < 2:
        fail("Choose at least two proteins to align.")
    return out


def sequences(entities: list[dict[str, Any]]) -> list[str]:
    out = []
    for entity in entities:
        sequence = entity["values"]["sequence"].upper().rstrip("*")
        if not sequence:
            fail(f"{entity['hrid']} has no residues to align.")
        if "*" in sequence:
            fail(f"{entity['hrid']} has a stop inside it, at residue {sequence.index('*') + 1}.")
        odd = re.search(r"[^A-Z]", sequence)
        if odd:
            fail(
                f"{entity['hrid']} has {odd.group()!r} at residue {odd.start() + 1}, not a residue."
            )
        if len(sequence) > MAX_LENGTH:
            fail(
                f"{entity['hrid']} is too long to align here: {len(sequence):,} residues is "
                f"more than {MAX_LENGTH:,}."
            )
        out.append(sequence)
    total = sum(len(s) for s in out)
    if total > MAX_RESIDUES:
        fail(f"These are too many residues to align here: {total:,} is more than {MAX_RESIDUES:,}.")
    return out


def read_fasta(text: str) -> list[tuple[str, str]]:
    records: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith(">"):
            records.append((line[1:].strip(), []))
        elif records:
            records[-1][1].append(line.strip())
    return [(name, "".join(parts).upper()) for name, parts in records]


def mafft(strategy: str, given: Path, unusual: bool, cpus: int) -> tuple[str, str]:
    """MAFFT's alignment on `cpus` threads, and what it says about the strategy it used."""
    options = [*STRATEGIES[strategy], "--amino", "--thread", str(cpus)]
    if unusual:
        options.append("--anysymbol")
    done = subprocess.run(  # noqa: S603 - MAFFT from the image, on a file made here
        ["mafft", *options, str(given)],  # noqa: S607 - the image's
        capture_output=True,
        text=True,
        check=False,
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
    entities = proteins(run)
    residues = sequences(entities)
    strategy = run["params"]["strategy"]
    if strategy != "auto" and STRATEGIES[strategy][-1] == "1000" and len(entities) > MAX_ITERATIVE:
        fail(
            f"{len(entities):,} proteins are too many for this strategy, which MAFFT means for "
            f"up to {MAX_ITERATIVE}: choose Auto or FFT-NS-2."
        )
    hrids = [e["hrid"] for e in entities]
    unusual = any(set(s) - STANDARD for s in residues)
    with tempfile.TemporaryDirectory() as tmp:
        given = Path(tmp, "given.fasta")
        given.write_text(
            "".join(f">{h}\n{s}\n" for h, s in zip(hrids, residues, strict=True)),
            encoding="ascii",
        )
        # MAFFT's alignment can differ with the number of threads it runs on, so the
        # method says how many it had.
        cpus = threads()
        text, used = mafft(strategy, given, unusual, cpus)
    rows = read_fasta(text)
    if [name for name, _ in rows] != hrids:
        fail("MAFFT's alignment doesn't have the proteins given, in order.")
    for (name, row), sequence in zip(rows, residues, strict=True):
        if row.replace("-", "") != sequence:
            fail(f"MAFFT changed {name}'s residues, not only its gaps.")
    length = len(rows[0][1])
    if any(len(row) != length for _, row in rows):
        fail("MAFFT's rows aren't all the same length.")
    columns = list(zip(*(row for _, row in rows), strict=True))
    identical = sum(1 for c in columns if "-" not in c and len(set(c)) == 1)
    gapped = sum(1 for c in columns if "-" in c)
    (out_dir / "alignment.fasta").write_text(
        "".join(
            f">{n}\n" + "".join(f"{row[i : i + 60]}\n" for i in range(0, len(row), 60))
            for n, row in rows
        ),
        encoding="utf-8",
    )
    picked = used or (strategy if strategy != "auto" else "MAFFT's choice")
    print(f"Aligned {len(rows)} proteins over {length} columns with MAFFT's {picked}.")
    reference = run["inputs"].get("reference") or []
    titles = [e["values"].get("name") or e["hrid"] for e in entities]
    name = (run["params"].get("name") or "").strip() or (
        f"{len(entities)} proteins aligned to {titles[0]}"
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
        + (", with --anysymbol for unusual residues" if unusual else "")
        + f", {cpus} thread{'' if cpus == 1 else 's'}"
    )
    values: dict[str, Any] = {
        "name": name[:200],
        "description": (
            f"{len(rows)} proteins over {length} columns: {percent(identical, length)}% of "
            f"columns identical, {percent(gapped, length)}% with gaps."
        ),
        "members": [e["id"] for e in entities],
        "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-aa-fasta"},
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

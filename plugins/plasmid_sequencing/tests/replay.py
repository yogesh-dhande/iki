"""minimap2's answers, recorded, so the whole-plasmid tests run where minimap2 isn't.

The tests run `scripts/whole_plasmid.py` on the scenarios below. Each time it runs
minimap2, the test looks the call up in `data/minimap2-replay.json.gz` by its arguments,
with each file named by its contents' SHA-256 (and the thread count left out: minimap2
writes its alignments in its input's order, whatever the threads). A call it hasn't seen
fails the test, which then needs recording again, in the plugin's own environment (the
lockfile's minimap2), from the repository's root:

    docker run --rm -v "$PWD/plugins/plasmid_sequencing:/plugin" -w /plugin IMAGE \\
        python -I tests/record_minimap2.py

where IMAGE has the environment `environments/long-read-tools-lock.yml` makes, such as
one built from `micromamba create --file` it.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

PLUGIN = Path(__file__).resolve().parents[1]
DATA = PLUGIN / "tests/data"
REPLAY = DATA / "minimap2-replay.json.gz"
READS = DATA / "SRR17915342-every-900th.fastq.gz"
SEQUENCE_ID = "00000000-0000-7000-8003-000000000099"
SEQUENCE_ENTRY = "00000000-0000-7000-8005-000000000099"
PLASMID_ID = "00000000-0000-7000-8003-000000000002"
PLASMID_ENTRY = "00000000-0000-7000-8005-000000000002"
READ_SET_ID = "00000000-0000-7000-8003-000000000010"
SAMPLE_ID = "00000000-0000-7000-8003-000000000077"
#: What the plasmid plugin's annotate_plasmid finds on L09137.2 with its feature library
#: 1.0 (34 parts, at least 90% identity).
PUC19_FEATURES = [
    {"type": "promoter", "name": "lac promoter", "start": 508, "end": 591, "strand": "reverse"},
    {"type": "rep_origin", "name": "ori", "start": 867, "end": 1455, "strand": "reverse"},
    {"type": "CDS", "name": "AmpR", "start": 1626, "end": 2486, "strand": "reverse"},
]


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "whole_plasmid", PLUGIN / "scripts/whole_plasmid.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    # Dataclasses look their module up while they're made.
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def puc19() -> str:
    lines = (DATA / "L09137.2.fasta").read_text(encoding="utf-8").splitlines()
    return "".join(line.strip() for line in lines if not line.startswith(">"))


def key(args: list[str]) -> str:
    """A call's arguments, each file by its contents, without the thread count."""
    parts: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "-t":
            skip = True
            continue
        path = Path(arg)
        if path.is_absolute() and path.is_file():
            parts.append("sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            parts.append(arg)
    return " ".join(parts)


def replaying() -> Callable[[list[str]], list[str]]:
    with gzip.open(REPLAY, "rt", encoding="utf-8") as handle:
        recorded: dict[str, Any] = json.load(handle)

    def minimap2(args: list[str]) -> list[str]:
        found = key(args)
        if found not in recorded["calls"]:
            raise AssertionError(f"minimap2 wasn't recorded for {found}: record again ({__doc__})")
        return list(recorded["calls"][found])

    minimap2.version = recorded["version"]  # type: ignore[attr-defined]
    return minimap2


def recording(calls: dict[str, list[str]]) -> Callable[[list[str]], list[str]]:
    def minimap2(args: list[str]) -> list[str]:
        done = subprocess.run(["minimap2", *args], capture_output=True, text=True, check=True)  # noqa: S603, S607 - the environment's
        out = done.stdout.splitlines(keepends=True)
        calls[key(args)] = out
        return out

    return minimap2


def sequence_entity(bases: str, topology: str = "circular") -> dict[str, Any]:
    return {
        "id": SEQUENCE_ID,
        "entry_id": SEQUENCE_ENTRY,
        "hrid": "DNA-000099",
        "revision": 1,
        "values": {"bases": bases, "topology": topology},
    }


def plasmid_entity(bases: str, topology: str = "circular") -> dict[str, Any]:
    return {
        "id": PLASMID_ID,
        "entry_id": PLASMID_ENTRY,
        "hrid": "PLS-000002",
        "revision": 3,
        "values": {
            "name": "pUC19",
            "sequence": SEQUENCE_ENTRY,
            "annotated_features": PUC19_FEATURES,
        },
        "included": {"sequence": [sequence_entity(bases, topology)]},
    }


def reads_run(reads: Path, **params: Any) -> dict[str, Any]:
    return {
        "run": {"hrid": "RUN-000001"},
        "params": {"platform": "nanopore", "min_depth": 10, **params},
        "inputs": {
            "plasmid": [plasmid_entity(puc19())],
            "reads": [
                {
                    "id": READ_SET_ID,
                    "hrid": "READ-000010",
                    "revision": 2,
                    "values": {"read_1": "reads"},
                    "included": {
                        "library": [{"id": "lib", "revision": 1, "values": {"sample": SAMPLE_ID}}]
                    },
                }
            ],
        },
        "files": {"reads": {"path": str(reads), "name": reads.name, "sha256": "-"}},
    }


def consensus_run(consensus: Path, bases: str, topology: str = "circular") -> dict[str, Any]:
    return {
        "run": {"hrid": "RUN-000002"},
        "params": {"consensus": "given"},
        "inputs": {"plasmid": [plasmid_entity(bases, topology)]},
        "files": {"given": {"path": str(consensus), "name": consensus.name, "sha256": "-"}},
    }


def run(script: ModuleType, given: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = script.verify(given, out_dir)
    return result


def scenarios(tmp: Path) -> dict[str, tuple[dict[str, Any], Path]]:
    """Each run the tests make that runs minimap2: its input, and where it writes."""
    reference = puc19()
    out: dict[str, tuple[dict[str, Any], Path]] = {
        "reads": (reads_run(READS), tmp / "reads"),
    }
    for name, (bases, topology) in edited(reference).items():
        path = tmp / f"{name}.fasta"
        path.write_text(f">{name}\n{bases}\n", encoding="utf-8")
        out[name] = (consensus_run(path, reference, topology), tmp / name)
    return out


def reverse_complement(bases: str) -> str:
    return bases.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def edited(reference: str) -> dict[str, tuple[str, str]]:
    """Synthetic consensuses: pUC19 (L09137.2) edited by hand, each as a service might
    send it, to test what the comparison makes of each kind of change."""
    rotated = reference[1000:] + reference[:1000]
    # Bases 2684 to 2686 and 1 to 2 (1-based) gone: a deletion across the origin.
    across = reference[2:2683]
    # Base 1,000 (1-based), an A in the ori, changed to C, then the whole turned round.
    substituted = reference[:999] + "C" + reference[1000:]
    # An EcoRI site (GAATTC) inserted after base 1,700, in AmpR.
    inserted = reference[:1700] + "GAATTC" + reference[1700:]
    return {
        "same-rotated": (rotated, "circular"),
        "origin-deletion": (across, "circular"),
        "substitution-other-strand": (
            reverse_complement(substituted[1500:] + substituted[:1500]),
            "circular",
        ),
        "insertion": (inserted[2000:] + inserted[:2000], "circular"),
        "linear-short": (reference[: len(reference) - 50], "linear"),
        # The whole plasmid twice over, as a dimer's consensus would be: every base is in it,
        # but each is there twice.
        "two-laps": (reference + reference, "circular"),
        # 450 bases that aren't pUC19's past the end of a linear copy of it.
        "linear-longer": (reference + unrelated(450), "linear"),
    }


def unrelated(n: int) -> str:
    """Synthetic bases, the same each time: a linear congruential generator's (Knuth's
    MMIX constants), two bits a base."""
    state, out = 1, []
    for _ in range(n):
        state = (6364136223846793005 * state + 1442695040888963407) % 2**64
        out.append("ACGT"[state >> 62])
    return "".join(out)

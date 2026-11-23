"""Records what the DNA plugin's `design_guides` writes for real designs, for the
component test that draws them (server/tests/test_dna_guide_components.py), which has to
be given whole designs rather than plausible-looking ones. Run it from the repository
root:

    uv run python server/tests/data/record_guide_design.py \
        > server/tests/data/guide_design.json

Two designs are recorded:

- **pUC19's ampicillin resistance gene**, bases 1,901 to 2,000, for SpCas9, searched
  against pUC19 and pBR322 (GenBank J01749.1). pUC19 got that gene from pBR322, so some
  of the guides have a perfect match in the other plasmid and some differ by a single
  base: the one kind halves a guide's specificity and the other costs it by where the
  base lies. That is a real off-target result, not an invented one.
- **pUC19's polylinker** for AsCas12a, which reads its motif at the other end of the
  protospacer and cuts staggered, with nothing searched -- so the tab has a design whose
  guides have no specificity to show, and says so.

The designs' own numbers -- where each guide lies, where it cuts, its GC content, what is
flagged about it and every off-target with its score -- are checked in
server/tests/test_dna_guides.py and in the action's own tests
(plugins/dna/tests/design_guides.yaml); this file only keeps them where a browser test can
be handed them.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from Bio import SeqIO

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "plugins" / "dna" / "scripts" / "design_guides.py"
HERE = Path(__file__).resolve().parent

#: pUC19, GenBank L09137.2, and pBR322, J01749.1, as the repository already keeps them.
PUC19 = "".join(
    line.strip()
    for line in (HERE / "L09137.2.fasta").read_text().splitlines()
    if not line.startswith(">")
)
PBR322 = str(
    SeqIO.read(HERE / "plasmids" / "J01749.1.gb", "genbank").seq  # type: ignore[no-untyped-call]
).upper()


def sequence(hrid: str, name: str, bases: str, accession: str) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-0000-0000-0000000000{hrid[-2:]}",
        "hrid": hrid,
        "values": {
            "name": name,
            "bases": bases,
            "topology": "circular",
            "strandedness": "double",
            "accession": accession,
        },
    }


PUC = sequence("DNA-000002", "pUC19", PUC19, "L09137.2")
PBR = sequence("DNA-000003", "pBR322", PBR322, "J01749.1")


def given(values: dict[str, Any], searching: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "params": {},
        "inputs": {
            "design": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "hrid": "GDE-000001",
                    "values": values,
                    "included": {"target": [PUC], "search_in": searching},
                }
            ]
        },
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / "input.json").write_text(json.dumps(payload), encoding="utf-8")
        subprocess.run(  # noqa: S603 - the plugin's own script
            [sys.executable, str(SCRIPT)],
            env={
                **os.environ,
                "IKI_INPUT": str(work / "input.json"),
                "IKI_OUTPUT": str(work / "output.json"),
            },
            check=True,
            stdout=sys.stderr,
        )
        written = json.loads((work / "output.json").read_text(encoding="utf-8"))
    found: dict[str, Any] = written["updates"]["design"][0]["values"]
    return found


SHARED = given(
    {
        "name": "bla guides",
        "nuclease": "spcas9",
        "region_start": 1901,
        "region_end": 2000,
        "most_mismatches": 3,
        "most_guides": 8,
    },
    [PUC, PBR],
)
POLYLINKER = given(
    {
        "name": "Cas12a in the polylinker",
        "nuclease": "ascas12a",
        "region_start": 400,
        "region_end": 500,
    },
    [],
)


def main() -> None:
    recorded = {
        "recorded_by": "server/tests/data/record_guide_design.py",
        "shared": {
            "target": "pUC19 (L09137.2), bases 1,901 to 2,000",
            "searched": "pUC19 (L09137.2) and pBR322 (J01749.1)",
            "values": {"name": "bla guides", "nuclease": "spcas9", **run(SHARED)},
        },
        "polylinker": {
            "target": "pUC19 (L09137.2), bases 400 to 500",
            "searched": "nothing",
            "values": {
                "name": "Cas12a in the polylinker",
                "nuclease": "ascas12a",
                **run(POLYLINKER),
            },
        },
    }
    json.dump(recorded, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

"""Checks an alignment-tools image: the plugin's align_multiple, on the image's MAFFT,
aligns three stretches of pUC19 (GenBank L09137.2, bases 396-515: its multiple cloning
site and lacZ-alpha's start), one missing three bases and one given as its other strand.
MAFFT must put the gap where the bases are missing, and align the third as its reverse
complement, headed by its span running down.

    docker run --rm --network none -v "$PWD/plugins/dna/images/alignment-tools:/check:ro" \
        -v "$PWD/plugins/dna/scripts:/scripts:ro" IMAGE python -I /check/check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

STRETCH = (
    "GAATTCGAGCTCGGTACCCGGGGATCCTCTAGAGTCGACCTGCAGGCATGCAAGCTTGGCGTAATCATGGTCATAGCTGTTTCC"
    "TGTGTGAAATTGTTATCCGCTCACAATTCCACACAA"
)
#: Without its bases 61-63 (GTA): the gap may lie anywhere MAFFT finds as good, among
#: the repeated bases around them.
MISSING = STRETCH[:60] + STRETCH[63:]
COMPLEMENT = str.maketrans("ACGT", "TGCA")


def entity(i: int, bases: str) -> dict[str, object]:
    return {
        "id": f"00000000-0000-0000-0000-{i:012d}",
        "hrid": f"DNA-{i:06d}",
        "values": {"bases": bases},
    }


def main() -> None:
    other_strand = STRETCH.translate(COMPLEMENT)[::-1]
    run = {
        "run": {"hrid": "RUN-000001"},
        "params": {"strategy": "auto", "adjust_direction": "fast"},
        "inputs": {"sequences": [entity(1, STRETCH), entity(2, MISSING), entity(3, other_strand)]},
    }
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "input.json").write_text(json.dumps(run), encoding="utf-8")
        done = subprocess.run(
            # As the worker runs it: its own directory first, where it finds the plugin's
            # iki_action.py (which -I's safe path would leave out); no environment's paths.
            [sys.executable, "-E", "-s", "/scripts/multiple.py"],
            env={
                **os.environ,
                "IKI_INPUT": f"{tmp}/input.json",
                "IKI_OUTPUT": f"{tmp}/output.json",
                "IKI_OUT_DIR": tmp,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            sys.exit(f"align_multiple failed ({done.returncode}):\n{done.stdout}{done.stderr}")
        output = json.loads(Path(tmp, "output.json").read_text(encoding="utf-8"))
        text = Path(tmp, "alignment.fasta").read_text(encoding="utf-8")
    rows = {r.split("\n", 1)[0]: "".join(r.split("\n")[1:]) for r in text.split(">")[1:]}
    expected = ["DNA-000001", "DNA-000002", "DNA-000003/120-1"]
    if list(rows) != expected:
        sys.exit(f"expected rows {expected}, MAFFT gave {list(rows)}")
    if rows["DNA-000001"] != STRETCH or rows["DNA-000003/120-1"] != STRETCH:
        sys.exit(
            f"the whole stretch, and its other strand turned round, should align as one: {rows}"
        )
    gapped = rows["DNA-000002"]
    gap = gapped.find("---")
    if gapped.count("-") != 3 or gap < 0 or not 55 <= gap <= 63:
        sys.exit(f"the gap should be where bases 61-63 are missing: {gapped}")
    print(
        f"{output['creates'][0]['values']['method']}: the gap at {gap + 1}-{gap + 3}, "
        "the other strand turned round"
    )


if __name__ == "__main__":
    main()

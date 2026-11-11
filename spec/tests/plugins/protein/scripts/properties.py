"""Compute each protein's average molecular weight from its sequence.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does.
"""

import json
import os
import sys
from pathlib import Path

#: Average residue masses (u), as in a peptide chain: each amino acid less one water.
RESIDUE = {
    "A": 71.0788, "R": 156.1875, "N": 114.1038, "D": 115.0886, "C": 103.1388,
    "E": 129.1155, "Q": 128.1307, "G": 57.0519, "H": 137.1411, "I": 113.1594,
    "L": 113.1594, "K": 128.1741, "M": 131.1926, "F": 147.1766, "P": 97.1167,
    "S": 87.0782, "T": 101.1051, "W": 186.2132, "Y": 163.1760, "V": 99.1326,
}  # fmt: skip
WATER = 18.01528


def weight(sequence: str) -> float:
    return round(sum(RESIDUE[residue] for residue in sequence.upper()) + WATER, 2)


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    proteins = run["inputs"]["protein"]
    print(f"Python {sys.version.split()[0]} in {sys.prefix}")
    updates = []
    for done, protein in enumerate(proteins, start=1):
        sequence = protein["values"]["sequence"]
        updates.append({"entity": protein["id"], "values": {"molecular_weight": weight(sequence)}})
        print(f"{protein['hrid']}: {len(sequence)} residues")
        print(f"::progress {done / len(proteins):.2f} Weighed {done} of {len(proteins)}")
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"protein": updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()

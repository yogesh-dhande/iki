"""A complex's formula, masses and chain count, from its parts: `compute_composition`.

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does. A run that can't do what it was asked says why in an `::error`
line and exits 1.

A complex is its parts, each with its copies (iki/core.Complex, ADR 0161), so its
formula is theirs, each times its copies:

- a protein's from its residues: each residue's formula as in a chain (the amino acid's
  less a water), and one water for its termini. Selenocysteine (U) is C3H5NOSe and
  pyrrolysine (O) C12H19N3O2; an ambiguous residue (B, Z, J or X) has no one formula, so
  a protein with one is named and nothing is computed;
- any other part's from its entry's `molecular_formula`: a small molecule's, an
  oligonucleotide's or a duplex's, or another complex's, which this action computed when
  it was registered. A charged formula is refused: a complex's parts are neutral here.

Its average mass is its formula's, with the standard atomic weights Biopython has
(Bio.Data.IUPACData.atom_weights), as the protein plugin's molecular weights are, and its
monoisotopic mass from each element's most abundant isotope (AME2020: Wang et al., Chin
Phys C 45:030003, 2021). They're its parts as they are, each cysteine's thiol free: each
disulfide makes a cystine of two cysteines and takes two hydrogens off, which the run
says for its bonds. So amivantamab's chains (KEGG DRUG D11894), C6472H10046N1730O2023S46,
with its 16 disulfides are C6472H10014N1730O2023S46, as KEGG gives them, 145,900.13 u
average and 145,810.1054 u monoisotopic (KEGG's 145,900.22 and 145,810.1055).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

#: Each residue's formula in a chain (the amino acid less a water), as C, H, N, O, S, Se.
RESIDUES: dict[str, dict[str, int]] = {
    "A": {"C": 3, "H": 5, "N": 1, "O": 1},
    "R": {"C": 6, "H": 12, "N": 4, "O": 1},
    "N": {"C": 4, "H": 6, "N": 2, "O": 2},
    "D": {"C": 4, "H": 5, "N": 1, "O": 3},
    "C": {"C": 3, "H": 5, "N": 1, "O": 1, "S": 1},
    "E": {"C": 5, "H": 7, "N": 1, "O": 3},
    "Q": {"C": 5, "H": 8, "N": 2, "O": 2},
    "G": {"C": 2, "H": 3, "N": 1, "O": 1},
    "H": {"C": 6, "H": 7, "N": 3, "O": 1},
    "I": {"C": 6, "H": 11, "N": 1, "O": 1},
    "L": {"C": 6, "H": 11, "N": 1, "O": 1},
    "K": {"C": 6, "H": 12, "N": 2, "O": 1},
    "M": {"C": 5, "H": 9, "N": 1, "O": 1, "S": 1},
    "F": {"C": 9, "H": 9, "N": 1, "O": 1},
    "P": {"C": 5, "H": 7, "N": 1, "O": 1},
    "S": {"C": 3, "H": 5, "N": 1, "O": 2},
    "T": {"C": 4, "H": 7, "N": 1, "O": 2},
    "W": {"C": 11, "H": 10, "N": 2, "O": 1},
    "Y": {"C": 9, "H": 9, "N": 1, "O": 2},
    "V": {"C": 5, "H": 9, "N": 1, "O": 1},
    "U": {"C": 3, "H": 5, "N": 1, "O": 1, "Se": 1},
    "O": {"C": 12, "H": 19, "N": 3, "O": 2},
}
WATER = {"H": 2, "O": 1}
#: Standard atomic weights, as Biopython has them (Bio.Data.IUPACData.atom_weights).
AVERAGE = {
    "C": 12.0107, "H": 1.00794, "N": 14.0067, "O": 15.9994, "S": 32.065, "P": 30.973761,
    "Se": 78.96, "F": 18.9984032, "Cl": 35.453, "Br": 79.904, "I": 126.90447,
    "Na": 22.98977, "K": 39.0983, "Si": 28.0855, "B": 10.811, "Fe": 55.845, "Zn": 65.39,
    "Cu": 63.546, "Mg": 24.305, "Ca": 40.078, "Mn": 54.938049, "Co": 58.9332,
}  # fmt: skip
#: Each element's most abundant isotope's mass (AME2020).
MONOISOTOPIC = {
    "C": 12.0, "H": 1.00782503223, "N": 14.00307400443, "O": 15.99491461957,
    "S": 31.9720711744, "P": 30.97376199842, "Se": 79.9165218, "F": 18.99840316273,
    "Cl": 34.968852682, "Br": 78.9183376, "I": 126.9044719, "Na": 22.989769282,
    "K": 38.9637064864, "Si": 27.97692653465, "B": 11.00930536, "Fe": 55.93493633,
    "Zn": 63.92914201, "Cu": 62.92959772, "Mg": 23.985041697, "Ca": 39.962590863,
    "Mn": 54.93804391, "Co": 58.93319429,
}  # fmt: skip
#: A part that's one chain, for its chain count: a protein's, a nucleic acid's, an
#: oligonucleotide's.
ONE_CHAIN = ("sequence", "bases", "helm")
#: A formula's elements and counts, in Hill order or any; a charge isn't one.
ELEMENT = re.compile(r"([A-Z][a-z]?)([0-9]*)")
FORMULA = re.compile(r"^(?:[A-Z][a-z]?[0-9]*)+$")


class RefusedError(Exception):
    """What a run can't do, and why."""


def _label(entity: dict[str, Any]) -> str:
    return str(entity["values"].get("name") or entity.get("hrid") or entity["id"])


def protein_formula(sequence: str) -> Counter[str]:
    """A protein's formula from its residues. Raises KeyError on a residue without one."""
    out: Counter[str] = Counter(WATER)
    for residue in sequence.removesuffix("*").upper():
        out.update(RESIDUES[residue])
    return out


def parse(formula: str) -> Counter[str]:
    """A neutral formula's elements. Raises ValueError for anything else."""
    if not FORMULA.match(formula):
        raise ValueError(formula)
    out: Counter[str] = Counter()
    for element, count in ELEMENT.findall(formula):
        if element not in AVERAGE:
            raise ValueError(element)
        out[element] += int(count) if count else 1
    return out


def hill(formula: Counter[str]) -> str:
    """Hill order: C, then H, then the rest alphabetically; without carbon, all of them."""
    elements = [e for e in formula if formula[e]]
    first = ["C", "H"] if "C" in elements else []
    ordered = first + sorted(e for e in elements if e not in first)
    return "".join(f"{e}{formula[e] if formula[e] != 1 else ''}" for e in ordered)


def mass(formula: Counter[str], table: dict[str, float]) -> float:
    return sum(table[e] * n for e, n in formula.items())


def _part(part: dict[str, Any]) -> tuple[Counter[str], int]:
    """A part's formula and how many chains it is. Raises :class:`RefusedError`."""
    values = part["values"]
    if part.get("type") == "iki/protein.Protein":
        try:
            return protein_formula(str(values["sequence"])), 1
        except KeyError as residue:
            raise RefusedError(
                f"{_label(part)} has {residue.args[0]!r}, an ambiguous or unknown residue, "
                "which has no one formula."
            ) from None
    given = values.get("molecular_formula")
    if not given:
        raise RefusedError(
            f"{_label(part)} has no molecular formula yet: compute it first, then this."
        )
    try:
        formula = parse(str(given))
    except ValueError:
        raise RefusedError(
            f"{_label(part)}'s formula, {given}, is charged or has an element this doesn't "
            "weigh, so its mass in the complex isn't known."
        ) from None
    if values.get("chain_count") is not None:
        chains = int(values["chain_count"])
    elif part.get("type") == "iki/oligo.Duplex":
        chains = 2
    else:
        chains = int(any(values.get(k) for k in ONE_CHAIN))
    return formula, chains


def composition(complex_: dict[str, Any]) -> dict[str, Any]:
    """A complex's update and messages. Raises :class:`RefusedError`."""
    rows = complex_["values"].get("components") or []
    parts = (complex_.get("included") or {}).get("components.part") or []
    if len(parts) != len(rows):
        raise RefusedError("Its parts weren't given to the run.")
    formula: Counter[str] = Counter()
    chains = 0
    for row, part in zip(rows, parts, strict=True):
        each, count = _part(part)
        copies = int(row.get("copies") or 1)
        for element, n in each.items():
            formula[element] += n * copies
        chains += count * copies
    values = {
        "molecular_formula": hill(formula),
        "molecular_weight": round(mass(formula, AVERAGE), 2),
        "monoisotopic_mass": round(mass(formula, MONOISOTOPIC), 4),
        "chain_count": chains,
    }
    messages = []
    disulfides = sum(
        1 for b in complex_["values"].get("bonds") or [] if b.get("kind") == "disulfide"
    )
    if disulfides:
        bonded = formula.copy()
        bonded["H"] -= 2 * disulfides
        messages.append(
            {
                "level": "info",
                "text": (
                    f"With its {disulfides} disulfide{'s' if disulfides != 1 else ''}: "
                    f"{hill(bonded)}, {mass(bonded, AVERAGE):,.2f} u average and "
                    f"{mass(bonded, MONOISOTOPIC):,.4f} u monoisotopic."
                ),
            }
        )
    others = [
        b["kind"] for b in complex_["values"].get("bonds") or [] if b.get("kind") != "disulfide"
    ]
    if others:
        messages.append(
            {
                "level": "info",
                "text": f"Its {', '.join(sorted(set(others)))} bonds aren't weighed: what each "
                "takes off depends on its chemistry.",
            }
        )
    for message in messages:
        message["entity"] = complex_["id"]
    return {"update": {"entity": complex_["id"], "values": values}, "messages": messages}


def compute(run: dict[str, Any]) -> dict[str, Any]:
    """Each complex's composition. One that can't be computed is left out, saying why; a
    run that computes none fails, raising :class:`RefusedError`."""
    updates: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = []
    refused: list[str] = []
    for complex_ in run["inputs"]["complex"]:
        try:
            found = composition(complex_)
        except RefusedError as why:
            refused.append(f"{_label(complex_)}: {why}")
            messages.append({"level": "warning", "text": str(why), "entity": complex_["id"]})
            continue
        updates.append(found["update"])
        messages += found["messages"]
        values = found["update"]["values"]
        print(f"{_label(complex_)}: {values['molecular_formula']}, {values['molecular_weight']} u")
    if not updates:
        raise RefusedError("; ".join(refused))
    return {"updates": {"complex": updates}, "messages": messages}


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        result = compute(run)
    except RefusedError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **result}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()

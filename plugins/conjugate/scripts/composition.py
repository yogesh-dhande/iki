"""Compute the neutral composition of a declared conjugate species.

Each attachment contributes copies × (its registered formula less its stated loss).
An unlabelled atom contributes its standard atomic weight to the average mass, from
CIAAW's table every Iki plugin weighs by (weights.py), and its most abundant isotope to
the monoisotopic mass (IUPAC, doi:10.1351/goldbook.12495), from the periodic table in the
plugin's pinned RDKit release
(https://www.rdkit.org/docs/cppapi/classRDKit_1_1PeriodicTable.html). Explicitly labelled
atoms contribute their isotope's mass, RDKit's, to both.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from functools import cache
from math import fsum, isfinite
from pathlib import Path
from typing import Any

from rdkit import Chem
from rdkit.Chem import rdMolDescriptors
from weights import AVERAGE

TABLE = Chem.GetPeriodicTable()
ELEMENTS = {TABLE.GetElementSymbol(n): n for n in range(1, TABLE.GetMaxAtomicNumber() + 1)}
ATOM = re.compile(
    r"(?:\[(?P<isotope>[1-9]\d{0,2})(?P<label>[A-Z][a-z]?)\]|(?P<element>[A-Z][a-z]?))"
    r"(?P<count>[1-9]\d*)?"
)
type Formula = Counter[tuple[str, int]]
OLIGO_BASIS = "Neutral free-acid carrier and declared attachments, after each recorded atom loss."
PROTEIN_BASIS = (
    "Model of neutral, reduced carrier chains without glycans or other post-translational "
    "modifications, plus the declared attachments. Each atom loss is relative to that basis."
)
# Free amino acids outside RDKit's peptide reader's alphabet (PubChem CIDs 25076, 5460671).
SPECIAL = {"U": "C3H7NO2Se", "O": "C12H21N3O3"}
WATER: Formula = Counter({("H", 0): 2, ("O", 0): 1})


def parse(text: str) -> Formula:
    """Read a neutral elemental formula, including RDKit's [13C], D and T labels."""
    atoms: Formula = Counter()
    pos = 0
    while pos < len(text):
        token = ATOM.match(text, pos)
        if token is None:
            raise ValueError(f"Expected a neutral elemental formula, received {text!r}.")
        pos = token.end()
        element = token["label"] or token["element"]
        isotope = int(token["isotope"] or 0)
        if element in {"D", "T"} and not isotope:
            isotope = 2 if element == "D" else 3
            element = "H"
        if element not in ELEMENTS or (
            isotope and TABLE.GetMassForIsotope(ELEMENTS[element], isotope) <= 0
        ):
            raise ValueError(f"Unknown element or isotope in {text!r}.")
        atoms[element, isotope] += int(token["count"] or 1)
    if not atoms:
        raise ValueError("The neutral molecular formula is missing.")
    return atoms


@cache
def amino_acid(code: str) -> Formula:
    """The neutral free amino acid, building a one-residue peptide with RDKit."""
    if code in SPECIAL:
        return parse(SPECIAL[code])
    # IUPAC ambiguity J comprises leucine and isoleucine, which have the same formula.
    molecule = Chem.MolFromSequence("L" if code == "J" else code)
    if molecule is None:
        raise ValueError(f"Residue {code!r} has no unique amino-acid formula.")
    return parse(rdMolDescriptors.CalcMolFormula(molecule))


def protein(sequence: str) -> Formula:
    """Sum the free amino acids, subtracting one water per peptide bond (IUPAC Gold Book P04479)."""
    residues = sequence.removesuffix("*").upper()
    if not residues:
        raise ValueError("The carrier protein sequence is missing.")
    atoms: Formula = Counter()
    for code, count in Counter(residues).items():
        for atom, number in amino_acid(code).items():
            atoms[atom] += number * count
    for atom, number in WATER.items():
        atoms[atom] -= number * (len(residues) - 1)
    return atoms


def properties(atoms: Formula) -> dict[str, Any]:
    if any(number < 0 for number in atoms.values()) or not any(atoms.values()):
        raise ValueError("The declared atom losses exceed or exhaust the combined formula.")
    # Hill, JACS 22 (1900), 478–494, doi:10.1021/ja02046a005. Keep labels with their element.
    has_carbon = any(e == "C" and n > 0 for (e, _), n in atoms.items())
    terms = []
    averages = []
    monoisotopic = []
    for (element, isotope), count in sorted(
        atoms.items(),
        key=lambda item: ({"C": 0, "H": 1}.get(item[0][0], 2) if has_carbon else 0, *item[0]),
    ):
        if not count:
            continue
        n = ELEMENTS[element]
        if isotope:
            token = {("H", 2): "D", ("H", 3): "T"}.get((element, isotope), f"[{isotope}{element}]")
            weight = exact = TABLE.GetMassForIsotope(n, isotope)
        else:
            token = element
            exact = TABLE.GetMostCommonIsotopeMass(n)
            if (
                not exact
                or not TABLE.GetAbundanceForIsotope(n, TABLE.GetMostCommonIsotope(n))
                or element not in AVERAGE
            ):
                raise ValueError(f"{element} requires an isotope label to assign a mass.")
            weight = AVERAGE[element]
        terms.append(token if count == 1 else f"{token}{count}")
        averages.append(weight * count)
        monoisotopic.append(exact * count)
    formula = "".join(terms)
    if len(formula) > 1000:
        raise ValueError("The combined formula exceeds the record's formula length limit.")
    average, mono = fsum(averages), fsum(monoisotopic)
    if not (isfinite(average) and isfinite(mono)):
        raise ValueError("The combined masses exceed the record's numeric range.")
    return {
        "molecular_formula": formula,
        "molecular_weight": round(average, 2),
        "monoisotopic_mass": round(mono, 4),
    }


def composition(entity: dict[str, Any]) -> dict[str, Any]:
    included = entity.get("included") or {}
    carriers = included.get("carrier.molecule") or []
    carrier = carriers[0]["values"] if len(carriers) == 1 else {}
    values: dict[str, Any] = {
        "carrier_heavy_copies": None,
        "carrier_light_copies": None,
        "molecular_formula": None,
        "molecular_weight": None,
        "monoisotopic_mass": None,
    }
    if carrier.get("components"):
        # Of the permitted carrier types, only antibodies have component rows.
        for role in ("heavy", "light"):
            values[f"carrier_{role}_copies"] = sum(
                row["copies"] for row in carrier["components"] if row["role"] == role
            )
    try:
        if not carrier:
            raise ValueError("The registered carrier was unavailable.")
        rows = entity["values"].get("attachments") or []
        parts = included.get("attachments.part") or []
        if len(rows) != len(parts) or not rows:
            raise ValueError("The registered attachment components were unavailable.")
        if any(row.get("peg_grade") for row in rows):
            raise ValueError("A polydisperse PEG grade has no single molecular formula or mass.")
        if any(row.get("copies") is None for row in rows):
            raise ValueError("Unspecified or distributed loading has no single formula or mass.")
        if "sequence" in carrier:
            atoms = protein(carrier["sequence"])
            basis = PROTEIN_BASIS
        else:
            atoms = parse(carrier.get("molecular_formula") or "")
            basis = PROTEIN_BASIS if "components" in carrier else OLIGO_BASIS
        for row, part in zip(rows, parts, strict=True):
            loss = row.get("loss_formula")
            if not loss:
                raise ValueError("The net atom loss per attachment must be specified.")
            compound = part["values"]
            if compound.get("formal_charge") or "." in (compound.get("smiles") or ""):
                raise ValueError(
                    "Use the neutral, single-component attachment without counterions or solvent."
                )
            part_formula = compound.get("molecular_formula")
            if not part_formula:
                name = compound.get("name") or part["hrid"]
                raise ValueError(f"The registered attachment {name} has no molecular formula.")
            attached = parse(part_formula)
            if loss != "none":
                attached.subtract(parse(loss))
            for atom, number in attached.items():
                atoms[atom] += number * row["copies"]
        values.update(properties(atoms))
        values["composition_note"] = basis
    except (ValueError, OverflowError) as exc:
        # Clear old project results when a referenced compound is corrected or loading
        # is unknown. The independent antibody chain counts still support a lot's DAR.
        values["composition_note"] = str(exc)[:1000]
    return values


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    updates = []
    messages = []
    for entity in run["inputs"]["conjugate"]:
        values = composition(entity)
        updates.append({"entity": entity["id"], "values": values})
        if values["molecular_formula"] is None:
            messages.append(
                {"entity": entity["id"], "level": "warning", "text": values["composition_note"]}
            )
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {"conjugate": updates},
        "messages": messages,
    }
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()

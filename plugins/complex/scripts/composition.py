"""A complex's formula, masses and chain count, from its parts: `compute_composition`.

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does. A run that can't do what it was asked says why in an `::error`
line and exits 1; a run of several complexes that can compute some says why it can't
compute the others in an error about each, which fails that one's run alone (ADR 0055).

A complex is its parts, each with its copies (iki/core.Complex, ADR 0161), so its
formula is theirs, each times its copies:

- a protein's from its residues: each residue's formula as in a chain (the amino acid's
  less a water), and one water for its termini. Selenocysteine (U) is C3H5NOSe and
  pyrrolysine (O) C12H19N3O2. J is leucine or isoleucine (NCBI's IUPAC codes), which
  have one formula, C6H11NO in a chain, and neither absorbs at 280 nm, so it's weighed as
  either. B (aspartate or asparagine), Z (glutamate or glutamine) and X have no one
  formula, so a protein with one is named and nothing is computed;
- a DNA sequence's (iki/dna.DNASequence) from its bases, as Biopython's
  molecular_weight counts DNA's atoms: each base its deoxynucleoside 5′-monophosphate's (dAMP
  C10H14N5O6P, dCMP C9H14N3O7P, dGMP C10H14N5O7P, dTMP C10H15N2O8P) less a water for each
  phosphodiester bond, so a linear strand has a 5′ phosphate and a 3′ hydroxyl, and a
  circular one neither; a double-stranded one is both its strands. A strand made with a
  5′ hydroxyl, as a synthetic oligonucleotide or a PCR product of unphosphorylated
  primers is, has HPO3 (79.98 u) less, which the run says. An ambiguous base (N, R and
  the like) has no one formula;
- any other part's from its entry's `molecular_formula`: a small molecule's, an
  oligonucleotide's or a duplex's, or another complex's, which this action computed when
  it was registered, with any isotope it gives as RDKit writes it ([13C], D for 2H). A
  charged formula is refused: a complex's parts are neutral here.

Its average mass is its formula's, with CIAAW's standard atomic weights, the table every
Iki plugin weighs by (weights.py), as the protein plugin's molecular weights are, and a
labelled atom's isotope's mass; and its monoisotopic mass from each element's most
abundant isotope, or a labelled atom's own (AME2020: Wang et al., Chin Phys C 45:030003,
2021). They're its parts as they are, each cysteine's thiol free: each disulfide makes a
cystine of two cysteines and takes two hydrogens off, which the run says for its bonds.
So amivantamab's chains (KEGG DRUG D11894), C6472H10046N1730O2023S46, with its 16
disulfides are C6472H10014N1730O2023S46, as KEGG gives them, 145,902.15 u average and
145,810.1054 u monoisotopic (KEGG's 145,900.22, by older atomic weights, and
145,810.1055). They're its chains' residues alone: amivantamab's major glycoform is
148,209 Da (FDA's chemistry review of Rybrevant, BLA 761210), its glycans' 2,307 u more.

A part that's a complex has disulfides of its own, on it: a Fab's, in a complex of the
Fab and its antigen. They're counted, each times its copies, with its own parts' (which
its composition counted in this project), as `part_disulfides`, a project's record's
value, as its parts' bonds are facts their registrars may correct; and
`part_bonds_complete` says whether every one was counted. The run says the complex with
all of its disulfides, its own and its parts'.

Its molar absorbance at 280 nm in water is Pace's (Pace et al., Protein Sci 4:2411-2423,
1995), as the protein plugin's is: 5,500 L/(mol.cm) for each tryptophan and 1,490 for each
tyrosine in its parts, each times its copies, with each cysteine's thiol free, as its
masses are; each disulfide makes a cystine, 125 more, which the run says with its bonds,
counting the disulfides it has, not every pair of cysteines. A part that's another
complex gives its own; one that's neither (a heme, an oligonucleotide) absorbs in ways
Pace's values don't weigh, so the complex has none, and the run says why.

Each complex gets one message, at most, whatever it says, so a run of the most complexes
the action packs (100) keeps to the most messages a run's output has (100): each says
everything about its complex, the longest shortened to fit, all of it in the run's log.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from weights import AVERAGE, RESIDUES, WATER

#: Each deoxynucleoside 5′-monophosphate's formula (the free acid), as Biopython weighs a
#: DNA strand's bases (Bio.Data.IUPACData.unambiguous_dna_weights).
DEOXYNUCLEOTIDES: dict[str, dict[str, int]] = {
    "A": {"C": 10, "H": 14, "N": 5, "O": 6, "P": 1},
    "C": {"C": 9, "H": 14, "N": 3, "O": 7, "P": 1},
    "G": {"C": 10, "H": 14, "N": 5, "O": 7, "P": 1},
    "T": {"C": 10, "H": 15, "N": 2, "O": 8, "P": 1},
}
COMPLEMENT = str.maketrans("ACGT", "TGCA")
#: Pace's molar absorbances at 280 nm in water, L/(mol.cm): a tryptophan's, a tyrosine's,
#: and a cystine's (two cysteines' disulfide).
TRYPTOPHAN, TYROSINE, CYSTINE = 5500, 1490, 125
#: Each element's most abundant isotope's mass (AME2020).
MONOISOTOPIC = {
    "C": 12.0, "H": 1.00782503223, "N": 14.00307400443, "O": 15.99491461957,
    "S": 31.9720711744, "P": 30.97376199842, "Se": 79.9165218, "F": 18.99840316273,
    "Cl": 34.968852682, "Br": 78.9183376, "I": 126.9044719, "Na": 22.989769282,
    "K": 38.9637064864, "Si": 27.97692653465, "B": 11.00930536, "Fe": 55.93493633,
    "Zn": 63.92914201, "Cu": 62.92959772, "Mg": 23.985041697, "Ca": 39.962590863,
    "Mn": 54.93804391, "Co": 58.93319429,
}  # fmt: skip
#: The other isotopes a labelled compound's atoms may be, by element and mass number,
#: with their masses (AME2020, as NIST's Atomic Weights and Isotopic Compositions gives
#: them): the stable labels, and tritium and carbon-14.
ISOTOPES: dict[tuple[str, int], float] = {
    ("H", 2): 2.01410177812, ("H", 3): 3.0160492779, ("C", 13): 13.00335483507,
    ("C", 14): 14.0032419884, ("N", 15): 15.00010889888, ("O", 17): 16.99913175650,
    ("O", 18): 17.99915961286, ("S", 33): 32.9714589098, ("S", 34): 33.967867004,
    ("S", 36): 35.96708071, ("Cl", 37): 36.965902602, ("Br", 81): 80.9162897,
    ("B", 10): 10.01293695, ("Se", 77): 76.919914, ("Fe", 54): 53.9396090,
    ("Fe", 57): 56.9353928, ("Mg", 25): 24.98583696, ("Mg", 26): 25.98259297,
    ("Si", 29): 28.97649466, ("Si", 30): 29.973770137, ("Zn", 67): 66.9271291,
    ("Cu", 65): 64.9277897, ("Ca", 44): 43.9554816, ("K", 41): 40.96182525,
    # Each element's most abundant isotope, written as a label: [12C], [14N] and so on.
    **{(e, round(m)): m for e, m in MONOISOTOPIC.items()},
}  # fmt: skip
#: How RDKit writes hydrogen's isotopes.
HYDROGENS = {"D": 2, "T": 3}
#: A part that's one chain, for its chain count: a protein's, a nucleic acid's, an
#: oligonucleotide's.
ONE_CHAIN = ("sequence", "bases", "helm")
#: A formula's atoms and counts, in Hill order or any: an element (C), or one of its
#: isotopes ([13C], or D and T for hydrogen's). A charge isn't one.
ATOM = re.compile(r"(?:\[([1-9][0-9]*)([A-Z][a-z]?)\]|([A-Z][a-z]?))([0-9]*)")
FORMULA = re.compile(r"^(?:(?:\[[1-9][0-9]*[A-Z][a-z]?\]|[A-Z][a-z]?)[0-9]*)+$")
#: The most a run's output keeps of one message's text, and of its messages.
MAX_TEXT = 2000
MAX_MESSAGES = 100
#: A message that's longer is cut at a sentence to fit, saying so.
CUT = " The rest is in the run's log."


class RefusedError(Exception):
    """What a run can't do, and why."""


def _label(entity: dict[str, Any]) -> str:
    return str(entity["values"].get("name") or entity.get("hrid") or entity["id"])


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else many or one + 's'}"


# --- Formulas ---------------------------------------------------------------------------------
#
# A formula is a Counter of its atoms: an element by its symbol ("C"), an isotope by its
# mass number and symbol ("13C", "2H" for deuterium).


def _element(atom: str) -> str:
    return atom.lstrip("0123456789")


def _mass_number(atom: str) -> int | None:
    digits = atom[: len(atom) - len(_element(atom))]
    return int(digits) if digits else None


def protein_formula(sequence: str) -> Counter[str]:
    """A protein's formula from its residues. Raises KeyError on a residue without one."""
    out: Counter[str] = Counter(WATER)
    for residue in sequence.removesuffix("*").upper():
        out.update(RESIDUES[residue])
    return out


def strand_formula(bases: str, *, circular: bool) -> Counter[str]:
    """A DNA strand's formula from its bases, 5′ to 3′: its nucleotides' less a water for
    each phosphodiester bond. Raises KeyError on a base without one."""
    out: Counter[str] = Counter()
    for base in bases:
        out.update(DEOXYNUCLEOTIDES[base])
    bonds = len(bases) if circular else len(bases) - 1
    out["H"] -= 2 * bonds
    out["O"] -= bonds
    return out


def parse(formula: str) -> Counter[str]:
    """A neutral formula's atoms. Raises ValueError for a formula that isn't one (a
    charged one), and KeyError for an element or isotope this doesn't weigh."""
    if not FORMULA.match(formula):
        raise ValueError(formula)
    out: Counter[str] = Counter()
    for number, labelled, element, count in ATOM.findall(formula):
        if labelled:
            if (labelled, int(number)) not in ISOTOPES:
                raise KeyError(f"[{number}{labelled}]")
            atom = f"{int(number)}{labelled}"
        elif element in HYDROGENS:
            atom = f"{HYDROGENS[element]}H"
        elif element in MONOISOTOPIC:
            atom = element
        else:
            raise KeyError(element)
        out[atom] += int(count) if count else 1
    return out


def _written(atom: str) -> str:
    number, element = _mass_number(atom), _element(atom)
    if number is None:
        return element
    if element == "H" and number in HYDROGENS.values():
        return next(k for k, v in HYDROGENS.items() if v == number)
    return f"[{number}{element}]"


def hill(formula: Counter[str]) -> str:
    """Hill order: C, then H, then the rest alphabetically; without carbon, all of them.
    Only atoms it has, each element's isotopes after it, lightest first, as RDKit writes
    them: C2[13C]H5DCl[37Cl]."""
    atoms = [a for a in formula if formula[a] > 0]
    elements = {_element(a) for a in atoms}
    first = [e for e in ("C", "H") if e in elements] if "C" in elements else []
    ordered = first + sorted(elements - set(first))
    out = []
    for element in ordered:
        mine = sorted(
            (a for a in atoms if _element(a) == element), key=lambda a: _mass_number(a) or 0
        )
        for atom in mine:
            n = formula[atom]
            out.append(f"{_written(atom)}{n if n != 1 else ''}")
    return "".join(out)


def mass(formula: Counter[str], table: dict[str, float]) -> float:
    """Its mass: each element's, from ``table``, and each isotope's own."""
    total = 0.0
    for atom, n in formula.items():
        number = _mass_number(atom)
        each = table[atom] if number is None else ISOTOPES[(_element(atom), number)]
        total += each * n
    return total


# --- Parts ------------------------------------------------------------------------------------


def is_a(record: dict[str, Any], type_name: str) -> bool:
    """Whether a run input's record is of a type or of a lab's subtype of it."""
    return type_name in record["types"]


def _part(part: dict[str, Any]) -> tuple[Counter[str], int, int | None, list[str]]:
    """A part's formula, how many chains it is, its molar absorbance at 280 nm with each
    cysteine's thiol free, or None if Pace's values don't give it, and what to say of how
    it was weighed. Raises :class:`RefusedError`."""
    values = part["values"]
    notes: list[str] = []
    if is_a(part, "iki/protein.Protein"):
        sequence = str(values["sequence"]).removesuffix("*").upper()
        try:
            formula = protein_formula(sequence)
        except KeyError as residue:
            raise RefusedError(
                f"{_label(part)} has {residue.args[0]!r}, an ambiguous or unknown residue, "
                "which has no one formula."
            ) from None
        if "J" in sequence:
            notes.append(
                f"{_label(part)}'s {_plural(sequence.count('J'), 'J')} (leucine or isoleucine) "
                "are weighed as either, as they have one formula."
            )
        absorbance = TRYPTOPHAN * sequence.count("W") + TYROSINE * sequence.count("Y")
        return formula, 1, absorbance, notes
    given = values.get("molecular_formula")
    if _by_bases(part):
        return (*_dna(part), None, notes)
    if not given:
        raise RefusedError(
            f"{_label(part)} has no molecular formula yet: compute it first, then this."
        )
    try:
        formula = parse(str(given))
    except ValueError:
        raise RefusedError(
            f"{_label(part)}'s formula, {given}, isn't a neutral one (a complex's parts are "
            "neutral here), so its mass in the complex isn't known."
        ) from None
    except KeyError as atom:
        raise RefusedError(
            f"{_label(part)}'s formula, {given}, has {atom.args[0]}, which this doesn't "
            "weigh, so its mass in the complex isn't known."
        ) from None
    if values.get("chain_count") is not None:
        chains = int(values["chain_count"])
    elif is_a(part, "iki/oligo.Duplex") or values.get("strandedness") == "double":
        chains = 2
    else:
        chains = int(any(values.get(k) for k in ONE_CHAIN))
    # Another complex's own, which this action computed; none for anything else.
    own = values.get("extinction_coefficient_reduced") if "components" in values else None
    return formula, chains, int(own) if own is not None else None, notes


def _by_bases(part: dict[str, Any]) -> bool:
    """Whether a part is weighed by its bases: a DNA sequence, which has no formula."""
    values = part["values"]
    return (
        not values.get("molecular_formula")
        and isinstance(values.get("bases"), str)
        and "strandedness" in values
    )


def _dna(part: dict[str, Any]) -> tuple[Counter[str], int]:
    """A DNA sequence's formula from its bases, and its strands. Raises
    :class:`RefusedError`."""
    values = part["values"]
    bases = str(values["bases"]).upper()
    circular = values.get("topology") == "circular"
    strands = [bases]
    if values.get("strandedness") == "double":
        strands.append(bases.translate(COMPLEMENT)[::-1])
    formula: Counter[str] = Counter()
    try:
        for strand in strands:
            formula.update(strand_formula(strand, circular=circular))
    except KeyError as base:
        raise RefusedError(
            f"{_label(part)} has {base.args[0]!r}, an ambiguous base, which has no one formula."
        ) from None
    return formula, len(strands)


def _disulfides(entity: dict[str, Any]) -> int:
    return sum(1 for b in entity["values"].get("bonds") or [] if b.get("kind") == "disulfide")


def _with(formula: Counter[str], absorbance: int | None, disulfides: int) -> str:
    """Its formula, masses and absorbance with ``disulfides`` disulfides: two hydrogens off
    and a cystine's absorbance on for each."""
    bonded = formula.copy()
    bonded["H"] -= 2 * disulfides
    text = (
        f"{hill(bonded)}, {mass(bonded, AVERAGE):,.2f} u average and "
        f"{mass(bonded, MONOISOTOPIC):,.4f} u monoisotopic"
    )
    if absorbance is not None:
        text += f", and {absorbance + CYSTINE * disulfides:,} L/(mol.cm) at 280 nm"
    return text


#: What a bond map that isn't complete is, as the run says it.
NOT_COMPLETE = {
    "incomplete": "don't include every bond it has",
    "predominant": "are only its predominant form's, while some of its molecules are bonded "
    "otherwise",
}


def composition(complex_: dict[str, Any]) -> dict[str, Any]:
    """A complex's update, and what its message says, with its level. Raises
    :class:`RefusedError`."""
    rows = complex_["values"].get("components") or []
    parts = (complex_.get("included") or {}).get("components.part") or []
    if len(parts) != len(rows):
        raise RefusedError("Its parts weren't given to the run.")
    formula: Counter[str] = Counter()
    chains = 0
    absorbance: int | None = 0
    # The parts that give it no absorbance: complexes without one, and the rest.
    unweighed: list[str] = []
    others: list[str] = []
    notes: list[str] = []
    warnings: list[str] = []
    # A part complex's disulfides, its own and its parts', each times its copies: they're
    # its bonds, not these, and they're counted as they are now, in this project.
    inner: list[tuple[str, int]] = []
    counted = True
    for row, part in zip(rows, parts, strict=True):
        each, count, absorbs, weighed = _part(part)
        notes += weighed
        copies = int(row.get("copies") or 1)
        for atom, n in each.items():
            formula[atom] += n * copies
        chains += count * copies
        if "components" in part["values"]:
            theirs = part["values"]
            own = _disulfides(part)
            below = theirs.get("part_disulfides")
            if below is None:
                counted = False
                warnings.append(
                    f"{_label(part)}'s own parts' disulfides weren't counted in this project: "
                    "compute its composition, then this"
                )
            mapped = theirs.get("bond_map")
            if mapped in NOT_COMPLETE:
                counted = False
                warnings.append(f"{_label(part)}'s bonds {NOT_COMPLETE[mapped]}")
            elif theirs.get("part_bonds_complete") is False:
                counted = False
                warnings.append(f"not all of {_label(part)}'s parts' disulfides are counted")
            total = (own + int(below or 0)) * copies
            if total:
                inner.append((_label(part), total))
        if absorbs is None:
            (unweighed if "components" in part["values"] else others).append(_label(part))
        elif absorbance is not None:
            absorbance += absorbs * copies
    if unweighed or others:
        absorbance = None
    part_disulfides = sum(n for _, n in inner)
    values: dict[str, Any] = {
        "molecular_formula": hill(formula),
        "molecular_weight": round(mass(formula, AVERAGE), 2),
        "monoisotopic_mass": round(mass(formula, MONOISOTOPIC), 4),
        "chain_count": chains,
        "part_disulfides": part_disulfides,
        "part_bonds_complete": counted,
    }
    if absorbance is not None:
        values["extinction_coefficient_reduced"] = absorbance
    said: list[str] = []
    if unweighed or others:
        why = []
        if unweighed:
            have = "has" if len(unweighed) == 1 else "have"
            why.append(
                f"{_listed(unweighed)} {have} none of {'its' if have == 'has' else 'their'} own"
            )
        if others:
            are = "isn't a protein" if len(others) == 1 else "aren't proteins"
            why.append(
                f"{_listed(others)} {are}, and Pace's values weigh only proteins' "
                "tryptophans, tyrosines and cystines"
            )
        said.append(f"Its molar absorbance at 280 nm isn't computed: {'; '.join(why)}.")
    disulfides = _disulfides(complex_)
    if disulfides:
        said.append(
            f"With its {_plural(disulfides, 'disulfide')}: "
            f"{_with(formula, absorbance, disulfides)}."
        )
    if inner:
        listed = _listed([f"{name}'s {n:,}" for name, n in inner])
        said.append(
            f"Its parts have {_plural(part_disulfides, 'disulfide')} of their own ({listed}), "
            "which are their bonds, shown on them. With those as well as its own: "
            f"{_with(formula, absorbance, disulfides + part_disulfides)}."
        )
    mapped = complex_["values"].get("bond_map")
    if mapped in NOT_COMPLETE:
        warnings.insert(0, f"its bonds {NOT_COMPLETE[mapped]}")
    if warnings:
        said.append(
            "What's worked out with its disulfides may count too few or too many: "
            + "; ".join(warnings)
            + "."
        )
    kinds = [
        b["kind"] for b in complex_["values"].get("bonds") or [] if b.get("kind") != "disulfide"
    ]
    if kinds:
        said.append(
            f"Its {', '.join(sorted(set(kinds)))} bonds aren't weighed: what each takes off "
            "depends on its chemistry."
        )
    said += notes
    if any(_by_bases(p) and p["values"].get("topology") != "circular" for p in parts):
        said.append(
            "Its linear DNA's strands are weighed with a 5′ phosphate and a 3′ hydroxyl, as "
            "Biopython weighs DNA: a strand made with a 5′ hydroxyl, as a synthetic or PCR-made "
            "one is, is HPO3 (79.98 u) lighter."
        )
    return {
        "update": {"entity": complex_["id"], "values": values},
        "level": "warning" if warnings else "info",
        "said": said,
    }


def message(entity: str, level: str, sentences: list[str]) -> dict[str, str]:
    """One message about ``entity``, of its sentences, cut at one to fit the output's limit
    if it's too long (the run's log has all of them)."""
    text = " ".join(sentences)
    if len(text) > MAX_TEXT:
        kept: list[str] = []
        for sentence in sentences:
            if len(" ".join([*kept, sentence])) + len(CUT) > MAX_TEXT:
                break
            kept.append(sentence)
        text = (
            (" ".join(kept) + CUT).strip() if kept else text[: MAX_TEXT - len(CUT) - 1] + "…" + CUT
        )
    return {"level": level, "text": text, "entity": entity}


def compute(run: dict[str, Any]) -> dict[str, Any]:
    """Each complex's composition. One that can't be computed is left out, with an error
    about it, which fails its run alone; a run that computes none fails, raising
    :class:`RefusedError`."""
    updates: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = []
    refused: list[str] = []
    for complex_ in run["inputs"]["complex"]:
        try:
            found = composition(complex_)
        except RefusedError as why:
            refused.append(f"{_label(complex_)}: {why}")
            print(f"{_label(complex_)}: {why}")
            messages.append(message(complex_["id"], "error", [str(why)]))
            continue
        updates.append(found["update"])
        values = found["update"]["values"]
        print(f"{_label(complex_)}: {values['molecular_formula']}, {values['molecular_weight']} u")
        for sentence in found["said"]:
            print(f"  {sentence}")
        if found["said"]:
            messages.append(message(complex_["id"], found["level"], found["said"]))
    if not updates:
        raise RefusedError("; ".join(refused))
    return {"updates": {"complex": updates}, "messages": messages[:MAX_MESSAGES]}


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

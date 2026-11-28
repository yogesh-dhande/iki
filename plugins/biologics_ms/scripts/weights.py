"""The atomic weights an Iki plugin weighs a molecule's average mass by.

One table, so the same formula weighs the same in every plugin: CIAAW's standard atomic
weights 2024 (Commission on Isotopic Abundances and Atomic Weights,
https://www.ciaaw.org/atomic-weights.htm, from the "Atomic Weights 2021" report, Prohaska
et al., Pure Appl. Chem. 94, 573, 2022, and its 2024 revisions). Fourteen elements' standard
atomic weights are intervals, since their isotopic composition in normal materials varies
(H, Li, B, C, N, O, Mg, Si, S, Cl, Ar, Br, Tl, Pb): for each, the single value CIAAW gives
in its abridged table, https://www.ciaaw.org/abridged-atomic-weights.htm (H 1.0080,
C 12.011, S 32.06). An element CIAAW gives none for, such as technetium, has no isotopic
composition in nature, so only a labelled isotope of it weighs.

The file is the same in each plugin that has it (a test checks they agree, and that the web
packages' copies do): change it in all of them, with an ADR, never in one.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Each element's standard atomic weight, or for an interval CIAAW's abridged value, g/mol.
AVERAGE: dict[str, float] = {
    "H": 1.0080, "He": 4.002602, "Li": 6.94, "Be": 9.0121831, "B": 10.81, "C": 12.011,
    "N": 14.007, "O": 15.999, "F": 18.998403162, "Ne": 20.1797, "Na": 22.98976928,
    "Mg": 24.305, "Al": 26.9815384, "Si": 28.085, "P": 30.973761998, "S": 32.06,
    "Cl": 35.45, "Ar": 39.95, "K": 39.0983, "Ca": 40.078, "Sc": 44.955907, "Ti": 47.867,
    "V": 50.9415, "Cr": 51.9961, "Mn": 54.938043, "Fe": 55.845, "Co": 58.933194,
    "Ni": 58.6934, "Cu": 63.546, "Zn": 65.38, "Ga": 69.723, "Ge": 72.630, "As": 74.921595,
    "Se": 78.971, "Br": 79.904, "Kr": 83.798, "Rb": 85.4678, "Sr": 87.62, "Y": 88.905838,
    "Zr": 91.222, "Nb": 92.90637, "Mo": 95.95, "Ru": 101.07, "Rh": 102.90549, "Pd": 106.42,
    "Ag": 107.8682, "Cd": 112.414, "In": 114.818, "Sn": 118.710, "Sb": 121.760,
    "Te": 127.60, "I": 126.90447, "Xe": 131.293, "Cs": 132.90545196, "Ba": 137.327,
    "La": 138.90547, "Ce": 140.116, "Pr": 140.90766, "Nd": 144.242, "Sm": 150.36,
    "Eu": 151.964, "Gd": 157.249, "Tb": 158.925354, "Dy": 162.500, "Ho": 164.930329,
    "Er": 167.259, "Tm": 168.934219, "Yb": 173.045, "Lu": 174.96669, "Hf": 178.486,
    "Ta": 180.94788, "W": 183.84, "Re": 186.207, "Os": 190.23, "Ir": 192.217, "Pt": 195.084,
    "Au": 196.966570, "Hg": 200.592, "Tl": 204.38, "Pb": 207.2, "Bi": 208.98040,
    "Th": 232.0377, "Pa": 231.03588, "U": 238.02891,
}  # fmt: skip

#: Each amino acid's residue in a chain (the amino acid less a water), as C, H, N, O, S, Se.
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
    # Leucine or isoleucine (NCBI's J), which have one formula.
    "J": {"C": 6, "H": 11, "N": 1, "O": 1},
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
#: The water a chain's ends add to its residues.
WATER = {"H": 2, "O": 1}


def chain_formula(sequence: str) -> dict[str, int]:
    """A protein chain's formula, from its residues (one letter each) and its ends' water.
    Raises KeyError, naming the residue, for one with no formula (an X, a B or a Z)."""
    out = dict(WATER)
    for residue in sequence:
        for element, n in RESIDUES[residue].items():
            out[element] = out.get(element, 0) + n
    return out


def average_mass(formula: Mapping[str, int]) -> float:
    """A formula's average mass, g/mol. Raises KeyError, naming the element, for one with
    no standard atomic weight."""
    return sum(AVERAGE[element] * n for element, n in formula.items())

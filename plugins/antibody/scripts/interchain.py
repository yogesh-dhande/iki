"""What joins an IgG's chains: the disulfides between them and within each of its domains,
read off where a chain's residues fall in EU numbering.

EU numbering (Edelman et al., PNAS 63:78-85, 1969) numbers a heavy chain's constant
region as the Eu myeloma protein's, an IgG1: CH1 from A118 to 215, the hinge 216 to 230,
CH2 231 to 340 and CH3 341 to 447. IMGT's IGHG1*01 (constant_regions.py) is that region
residue for residue, so a chain's residue has the EU number of the reference residue it
aligns with, whatever its subclass: an IgG4's hinge is three residues shorter, and its
residues there have no EU number of their own.

An IgG's chains are joined by disulfides between cysteines its subclass puts in the same
places (Liu and May, mAbs 4:17-23, 2012):

- IgG1: each light chain's last cysteine (a kappa's C214, its last residue; a lambda's
  next to last) to its heavy chain's C220, at the start of the hinge; and the two heavy
  chains to each other at C226 and C229.
- IgG4: each light chain's last cysteine to its heavy chain's C131, in CH1; and the heavy
  chains at C226 and C229, which an IgG4's S228P keeps from bonding within one chain.
- IgG2's disulfides between its heavy and light chains take more than one arrangement
  (its A, B and A/B isoforms), and IgG3's hinge has 11 between its heavy chains, so
  neither's are given here.

So trastuzumab, an IgG1, has H223-L214, H229-H'229 and H232-H'232, as KEGG DRUG's entry
D03257 lists them, and pembrolizumab, an IgG4, H134-L214, H226-H'226 and H229-H'229, as
D10574 does.

Each immunoglobulin domain has a disulfide of its own, between its two sheets (Liu and May,
2012): an IgG has 12, as KEGG DRUG's entries list them.

- A variable domain's joins IMGT's C23 and C104 (Lefranc et al., Dev Comp Immunol
  27:55-77, 2003), Kabat's H22 and H92, or L23 and L88. A domain whose only cysteines
  are those two has them; one with more (a VHH's CDR1-CDR3 disulfide, an unpaired
  cysteine in a CDR) has them where its numbering puts them, read from the scheme it was
  numbered in, and the rest are left out.
- An IgG heavy chain's constant domains' join EU C144 and C200 (CH1), C261 and C321
  (CH2), and C367 and C425 (CH3), which every subclass has.
- A light chain's constant domain's are its two cysteines before its last.

So trastuzumab's are KEGG's H22-H96, H147-H203, H264-H324, H370-H428, L23-L88 and
L134-L194.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import constant_regions

#: The IgG1 reference, CH1 to CH3, whose residues are EU 118 to 447.
EU_REFERENCE = "".join(s for _, s in constant_regions.REFERENCES["igg1"][3])
EU_FIRST = 118
#: The heavy chain's cysteine bonding its light chain, and those bonding the other heavy
#: chain, by subclass.
TO_LIGHT = {"igg1": 220, "igg4": 131}
TO_HEAVY = {"igg1": (226, 229), "igg4": (226, 229)}
#: The cysteines each IgG heavy chain constant domain's own disulfide joins (EU): CH1's,
#: CH2's and CH3's.
WITHIN_HEAVY = ((144, 200), (261, 321), (367, 425))
#: The cysteines a variable domain's own disulfide joins, by numbering scheme and chain.
WITHIN_VARIABLE = {
    "imgt": {"H": (23, 104), "K": (23, 104), "L": (23, 104)},
    "kabat": {"H": (22, 92), "K": (23, 88), "L": (23, 88)},
    "chothia": {"H": (22, 92), "K": (23, 88), "L": (23, 88)},
    "martin": {"H": (22, 92), "K": (23, 88), "L": (23, 88)},
}


def eu_numbers(constant: str) -> dict[int, int]:
    """Where each EU number falls in ``constant`` (a heavy chain's residues after its
    variable domain, or a whole Fc fusion chain), from 0: the residues aligned with the
    IgG1 reference, identical or not."""
    alignment = constant_regions._ALIGNER.align(constant, EU_REFERENCE)[0]
    out: dict[int, int] = {}
    for (q_start, q_end), (t_start, _) in zip(*alignment.aligned, strict=True):
        for offset in range(q_end - q_start):
            out[int(EU_FIRST + t_start + offset)] = int(q_start + offset)
    return out


@dataclass(frozen=True)
class Bond:
    """A disulfide: residue ``residue`` of copy ``copy`` of one chain to
    ``partner_residue`` of copy ``partner_copy`` of another (or the same) chain, each from
    1 along its sequence, as an ``iki/core.Complex``'s bonds have them."""

    part: str
    copy: int
    residue: int
    partner: str
    partner_copy: int
    partner_residue: int

    def row(self) -> dict[str, object]:
        return {"kind": "disulfide", **self.__dict__}


def cysteine(sequence: str, offset: int, eu: dict[int, int], number: int) -> int | None:
    """The residue (from 1, along the whole chain) at EU ``number``, if it's a cysteine."""
    at = eu.get(number)
    if at is None or sequence[offset + at] != "C":
        return None
    return offset + at + 1


def last_cysteine(sequence: str) -> int | None:
    """A light chain's cysteine that bonds its heavy chain: its last, from 1."""
    found = sequence.rfind("C")
    return found + 1 if found >= 0 else None


def heavy_cysteines(
    sequence: str, offset: int, isotype: str | None
) -> tuple[int | None, tuple[int, ...]] | None:
    """A heavy chain's cysteine bonding its light chain, and those bonding the other heavy
    chain, each from 1, read from its residues from ``offset`` on; None for a subclass
    whose aren't given (see the module's docstring)."""
    if isotype not in TO_LIGHT:
        return None
    eu = eu_numbers(sequence[offset:])
    light = cysteine(sequence, offset, eu, TO_LIGHT[isotype])
    heavy = tuple(found for n in TO_HEAVY[isotype] if (found := cysteine(sequence, offset, eu, n)))
    return light, heavy


def _cysteines(sequence: str, start: int, end: int) -> list[int]:
    """The cysteines from residue ``start`` to ``end`` (from 1, both included)."""
    return [i + 1 for i in range(start - 1, min(end, len(sequence))) if sequence[i] == "C"]


def _numbered(domain: dict[str, Any], number: int) -> int | None:
    """The residue (from 1) a domain's numbering numbers ``number``, without an insertion."""
    start = int(domain["start"])
    numbering: list[list[Any]] = domain.get("numbering") or []
    for index, (n, insertion, _) in enumerate(numbering):
        if int(n) == number and not insertion:
            return start + index
    return None


def variable_within(
    sequence: str, domains: list[dict[str, Any]], scheme: str | None
) -> list[tuple[int, int]]:
    """Each variable domain's own disulfide, as residues from 1 (see the module's
    docstring); none for a domain where it can't be told."""
    out: list[tuple[int, int]] = []
    for domain in domains:
        start, end = int(domain["start"]), int(domain["end"])
        found = _cysteines(sequence, start, end)
        if len(found) == 2:
            out.append((found[0], found[1]))
            continue
        pair = WITHIN_VARIABLE.get(scheme or "", {}).get(str(domain["chain"]))
        if pair is None:
            continue
        a, b = (_numbered(domain, n) for n in pair)
        if a is not None and b is not None and sequence[a - 1] == sequence[b - 1] == "C":
            out.append((a, b))
    return out


def heavy_within(sequence: str, offset: int, *, fc_only: bool = False) -> list[tuple[int, int]]:
    """An IgG heavy chain's constant domains' own disulfides, as residues from 1, read from
    its residues from ``offset`` on: those of its domains it has (a Fab's CH1 alone). With
    ``fc_only``, an Fc fusion's: CH2's and CH3's, as what's fused to its hinge may align
    with CH1 without being one."""
    eu = eu_numbers(sequence[offset:])
    out = []
    for first, second in WITHIN_HEAVY[1:] if fc_only else WITHIN_HEAVY:
        a, b = cysteine(sequence, offset, eu, first), cysteine(sequence, offset, eu, second)
        if a is not None and b is not None:
            out.append((a, b))
    return out


def light_within(sequence: str, offset: int) -> list[tuple[int, int]]:
    """A light chain's constant domain's own disulfide: its two cysteines after ``offset``
    (its VL's end) and before its last, which bonds the heavy chain."""
    found = _cysteines(sequence, offset + 1, len(sequence))[:-1]
    return [(found[0], found[1])] if len(found) == 2 else []


def within(chain: str, pairs: list[tuple[int, int]], copies: int) -> list[Bond]:
    """Disulfides within ``chain``, in each of its copies."""
    return [Bond(chain, n, a, chain, n, b) for n in range(1, copies + 1) for a, b in pairs]

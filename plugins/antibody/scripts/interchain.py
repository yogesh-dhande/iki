"""What joins an IgG's chains: the disulfides between them, and how two heavy chains are
made to pair, read off where a chain's residues fall in EU numbering.

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

So trastuzumab, an IgG1, has H223-L214, H229-H'229 and H232-H'232, and emicizumab, an
IgG4, H137-L214 and h133-L'214, H229-h225 and H232-h228, as KEGG DRUG's entries D03257
and D10821 list them.

Two different heavy chains are made to pair with each other, rather than each with
itself, by mutations in CH3, where the heavy chains meet (EU numbering):

- knobs into holes: T366W in one, the knob, and T366S, L368A and Y407V in the other, the
  hole (Ridgway et al., Protein Eng 9:617-621, 1996; Atwell et al., J Mol Biol
  270:26-35, 1997), as mosunetuzumab's;
- Fab-arm exchange: F405L in one and K409R in the other, which Genmab's DuoBody
  platform exchanges between two IgG1s (Labrijn et al., PNAS 110:5145-5150, 2013), as
  amivantamab's;
- charge pairs: a charge swapped at the interface, a lysine for an acid in one chain
  and an acid for a lysine in the other (Gunasekaran et al., J Biol Chem
  285:19637-19646, 2010), such as emicizumab's E356K and K439E (Sampei et al., PLoS ONE
  8:e57479, 2013).
"""

from __future__ import annotations

from dataclasses import dataclass

import constant_regions

#: The IgG1 reference, CH1 to CH3, whose residues are EU 118 to 447.
EU_REFERENCE = "".join(s for _, s in constant_regions.REFERENCES["igg1"][3])
EU_FIRST = 118
#: The heavy chain's cysteine bonding its light chain, and those bonding the other heavy
#: chain, by subclass.
TO_LIGHT = {"igg1": 220, "igg4": 131}
TO_HEAVY = {"igg1": (226, 229), "igg4": (226, 229)}
#: CH3's residues where two heavy chains are made to pair.
KNOB, HOLE = {366: "W"}, {366: "S", 368: "A", 407: "V"}
EXCHANGE = ({405: "L"}, {409: "R"})
CHARGE_POSITIONS = (356, 357, 370, 392, 399, 405, 409, 439)
BASIC, ACIDIC = frozenset("KR"), frozenset("DE")


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


def ch3(sequence: str, offset: int) -> dict[int, str]:
    """A heavy chain's residues at the EU numbers where heavy chains are made to pair."""
    eu = eu_numbers(sequence[offset:])
    wanted = {*KNOB, *HOLE, *CHARGE_POSITIONS}
    return {n: sequence[offset + eu[n]] for n in sorted(wanted) if n in eu}


def _has(residues: dict[int, str], wanted: dict[int, str]) -> bool:
    return all(residues.get(n) == r for n, r in wanted.items())


def reference_residue(isotype: str | None, number: int) -> str | None:
    """The residue at EU ``number`` of ``isotype``'s reference (IgG1's if it's none)."""
    if isotype is None or isotype not in constant_regions.REFERENCES:
        isotype = "igg1"
    sequence = "".join(s for _, s in constant_regions.REFERENCES[isotype][3])
    at = eu_numbers(sequence).get(number)
    return sequence[at] if at is not None else None


def fc_pairing(
    first: dict[int, str], second: dict[int, str], isotype: str | None = None
) -> tuple[str | None, str]:
    """How two heavy chains' CH3 residues (from :func:`ch3`) make them pair, and what says
    so: ``knobs_into_holes``, ``fab_arm_exchange`` or ``charge_pairs``, or None when
    nothing does. Mutations are named from ``isotype``'s reference residue."""
    for a, b in ((first, second), (second, first)):
        if _has(a, KNOB) and _has(b, HOLE):
            return (
                "knobs_into_holes",
                "T366W in one heavy chain and T366S, L368A and Y407V in the other",
            )
        if _has(a, EXCHANGE[0]) and _has(b, EXCHANGE[1]) and not _has(a, EXCHANGE[1]):
            return "fab_arm_exchange", "F405L in one heavy chain and K409R in the other"
    swaps = []
    for n in CHARGE_POSITIONS:
        x, y = first.get(n), second.get(n)
        if (
            x is None
            or y is None
            or not ((x in BASIC and y in ACIDIC) or (x in ACIDIC and y in BASIC))
        ):
            continue
        reference = reference_residue(isotype, n)
        mutated = next((r for r in (x, y) if r != reference), None)
        if reference is not None and mutated is not None:
            swaps.append(f"{reference}{n}{mutated}")
    if len(swaps) >= 2:
        return "charge_pairs", "charges swapped between them at " + ", ".join(swaps)
    return None, "no knobs into holes, Fab-arm exchange or charge pairs in their CH3 domains"

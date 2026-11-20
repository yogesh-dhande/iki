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

- IgG1: each light chain's interchain cysteine (a kappa's C214, its last residue; a
  lambda's homologous one, next to its last) to its heavy chain's C220, at the start of
  the hinge; and the two heavy chains to each other at C226 and C229.
- IgG4: each light chain's interchain cysteine to its heavy chain's C131, in CH1; and the
  heavy chains at C226 and C229. In a native IgG4, whose hinge has S228, some molecules
  bond C226 and C229 within each heavy chain instead (Angal et al., Mol Immunol
  30:105-108, 1993; Bloom et al., Protein Sci 6:407-415, 1997), so its bonds between
  heavy chains are its predominant form's; S228P keeps them between the chains.
- IgG2's disulfides between its heavy and light chains take more than one arrangement
  (its A, B and A/B isoforms), and IgG3's hinge has 11 between its heavy chains, so
  neither's are given here.

So trastuzumab, an IgG1, has H223-L214, H229-H'229 and H232-H'232, as KEGG DRUG's entry
D03257 lists them, and pembrolizumab, an IgG4, H134-L214, H226-H'226 and H229-H'229, as
D10574 does.

Each immunoglobulin domain has a disulfide of its own, between its two sheets (Liu and May,
2012): an IgG has 12, as KEGG DRUG's entries list them.

- A variable domain's joins IMGT's C23 and C104 (Lefranc et al., Dev Comp Immunol
  27:55-77, 2003), or a scheme's numbers for them, as the liability screen reads them
  (liabilities.DISULPHIDE): Kabat's, Chothia's and Martin's H22 and H92, or L23 and
  L88, and AHo's 23 and 106. They're read where the domain's numbering puts them,
  whatever other cysteines it has (a VHH's CDR1-CDR3 disulfide, an unpaired cysteine in
  a CDR), which are left out. Only a domain stored without its numbering is read off
  its cysteines, when it has just two.
- An IgG heavy chain's constant domains' join EU C144 and C200 (CH1), C261 and C321
  (CH2), and C367 and C425 (CH3), which every subclass has.
- A light chain's constant domain's are a kappa's C134 and C194 (Kabat), or a lambda's
  homologous two, read where its constant domain aligns with the human one's (UniProt
  P01834's IGKC, or P0DOY2's IGLC2), as its interchain cysteine is: so a chain without
  one of them, as one that stops short of C214 is, has the rest where they are.

A bond one of whose cysteines a chain hasn't, where its domains say it would be, isn't
given, and the run says which: so what's worked out from its bonds isn't taken for all
of them.

So trastuzumab's are KEGG's H22-H96, H147-H203, H264-H324, H370-H428, L23-L88 and
L134-L194.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import constant_regions
import liabilities

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
#: EU 228: S in a native IgG4's hinge, P in one stabilized by S228P.
HINGE_228 = 228
#: Human light chain constant domains, UniProt's IGKC (P01834) and IGLC2 (P0DOY2), by
#: the chain type numbering gives a light chain: each has three cysteines, its own
#: disulfide's two and the one that bonds the heavy chain (a kappa's C134, C194 and C214).
LIGHT_REFERENCES = {
    "K": "RTVAAPSVFIFPPSDEQLKSGTASVVCLLNNFYPREAKVQWKVDNALQSGNSQESVTEQDSKDSTYSLSSTLTLSKADYEKHKVY"
    "ACEVTHQGLSSPVTKSFNRGEC",
    "L": "GQPKAAPSVTLFPPSSEELQANKATLVCLISDFYPGAVTVAWKADSSPVKAGVETTTPSKQSNNKYAASSYLSLTPEQWKSHRS"
    "YSCQVTHEGSTVEKTVAPTECS",
}


def aligned(query: str, target: str) -> dict[int, int]:
    """Where each of ``target``'s residues falls in ``query``, both from 0: the residues
    aligned with each other, identical or not."""
    alignment = constant_regions._ALIGNER.align(query, target)[0]
    out: dict[int, int] = {}
    for (q_start, q_end), (t_start, _) in zip(*alignment.aligned, strict=True):
        for offset in range(q_end - q_start):
            out[int(t_start + offset)] = int(q_start + offset)
    return out


def eu_numbers(constant: str) -> dict[int, int]:
    """Where each EU number falls in ``constant`` (a heavy chain's residues after its
    variable domain, or a whole Fc fusion chain), from 0: the residues aligned with the
    IgG1 reference, identical or not."""
    return {EU_FIRST + t: q for t, q in aligned(constant, EU_REFERENCE).items()}


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


@dataclass(frozen=True)
class Light:
    """A light chain's constant domain's cysteines, each from 1 along the chain or None
    where it hasn't one: its own disulfide's two, and the one that bonds the heavy chain."""

    first: int | None
    second: int | None
    to_heavy: int | None

    @property
    def within(self) -> list[tuple[int, int]]:
        return [(self.first, self.second)] if self.first and self.second else []


def light_cysteines(sequence: str, offset: int, kind: str) -> Light:
    """A light chain's constant domain's cysteines, read from its residues after its
    variable domain (from ``offset``) where they align with the human constant domain of
    its type (``kind``, K or L, as numbering found it)."""
    reference = LIGHT_REFERENCES["L" if kind == "L" else "K"]
    where = aligned(sequence[offset:], reference)
    found = []
    for at in (i for i, residue in enumerate(reference) if residue == "C"):
        index = where.get(at)
        found.append(
            offset + index + 1 if index is not None and sequence[offset + index] == "C" else None
        )
    first, second, to_heavy = found
    return Light(first, second, to_heavy)


def hinge_228(sequence: str, offset: int) -> str | None:
    """The residue a heavy chain has at EU 228, from its residues from ``offset`` on."""
    at = eu_numbers(sequence[offset:]).get(HINGE_228)
    return sequence[offset + at] if at is not None else None


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
) -> tuple[list[tuple[int, int]], list[str]]:
    """Each variable domain's own disulfide, as residues from 1 (see the module's
    docstring), and the numbers of those a domain reaches but hasn't a cysteine at."""
    out: list[tuple[int, int]] = []
    missing: list[str] = []
    for domain in domains:
        numbering: list[list[Any]] = domain.get("numbering") or []
        if not numbering:
            # Stored without its numbering: its cysteines, if they're just the two.
            start, end = int(domain["start"]), int(domain["end"])
            found = _cysteines(sequence, start, end)
            if len(found) == 2:
                out.append((found[0], found[1]))
            continue
        side = "H" if domain["chain"] == "H" else "L"
        pair = liabilities.DISULPHIDE[scheme or "imgt"][side]
        a, b = (_numbered(domain, n) for n in pair)
        if a is not None and b is not None and sequence[a - 1] == sequence[b - 1] == "C":
            out.append((a, b))
        elif int(numbering[0][0]) <= pair[0] and pair[1] <= int(numbering[-1][0]):
            missing.append(f"{side}{pair[0]}-{side}{pair[1]} ({scheme or 'imgt'})")
    return out, missing


def heavy_within(
    sequence: str, offset: int, *, fc_only: bool = False
) -> tuple[list[tuple[int, int]], list[str]]:
    """An IgG heavy chain's constant domains' own disulfides, as residues from 1, read from
    its residues from ``offset`` on: those of its domains it has (a Fab's CH1 alone); and
    those of a domain it reaches whose cysteines it hasn't, by their EU numbers. With
    ``fc_only``, an Fc fusion's: CH2's and CH3's, as what's fused to its hinge may align
    with CH1 without being one."""
    eu = eu_numbers(sequence[offset:])
    out, missing = [], []
    for first, second in WITHIN_HEAVY[1:] if fc_only else WITHIN_HEAVY:
        a, b = cysteine(sequence, offset, eu, first), cysteine(sequence, offset, eu, second)
        if a is not None and b is not None:
            out.append((a, b))
        elif first in eu or second in eu:
            missing.append(f"C{first}-C{second} (EU)")
    return out, missing


def within(chain: str, pairs: list[tuple[int, int]], copies: int) -> list[Bond]:
    """Disulfides within ``chain``, in each of its copies."""
    return [Bond(chain, n, a, chain, n, b) for n in range(1, copies + 1) for a, b in pairs]

"""The pure-Python primer placement this repository ran before it was ported to Rust.

Kept verbatim, as the oracle the ported module is cross-checked against
(``test_plasmid_parity.py``, ADR 0200). Nothing but the tests imports it, and it is not
type-checked: it is a copy of a plugin script, which nothing type-checks either. Change it
only to keep a cross-check honest, never to change what Iki computes -- that lives in
``worker/crates/iki-wasm``.

What follows is the original module's own docstring.

Where a project's primers bind a DNA sequence, and the product a pair of them gives.

**Placing a primer.** A polymerase extends from a primer's 3' end, so that end has to
pair. A primer is placed by its 3'-most ``ANCHOR`` bases, on either strand, without gaps,
and binds where they pair well enough. "Well enough" is a penalty: a mismatch among the
3'-most ``THREE_PRIME`` bases costs ``NEAR_THREE_PRIME``, one further away costs
``ELSEWHERE``, and a site is kept where the total is at most the run's ``most_penalty``
(2 by default). So by default a primer binds where its anchor matches exactly, or with
one or two mismatches away from its 3' end, and never with one in its last five bases.
That weighting is what PCR does: a mismatch at or near the 3' terminus cuts extension
sharply, the more the nearer the end (Kwok et al., Nucleic Acids Res 18:999, 1990;
Stadhouders et al., J Mol Diagn 12:109, 2010), while mismatches toward the 5' end, and a
5' tail most of all, are carried into the product without stopping it. SnapGene places a
primer the same way: by a stretch of its 3' end and how well that pairs.

Beyond the anchor the primer goes on pairing toward its 5' end while its bases match; the
rest is its 5' tail, which the template hasn't got and the product has. IUPAC codes pair
with what they may be: a primer's N pairs with any base, its R with A or G.

A site may run across a circle's origin, and never across a linear molecule's ends
(ADR 0168): beyond an end there is nothing to pair with.

**Melting temperature** is SantaLucia's unified nearest-neighbour model (PNAS 95:1460,
1998), as ``Bio.SeqUtils.MeltingTemp.Tm_NN`` computes it with its default table
``DNA_NN3`` (Allawi & SantaLucia 1997, the same unified parameters) and SantaLucia's salt
correction (``saltcorr=5``), at 50 mM Na+ with no Mg2+ or dNTPs and 25 nM of each strand
-- the conditions ``iki/dna`` gives a primer's own Tm under, so the two agree. A site's Tm
is of its annealed bases against the template's own bases, so a mismatch lowers it
(Biopython's ``DNA_IMM1`` for an internal mismatch and ``DNA_TMM1`` for a terminal one);
where Biopython has no parameters for one, the Tm is left out rather than guessed.

**A product** comes of two sites facing each other: one on the forward strand extending
rightwards and one on the reverse extending leftwards, the first's 3' end at or before the
second's. Its bases are the forward primer whole, the template between the two 3' ends,
and the reverse primer's reverse complement whole -- so both 5' tails are in it, and so is
every base a primer mismatched, since the product is copied from the primer there. On a
circle each facing pair gives the product that runs from one to the other, across the
origin where that is the way round; on a linear molecule only a pair with the forward site
to the left gives one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from Bio.SeqUtils import MeltingTemp

COMPLEMENT = str.maketrans("ACGTRYKMBVDHNSWacgtrykmbvdhnsw", "TGCAYRMKVBHDNSWtgcayrmkvbhdnsw")
IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T", "U": "T", "R": "AG", "Y": "CT", "S": "CG",
    "W": "AT", "K": "GT", "M": "AC", "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG",
    "N": "ACGT",
}  # fmt: skip
#: The 3'-most bases that place a primer, and the fewest bases a primer is placed by.
ANCHOR = 15
SHORTEST = 8
#: Where a mismatch weighs more, and what one weighs there and elsewhere.
THREE_PRIME = 5
NEAR_THREE_PRIME = 3
ELSEWHERE = 1
MOST_PENALTY = 2
#: A polymerase's reach by default: a pair of sites further apart than this amplifies
#: nothing in the tube, so no product is worked out for it.
LONGEST_PRODUCT = 20_000
#: What a run keeps at most: sites of all the primers, and products of one reaction.
MOST_SITES = 500
MOST_PRODUCTS = 20
#: Biopython's Tm_NN under iki/dna's conditions (see the module's docstring).
CONDITIONS: dict[str, Any] = {
    "Na": 50, "K": 0, "Tris": 0, "Mg": 0, "dNTPs": 0, "dnac1": 25, "dnac2": 25, "saltcorr": 5,
}  # fmt: skip
CONDITIONS_TEXT = (
    "SantaLucia 1998 nearest-neighbour (Biopython Tm_NN, DNA_NN3), 50 mM Na+, no Mg2+ or "
    "dNTPs, 25 nM of each strand"
)


#: The sites are found by walking every place a primer's 3' end could sit, which costs a
#: run about a second for every 10 primers on a 100 kb molecule -- plasmid-sized work, which
#: is what this plugin's types hold. A genome-wide search would want the seed index the
#: annotation uses (``library.py``) rather than this scan.


class Refused(ValueError):  # noqa: N818 - reads as "the run refuses"
    """Why the primers can't be placed, or the product worked out, as asked."""


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def pairs(primer_base: str, template_base: str) -> bool:
    """Whether a primer's base may pair with the template's: every base the template's
    code may be is one the primer's may be. Nothing else says the primer pairs there."""
    may_be = set(IUPAC.get(template_base, ""))
    return bool(may_be) and may_be <= set(IUPAC.get(primer_base, ""))


def melting_temperature(primer: str, template: str | None = None) -> float | None:
    """°C for ``primer`` (5' to 3') on ``template``: the template's bases under it, 5' to 3'
    on the primer's own strand. None where a base isn't A, C, G or T, or Biopython has no
    parameters for a mismatch there."""
    primer = primer.upper()
    if len(primer) < 2 or set(primer) - set("ACGT"):
        return None
    try:
        if template is None or template.upper() == primer:
            value = MeltingTemp.Tm_NN(primer, **CONDITIONS)
        else:
            if len(template) != len(primer) or set(template.upper()) - set("ACGT"):
                return None
            # Tm_NN wants the other strand, base for base under the primer, 3' to 5'.
            value = MeltingTemp.Tm_NN(
                primer, c_seq=template.upper().translate(COMPLEMENT), **CONDITIONS
            )
    except (ValueError, KeyError, IndexError, ZeroDivisionError):
        return None
    return round(float(value), 1)


@dataclass(frozen=True)
class Site:
    """Where one primer binds. ``start`` and ``end`` (from 1) are the lowest and highest
    template bases its annealed part pairs with, an ``end`` before ``start`` crossing a
    circle's origin; ``three_prime`` is the base its 3' end pairs with, from which it
    extends -- rightwards on the forward strand, leftwards on the reverse."""

    hrid: str
    name: str
    bases: str
    strand: str
    start: int
    end: int
    three_prime: int
    annealed: int
    #: Each mismatch: how far its base is from the primer's 3' end (1 being the 3'-most),
    #: the primer's base, and the template's.
    mismatches: tuple[tuple[int, str, str], ...] = ()
    penalty: int = 0
    tm: float | None = None

    @property
    def tail(self) -> int:
        """Its 5' bases that don't pair with the template, and go into a product."""
        return len(self.bases) - self.annealed

    def value(self) -> dict[str, Any]:
        return {
            "primer": self.hrid,
            "name": self.name or self.hrid,
            "strand": self.strand,
            "start": self.start,
            "end": self.end,
            "three_prime": self.three_prime,
            "annealed": self.annealed,
            "tail": self.tail,
            "mismatches": [
                {"from_three_prime": away, "primer": mine, "template": theirs}
                for away, mine, theirs in self.mismatches
            ],
            **({"melting_temperature": self.tm} if self.tm is not None else {}),
        }


def _weight(away: int) -> int:
    """What a mismatch weighs, ``away`` bases from the primer's 3' end (1 is its last)."""
    return NEAR_THREE_PRIME if away <= THREE_PRIME else ELSEWHERE


def _template_at(bases: str, at: int, length: int, circular: bool) -> str | None:
    """The template's base at ``at`` (from 0), round a circle, or None past a linear end."""
    if 0 <= at < length:
        return bases[at]
    return bases[at % length] if circular else None


def find_sites(
    template: str,
    circular: bool,
    primers: list[dict[str, Any]],
    *,
    anchor: int = ANCHOR,
    most_penalty: int = MOST_PENALTY,
) -> list[Site]:
    """Every site the primers bind, on both strands, in order along the template.

    ``primers`` are dicts with ``hrid``, ``name`` and ``bases``. A primer is looked for on
    the forward strand (its bases are the forward strand's, so it reads rightwards) and on
    the reverse (its bases are the reverse strand's, reading leftwards).
    """
    template = template.upper().replace("U", "T")
    length = len(template)
    if not length:
        raise Refused("The template has no bases.")
    found: list[Site] = []
    for primer in primers:
        bases = (primer.get("bases") or "").upper().replace("U", "T")
        if len(bases) < SHORTEST:
            continue
        for strand in ("forward", "reverse"):
            # The primer's bases along the forward strand, 5' to 3' of that strand: a
            # reverse-strand primer reads as its reverse complement there.
            along = bases if strand == "forward" else reverse_complement(bases)
            # Where the primer's 3'-most base lies in `along`: its end, or its start.
            tip = len(along) - 1 if strand == "forward" else 0
            # Every place the primer's 3'-most base could sit (from 0). On a linear
            # molecule a site whose anchor would reach past an end is no site, which
            # `_site_at` finds when the template runs out.
            for offset in range(length):
                sites = _site_at(
                    template, length, circular, primer, bases, along, tip, offset,
                    anchor=anchor, most_penalty=most_penalty, strand=strand,
                )  # fmt: skip
                if sites is not None:
                    found.append(sites)
                if len(found) > MOST_SITES:
                    raise Refused(
                        f"The primers bind more than {MOST_SITES} places on a {length:,} bp "
                        "template. Allow fewer mismatches, or a longer 3' match."
                    )
    found.sort(key=lambda site: (site.start, site.end, site.strand, site.hrid))
    return found


def _site_at(
    template: str,
    length: int,
    circular: bool,
    primer: dict[str, Any],
    bases: str,
    along: str,
    tip: int,
    offset: int,
    *,
    anchor: int,
    most_penalty: int,
    strand: str,
) -> Site | None:
    """The site where `along`'s base `tip` sits at `offset` (from 0), or None if the
    primer's 3' end doesn't pair there well enough."""
    anchored = min(max(anchor, 1), len(bases))
    penalty = 0
    mismatches: list[tuple[int, str, str]] = []
    # From the 3'-most base of the primer back along it: `away` is 1 for its last base.
    for away in range(1, len(along) + 1):
        at = offset + (away - 1 if strand == "reverse" else 1 - away)
        there = _template_at(template, at, length, circular)
        mine = along[tip + (away - 1 if strand == "reverse" else 1 - away)]
        if there is None:
            # A linear molecule's end: the anchor must pair, and beyond it the primer's
            # remaining bases are a 5' tail hanging off the end, which a product keeps.
            if away <= anchored:
                return None
            break
        if pairs(mine, there):
            continue
        if away > anchored:
            break  # the pairing stops here: the rest of the primer is its 5' tail
        penalty += _weight(away)
        if penalty > most_penalty:
            return None
        # The primer's own base, 5' to 3' on its own strand, and the template's opposite it.
        own = bases[len(bases) - away]
        theirs = there if strand == "forward" else there.translate(COMPLEMENT)
        mismatches.append((away, own, theirs))
    else:
        away = len(along) + 1
    annealed = away - 1
    if annealed < anchored:
        return None
    # Where the annealed bases lie on the template, as the map counts them (from 1).
    if strand == "forward":
        first, last = offset - annealed + 1, offset
    else:
        first, last = offset, offset + annealed - 1
    start = first % length + 1
    end = last % length + 1
    annealed_bases = bases[len(bases) - annealed :]
    opposite = "".join(template[at % length] for at in range(first, last + 1))
    under = opposite if strand == "forward" else reverse_complement(opposite)
    return Site(
        hrid=primer["hrid"],
        name=primer.get("name") or primer["hrid"],
        bases=bases,
        strand=strand,
        start=start,
        end=end,
        three_prime=offset % length + 1,
        annealed=annealed,
        mismatches=tuple(reversed(mismatches)),
        penalty=penalty,
        tm=melting_temperature(annealed_bases, under),
    )


@dataclass(frozen=True)
class Product:
    """What a pair of facing sites amplifies: its bases, and where it lies on the
    template. ``template_start`` and ``template_end`` are the first and last template
    bases copied -- the two sites' annealed ends -- an end before a start crossing a
    circle's origin."""

    bases: str
    forward: Site
    reverse: Site
    template_start: int
    template_end: int

    @property
    def length(self) -> int:
        return len(self.bases)


def _distance(from_at: int, to_at: int, length: int, circular: bool) -> int | None:
    """The bases from ``from_at`` to ``to_at`` (both from 1, both counted), going along the
    forward strand, round a circle's origin where that is the way. None where a linear
    molecule's order rules it out."""
    if to_at >= from_at:
        return to_at - from_at + 1
    return length - from_at + 1 + to_at if circular else None


def products(
    template: str,
    circular: bool,
    sites: list[Site],
    *,
    longest: int,
) -> list[Product]:
    """Every product the sites give, shortest first.

    A forward site extends rightwards from its 3' end and a reverse site leftwards from
    its, so a pair makes a product when the forward site's 3' end is at or before the
    reverse site's, going along the forward strand. The product is the forward primer
    whole, the template between the two 3' ends, and the reverse primer's reverse
    complement whole: both 5' tails are in it, and so is every base either primer
    mismatched, since there the product is copied from the primer.
    """
    template = template.upper().replace("U", "T")
    length = len(template)
    made: list[Product] = []
    for ahead in (s for s in sites if s.strand == "forward"):
        for back in (s for s in sites if s.strand == "reverse"):
            # The template between the two primers' 3' ends, the ends themselves left to
            # the primers: the product has the primer's own bases there, mismatches and all.
            between = _distance(ahead.three_prime, back.three_prime, length, circular)
            if between is None or between < 2:
                continue
            middle_from = ahead.three_prime % length  # the base after the forward 3' end
            middle = "".join(template[(middle_from + i) % length] for i in range(between - 2))
            bases = ahead.bases + middle + reverse_complement(back.bases)
            if len(bases) > longest:
                continue
            start = (ahead.three_prime - ahead.annealed) % length + 1
            end = (back.three_prime + back.annealed - 2) % length + 1
            made.append(
                Product(
                    bases=bases,
                    forward=ahead,
                    reverse=back,
                    template_start=start,
                    template_end=end,
                )
            )
            if len(made) > MOST_PRODUCTS:
                raise Refused(
                    f"The primers make more than {MOST_PRODUCTS} products on this template. "
                    "Allow fewer mismatches, or a longer 3' match."
                )
    made.sort(key=lambda one: (len(one.bases), one.template_start))
    return made

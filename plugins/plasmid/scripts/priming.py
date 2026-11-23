"""Where a project's primers bind a DNA sequence, and the product a pair of them gives.

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
1998) at 50 mM Na+ with no Mg2+ or dNTPs and 25 nM of each strand -- the conditions
``iki/dna`` gives a primer's own Tm under, so the two agree. A site's Tm is of its
annealed bases against the template's own bases, so a mismatch lowers it (Allawi &
SantaLucia's internal mismatches and Bommarito's terminal ones); where no published
parameter covers a pair of neighbours, the Tm is left out rather than guessed.

**A product** comes of two sites facing each other: one on the forward strand extending
rightwards and one on the reverse extending leftwards, the first's 3' end at or before the
second's. Its bases are the forward primer whole, the template between the two 3' ends,
and the reverse primer's reverse complement whole -- so both 5' tails are in it, and so is
every base a primer mismatched, since the product is copied from the primer there. On a
circle each facing pair gives the product that runs from one to the other, across the
origin where that is the way round; on a linear molecule only a pair with the forward site
to the left gives one.

**Where the work happens.** All of it is the Rust in ``worker/crates/iki-wasm``, run from
the module this plugin's archive carries (ADR 0200), so the server, the worker and the web
app place a primer the same way. This module holds the shapes a run's output needs and
the conditions the records quote; the scientific rules above are the module's, and
Biopython is the oracle the tests cross-check it against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import molecular

COMPLEMENT = str.maketrans("ACGTRYKMBVDHNSWacgtrykmbvdhnsw", "TGCAYRMKVBHDNSWtgcayrmkvbhdnsw")
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
#: These conditions are what the temperatures here are computed at, and the text is shown
#: to whoever checks our numbers against their own, so it has to keep describing what the
#: code does. It says "25 nM of each strand" because that is what this path computes, for
#: every primer including a self-complementary one -- two species at 25 nM, with no
#: symmetry term. For a palindrome that is the wrong convention: SantaLucia 1998 has one
#: species at the whole concentration and the symmetry term applied, which the DNA plugin's
#: primer fields, the sequence viewer and ``thermodynamics::matched_melting_temperature``
#: all do, and which this path deliberately does not, because the parity oracle in
#: ``server/tests/oracles/priming_before.py`` pins it to the behaviour it was ported from.
#: ``GGATCC`` reads -13.7 here and -11.4 there.
#:
#: So do not change this text on its own: it would turn a true description of a flaw into a
#: false description of a fix. The arithmetic, this text, the Rust constant it mirrors in
#: ``worker/crates/iki-wasm/src/priming.rs``, the ``tm_method`` written onto records, the
#: fixtures in ``plugins/plasmid/tests/priming.yaml`` and the parity assertions move in one
#: commit, with the oracle keeping its old text and old reference.
#:
#: And do not reach for Biopython's ``selfcomp=True`` by itself: ``Tm_NN`` then reads
#: ``dnac1`` as the total, so with ``dnac1=25, dnac2=25`` it gives -13.758 for ``GGATCC``,
#: 0.04 degrees further from right. The flag and the concentration go together.
CONDITIONS_TEXT = (
    "SantaLucia 1998 nearest-neighbour (Biopython Tm_NN, DNA_NN3), 50 mM Na+, no Mg2+ or "
    "dNTPs, 25 nM of each strand"
)


class Refused(ValueError):  # noqa: N818 - reads as "the run refuses"
    """Why the primers can't be placed, or the product worked out, as asked."""


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def melting_temperature(primer: str, template: str | None = None) -> float | None:
    """°C for ``primer`` (5' to 3') on ``template``: the template's bases under it, 5' to 3'
    on the primer's own strand. None where a base isn't A, C, G or T, where there are
    fewer than two of them, or where no published parameter covers a pair of neighbours."""
    request: dict[str, Any] = {"primer": primer}
    if template is not None:
        request["template"] = template
    answer = molecular.call("iki_duplex_temperature", request)
    if "error" in answer:
        raise Refused(answer["error"])
    celsius: float | None = answer["celsius"]
    return celsius


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


def _sent(site: Site) -> dict[str, Any]:
    """One site as the module reads it back."""
    return {
        "primer": site.hrid,
        "name": site.name,
        "strand": site.strand,
        "start": site.start,
        "end": site.end,
        "three_prime": site.three_prime,
        "annealed": site.annealed,
        "tail": site.tail,
        "mismatches": [
            {"from_three_prime": away, "primer": mine, "template": theirs}
            for away, mine, theirs in site.mismatches
        ],
        "penalty": site.penalty,
        "melting_temperature": site.tm,
    }


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
    answer = molecular.call(
        "iki_place_primers",
        {
            "template": template,
            "circular": circular,
            "primers": [
                {
                    "hrid": primer["hrid"],
                    "name": primer.get("name") or "",
                    "bases": primer.get("bases") or "",
                }
                for primer in primers
            ],
            "anchor": anchor,
            "most_penalty": most_penalty,
        },
    )
    if "error" in answer:
        raise Refused(answer["error"])
    bases = {
        primer["hrid"]: (primer.get("bases") or "").upper().replace("U", "T") for primer in primers
    }
    return [
        Site(
            hrid=site["primer"],
            name=site["name"],
            bases=bases.get(site["primer"], ""),
            strand=site["strand"],
            start=site["start"],
            end=site["end"],
            three_prime=site["three_prime"],
            annealed=site["annealed"],
            mismatches=tuple(
                (one["from_three_prime"], one["primer"], one["template"])
                for one in site["mismatches"]
            ),
            penalty=site["penalty"],
            tm=site.get("melting_temperature"),
        )
        for site in answer["sites"]
    ]


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
    answer = molecular.call(
        "iki_pcr_products",
        {
            "template": template,
            "circular": circular,
            "sites": [_sent(site) for site in sites],
            "bases": [site.bases for site in sites],
            "longest": longest,
        },
    )
    if "error" in answer:
        raise Refused(answer["error"])
    return [
        Product(
            bases=product["bases"],
            forward=sites[product["forward"]],
            reverse=sites[product["reverse"]],
            template_start=product["template_start"],
            template_end=product["template_end"],
        )
        for product in answer["products"]
    ]

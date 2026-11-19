"""Working out what a cloning plan makes: restriction digests, ligation into a circle,
Gibson assembly by terminal homology, and whether what a plan makes is the molecule a
plasmid already has.

A **cut** is where an enzyme cuts the top strand, with the overhang it leaves: the
boundary index ``at`` (the cut lies between the bases ``at`` and ``at + 1``, counting
from 0) and ``overhang``, negative for a 5' overhang of that many bases, positive for a
3' one, zero for a blunt cut, as Biopython's restriction enzymes give it (``ovhg``). The
bottom strand is then cut ``at - overhang``.

A **fragment** is one piece of a digest, held as the top strand between two cuts, with
each end's overhang and the bases it's made of. Two ends ligate when their overhangs are
the same kind, the same length and the same bases: at that point the two top strands,
laid end to end, are a duplex again. So a ligation's product is its fragments' top
strands written one after another, and the overhangs only say which orders are possible.
Blunt ends ligate to any blunt end, which is why blunt cloning gives many products.

A **Gibson assembly** joins fragments that share their ends: the last bases of one are the
first bases of the next, which the exonuclease and polymerase make one. Here the shared
bases are counted once in the product.

Nothing here models partly double-stranded molecules beyond an end's overhang, methylation
or a star activity, and a ligation can't leave a nick: a plan whose ends don't pair
exactly makes nothing.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from Bio import Restriction
from Bio.Seq import Seq

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

COMPLEMENT = str.maketrans("ACGTRYKMBVDHNSWacgtrykmbvdhnsw", "TGCAYRMKVBHDNSWtgcayrmkvbhdnsw")

#: The most a search may try, so an ambiguous plan is answered rather than waited on.
MOST_PARTS = 6
MOST_FRAGMENTS = 24
MOST_ARRANGEMENTS = 200_000
#: Gibson assembly: the shortest overlap that joins two fragments, and the longest looked
#: for. NEB's protocol asks for 15 to 80 bases of homology at each end.
LEAST_OVERLAP = 15
MOST_OVERLAP = 200


class Impossible(Exception):  # noqa: N818 - reads as "the plan is impossible"
    """A plan that can't be worked out: an enzyme nobody knows, too many parts, a
    molecule that isn't DNA."""


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def least_rotation(bases: str) -> str:
    """The lexicographically least rotation of ``bases``, by Booth's algorithm, in linear
    time: a circular molecule read from the origin that sorts first (ADR 0138)."""
    if not bases:
        return bases
    doubled = bases + bases
    length = len(doubled)
    failure = [-1] * length
    start = 0
    for j in range(1, length):
        here = doubled[j]
        i = failure[j - start - 1]
        while i != -1 and here != doubled[start + i + 1]:
            if here < doubled[start + i + 1]:
                start = j - i - 1
            i = failure[i]
        if here != doubled[start + i + 1]:
            if here < doubled[start]:
                start = j
            failure[j - start] = -1
        else:
            failure[j - start] = i + 1
    return doubled[start : start + len(bases)]


def same_molecule(one: str, other: str) -> bool:
    """Whether two circular double-stranded sequences are the same molecule: the same
    bases from any origin, on either strand, as an identity reads them (ADR 0138)."""
    if len(one) != len(other):
        return False
    mine = min(least_rotation(one.upper()), least_rotation(reverse_complement(one.upper())))
    theirs = min(least_rotation(other.upper()), least_rotation(reverse_complement(other.upper())))
    return mine == theirs


@dataclass(frozen=True)
class End:
    """One end of a fragment: the overhang's kind and length, and its bases, read on the
    top strand. Two ends ligate when they're equal."""

    overhang: int
    bases: str

    @property
    def sticky(self) -> bool:
        return self.overhang != 0

    def flipped(self) -> End:
        """This end on the other strand: a fragment reverse-complemented keeps the kind
        of each overhang, with its bases complemented."""
        return End(self.overhang, reverse_complement(self.bases))

    def __str__(self) -> str:
        if not self.sticky:
            return "blunt"
        return f"{'5' if self.overhang < 0 else '3'}' {self.bases}"


@dataclass(frozen=True)
class Fragment:
    """A piece of a digest, or a whole linear molecule: its top strand, its two ends, and
    which part of the plan it came from."""

    part: str
    bases: str
    left: End
    right: End
    cut_with: tuple[str, ...] = ()

    def reversed(self) -> Fragment:
        return Fragment(
            part=self.part,
            bases=reverse_complement(self.bases),
            left=self.right.flipped(),
            right=self.left.flipped(),
            cut_with=self.cut_with,
        )


@dataclass
class Assembled:
    """What a plan makes: a circular top strand, and each junction in it."""

    bases: str
    junctions: list[dict[str, object]] = field(default_factory=list)


def enzyme(name: str) -> object:
    """The restriction enzyme called ``name``, from Biopython's REBASE-derived set."""
    found = getattr(Restriction, name, None)
    if found is None or not hasattr(found, "site"):
        raise Impossible(
            f"No restriction enzyme is called {name}. Write it as REBASE does, such as "
            "EcoRI, BamHI, BsaI or BsmBI."
        )
    return found


def cuts(bases: str, circular: bool, names: Sequence[str]) -> list[tuple[int, int, str]]:
    """Where ``names``'s enzymes cut ``bases``: each cut's boundary index (from 0), the
    overhang it leaves, and the enzyme that made it, in order along the molecule."""
    found: list[tuple[int, int, str]] = []
    sequence = Seq(bases.upper())
    for name in names:
        cutter = enzyme(name)
        at = cutter.search(sequence, linear=not circular)  # type: ignore[attr-defined]
        found.extend((position - 1, cutter.ovhg, name) for position in at)  # type: ignore[attr-defined]
    return sorted(set(found))


def digest(part: str, bases: str, circular: bool, names: Sequence[str]) -> list[Fragment]:
    """``bases`` cut by ``names``'s enzymes: each fragment, with its ends. An uncut
    circular molecule gives nothing (it can't go into a ligation); an uncut linear one
    gives itself, blunt at both ends."""
    bases = bases.upper()
    length = len(bases)
    made = cuts(bases, circular, names)
    if not made:
        return [] if circular else [Fragment(part, bases, End(0, ""), End(0, ""), ())]
    doubled = bases + bases
    ends = {
        at: End(o, doubled[at : at - o] if o < 0 else doubled[at + o : at]) for at, o, _ in made
    }
    by_at = {at: name for at, _, name in made}
    out: list[Fragment] = []
    if circular:
        for (left, _, _), (right, _, _) in zip(made, [*made[1:], made[0]], strict=True):
            stop = right if right > left else right + length
            cut_with = (by_at[left], by_at[right])
            out.append(Fragment(part, doubled[left:stop], ends[left], ends[right], cut_with))
    else:
        edges = [0, *[at for at, _, _ in made], length]
        for left, right in itertools.pairwise(edges):
            out.append(
                Fragment(
                    part,
                    bases[left:right],
                    ends.get(left, End(0, "")),
                    ends.get(right, End(0, "")),
                    tuple(by_at[at] for at in (left, right) if at in by_at),
                )
            )
    return [fragment for fragment in out if fragment.bases]


def _arrangements(
    choices: Sequence[Sequence[Fragment]],
) -> Iterator[tuple[Fragment, ...]]:
    """Each way of taking one fragment from each part and putting them in a circle: the
    first part first, the right way round, so one circle is offered once."""
    if len(choices) > MOST_PARTS:
        raise Impossible(
            f"{len(choices)} parts is more than {MOST_PARTS}, which is as many as "
            "an assembly is worked out from."
        )
    for part in choices:
        if len(part) > MOST_FRAGMENTS:
            raise Impossible(
                f"A part cuts into {len(part)} fragments, more than the {MOST_FRAGMENTS} an "
                "assembly is worked out from: digest it with fewer enzymes."
            )
    first, rest = choices[0], choices[1:]
    orders = list(itertools.permutations(range(len(rest))))
    flips = list(itertools.product((False, True), repeat=len(rest)))
    total = len(first) * len(orders) * len(flips)
    for part in rest:
        total *= max(len(part), 1)
    if total > MOST_ARRANGEMENTS:
        raise Impossible(
            f"The plan's parts and fragments make {total} arrangements, more than "
            f"{MOST_ARRANGEMENTS}: give fewer enzymes, or fewer parts."
        )
    for head in first:
        for order in orders:
            for picks in itertools.product(*[rest[i] for i in order]):
                for flip in flips:
                    turned = zip(picks, flip, strict=True)
                    yield (head, *(f.reversed() if turn else f for f, turn in turned))


def ligate(choices: Sequence[Sequence[Fragment]]) -> list[Assembled]:
    """Every circle one fragment of each part makes, ends ligated to matching ends. The
    same molecule found more than one way is given once."""
    made: dict[str, Assembled] = {}
    for arrangement in _arrangements(choices):
        joined = list(arrangement)
        if not all(
            one.right == other.left
            for one, other in zip(joined, [*joined[1:], joined[0]], strict=True)
        ):
            continue
        bases = "".join(fragment.bases for fragment in joined)
        key = min(least_rotation(bases), least_rotation(reverse_complement(bases)))
        if key in made:
            continue
        junctions = []
        offset = 0
        for one, other in zip(joined, [*joined[1:], joined[0]], strict=True):
            offset += len(one.bases)
            junctions.append(
                {
                    "at": offset % len(bases) + 1,
                    "from": one.part,
                    "to": other.part,
                    "overhang": str(one.right),
                    "enzymes": sorted({*one.cut_with, *other.cut_with}),
                }
            )
        made[key] = Assembled(bases=bases, junctions=junctions)
    return list(made.values())


def overlap(one: str, other: str, least: int, most: int) -> int:
    """The longest stretch at the end of ``one`` that is the start of ``other``, or 0."""
    for length in range(min(most, len(one), len(other)), least - 1, -1):
        if one[-length:] == other[:length]:
            return length
    return 0


def gibson(
    choices: Sequence[Sequence[Fragment]], least: int = LEAST_OVERLAP, most: int = MOST_OVERLAP
) -> list[Assembled]:
    """Every circle the parts make by sharing their ends, each shared stretch counted
    once. One molecule found more than one way is given once."""
    made: dict[str, Assembled] = {}
    for arrangement in _arrangements(choices):
        joined = list(arrangement)
        overlaps = [
            overlap(one.bases, other.bases, least, most)
            for one, other in zip(joined, [*joined[1:], joined[0]], strict=True)
        ]
        if not all(overlaps):
            continue
        bases = joined[0].bases
        # Where each shared stretch starts in the molecule being built: the last `shared`
        # bases of what's written so far, which the next fragment starts with.
        places: list[int] = []
        for fragment, shared in zip(joined[1:], overlaps[:-1], strict=True):
            places.append(len(bases) - shared)
            bases += fragment.bases[shared:]
        # The last fragment ends with the first's start, which closes the circle: that
        # stretch is the molecule's first bases, so the junction is at its origin.
        places.append(0)
        bases = bases[: len(bases) - overlaps[-1]]
        key = min(least_rotation(bases), least_rotation(reverse_complement(bases)))
        if key in made or not bases:
            continue
        junctions = [
            {
                "at": place % len(bases) + 1,
                "from": one.part,
                "to": other.part,
                "overlap": f"{shared} bp",
                "bases": other.bases[:shared],
            }
            for (one, other), shared, place in zip(
                zip(joined, [*joined[1:], joined[0]], strict=True),
                overlaps,
                places,
                strict=True,
            )
        ]
        made[key] = Assembled(bases=bases, junctions=junctions)
    return list(made.values())

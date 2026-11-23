"""Working out what a cloning plan makes: restriction digests, ligation into a circle,
Gibson assembly by terminal homology, and whether what a plan makes is the molecule a
plasmid already has.

A **cut** is where an enzyme cuts the top strand, with the overhang it leaves: the
boundary index ``at`` (the cut lies between the bases ``at`` and ``at + 1``, counting
from 0) and ``overhang``, negative for a 5' overhang of that many bases, positive for a
3' one, zero for a blunt cut, as Biopython's restriction enzymes give it (``ovhg``). The
bottom strand is then cut ``at - overhang``.

An **end**'s bases are the overhang's bases at that boundary, read along the top strand
of the molecule being cut, which is where Biopython's ``elucidate()`` puts them: a 5'
overhang lies after the top-strand cut (EcoRI is ``G^AATT_C``, so AATT), and a 3' one
before it (PstI is ``C_TGCA^G``, so TGCA). Read that way one stretch belongs to both of
the ends a cut makes, so two ends that came from the same kind of cut are equal, and two
ends whose overhangs differ -- PstI's TGCA against KpnI's GTAC -- are not.

A **fragment** is one piece of a digest, held as the top strand between two cuts, with
each end's overhang and the bases it's made of. Two ends ligate when their overhangs are
the same kind, the same length and the same bases: at that point the two top strands,
laid end to end, are a duplex again. So a ligation's product is its fragments' top
strands written one after another, and the overhangs only say which orders are possible.
Blunt ends ligate to any blunt end, which is why blunt cloning gives many products.

A **Gibson assembly** joins fragments that share their ends: the last bases of one are the
first bases of the next, which the exonuclease and polymerase make one. Here the shared
bases are counted once in the product.

What a plan makes is a **circle** where its parts close one, and a **linear molecule**
where they only join end to end: a Golden Gate or Gibson reaction whose outer ends don't
pair leaves a linear construct, and a digest leaves the band cut out of the gel. A circle
is read from any origin and on either strand, so one circle is one product however it is
written; a linear molecule has its own two ends, so its order matters and only reading it
from the other strand leaves it the same molecule (ADR 0138). Where the parts can close, the
circle is what the plan makes: that is what a ligase or an assembly master mix leaves to
transform.

Nothing here models partly double-stranded molecules beyond an end's overhang, methylation
or a star activity, and a ligation can't leave a nick: a plan whose ends don't pair
exactly makes nothing. A linear product's sequence is its top strand from the cut at one
end to the cut at the other, which is the reading that gives a digest band its length on
a gel; an overhang beyond those cuts lies on one strand only, and isn't kept, since a
linear duplex is read as fully paired (ADR 0138).

**Where the work happens.** The cutting itself -- recognising a site with its IUPAC
ambiguity codes, on both strands and round a circle's origin, and working out each cut's
overhang, the fragments and the gel bands -- is the Rust in ``worker/crates/iki-wasm``,
run from the module this plugin's archive carries (ADR 0200), so the server, the worker
and the web app cut a molecule the same way. The enzymes themselves stay in the
Biopython this plugin already depends on: their sites and cut offsets are REBASE's facts,
and REBASE's terms are not a licence Iki can redistribute them under (ADR 0200), so each
enzyme's description is read from Biopython here and passed to the module with the
request. Ligation and assembly are still worked out below, on the fragments it gives.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import molecular
from Bio import Restriction

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


def canonical(bases: str, circular: bool) -> str:
    """A double-stranded molecule as its identity reads it (ADR 0138): a circle from the
    origin that sorts first, on whichever strand sorts first; a linear molecule from
    whichever of its two strands sorts first, since both are the molecule read from its
    other end, and a rotation of it is another molecule."""
    bases = bases.upper()
    other = reverse_complement(bases)
    if not circular:
        return min(bases, other)
    return min(least_rotation(bases), least_rotation(other))


def same_molecule(one: str, other: str, *, circular: bool = True) -> bool:
    """Whether two double-stranded sequences of the same topology are the same molecule:
    a circle from any origin and on either strand, a linear molecule from either strand
    (ADR 0138)."""
    if len(one) != len(other):
        return False
    return canonical(one, circular) == canonical(other, circular)


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
    which part of the plan it came from.

    ``bases`` runs from the cut in the top strand on the left to the cut in the top strand
    on the right, so writing neighbouring fragments one after another spells the molecule
    again. The other strand's stretch is not the same one: it begins where a 5' overhang
    on the left ends and runs on through a 5' overhang on the right, and the other way
    round for 3' overhangs. ``other_strand`` is that stretch, which is what this fragment
    offers as its top strand when it goes in the other way round.
    """

    part: str
    bases: str
    left: End
    right: End
    cut_with: tuple[str, ...] = ()

    def other_strand(self) -> str:
        """The strand ``bases`` is paired with, read along the top strand (so not yet
        complemented). The ends' bases say what lies outside ``bases``, which is how this
        can be read without the molecule the fragment was cut from."""
        before = self.left.bases if self.left.overhang > 0 else ""
        after = self.right.bases if self.right.overhang < 0 else ""
        start = max(-self.left.overhang, 0)
        stop = len(self.bases) - max(self.right.overhang, 0)
        return before + self.bases[start:stop] + after

    def reversed(self) -> Fragment:
        """This fragment the other way round: what was the other strand read as the top
        one, and each end on the other strand."""
        return Fragment(
            part=self.part,
            bases=reverse_complement(self.other_strand()),
            left=self.right.flipped(),
            right=self.left.flipped(),
            cut_with=self.cut_with,
        )


@dataclass
class Assembled:
    """What a plan makes: its top strand, whether that molecule is a circle or has two
    ends, and each junction in it (a circle has one per part, a linear molecule one
    fewer)."""

    bases: str
    junctions: list[dict[str, object]] = field(default_factory=list)
    topology: str = "circular"

    @property
    def circular(self) -> bool:
        return self.topology == "circular"


def enzyme(name: str) -> object:
    """The restriction enzyme called ``name``, from Biopython's REBASE-derived set."""
    found = getattr(Restriction, name, None)
    if found is None or not hasattr(found, "site"):
        raise Impossible(
            f"No restriction enzyme is called {name}. Write it as REBASE does, such as "
            "EcoRI, BamHI, BsaI or BsmBI."
        )
    return found


def described(name: str) -> dict[str, object]:
    """One enzyme as the module reads it: its recognition site and where it cuts each
    strand, from the Biopython this plugin depends on. Whether the site reads the same on
    both strands, and whether the enzyme cuts each strand twice, are Biopython's own
    classification of it rather than fields of REBASE's table."""
    cutter = enzyme(name)
    return {
        "name": name,
        "site": cutter.site,  # type: ignore[attr-defined]
        "size": cutter.size,  # type: ignore[attr-defined]
        "fst5": cutter.fst5,  # type: ignore[attr-defined]
        "fst3": cutter.fst3,  # type: ignore[attr-defined]
        "scd5": cutter.scd5,  # type: ignore[attr-defined]
        "scd3": cutter.scd3,  # type: ignore[attr-defined]
        "ovhg": cutter.ovhg,  # type: ignore[attr-defined]
        "palindromic": bool(cutter.is_palindromic()),  # type: ignore[attr-defined]
        "two_cuts": bool(cutter.cut_twice()),  # type: ignore[attr-defined]
    }


def _cut(
    bases: str, circular: bool, names: Sequence[str], *, part: str = "", want_bands: bool = False
) -> dict[str, Any]:
    """The module's answer for one molecule, or the refusal it gives."""
    answer = molecular.call(
        "iki_digest",
        {
            "part": part,
            "bases": bases.upper(),
            "circular": circular,
            "enzymes": [described(name) for name in names],
            "want_bands": want_bands,
        },
    )
    if "error" in answer:
        raise Impossible(answer["error"])
    found: dict[str, Any] = answer
    return found


def _end(given: dict[str, Any]) -> End:
    return End(given["overhang"], given["bases"])


def cuts(
    bases: str, circular: bool, names: Sequence[str]
) -> list[tuple[int, int, tuple[str, ...]]]:
    """Where ``names``'s enzymes cut ``bases``: each cut's boundary index (from 0), the
    overhang it leaves, and every enzyme that makes that cut, in order along the molecule.

    A cut is one place where the strands are broken, not one enzyme's work: isoschizomers
    such as SacI and SstI, or BsaI and Eco31I, recognise the same site and break the same
    bonds, so a digest with both gives the fragments either one gives alone, with both
    names recorded against each boundary.
    """
    found = _cut(bases, circular, names)
    return [(one["at"], one["overhang"], tuple(one["made_by"])) for one in found["cuts"]]


def end_at(at: int, overhang: int, bases: str, circular: bool) -> End:
    """The end a cut at boundary ``at`` leaves, read on the top strand.

    A 5' overhang's bases are the ``overhang`` bases just after the top-strand cut, a 3'
    one's the bases just before it. Either stretch can cross a circular molecule's origin,
    so both are read from the doubled sequence.
    """
    if overhang == 0:
        return End(0, "")
    length = len(bases)
    start = at if overhang < 0 else at - overhang
    start = start % length if circular else max(start, 0)
    return End(overhang, (bases + bases)[start : start + abs(overhang)])


def digest(part: str, bases: str, circular: bool, names: Sequence[str]) -> list[Fragment]:
    """``bases`` cut by ``names``'s enzymes: each fragment, with its ends. An uncut
    circular molecule gives nothing (it can't go into a ligation); an uncut linear one
    gives itself, blunt at both ends."""
    found = _cut(bases, circular, names, part=part)
    return [
        Fragment(
            part=one["part"],
            bases=one["bases"],
            left=_end(one["left"]),
            right=_end(one["right"]),
            cut_with=tuple(one["cut_with"]),
        )
        for one in found["fragments"]
    ]


def _within_limits(choices: Sequence[Sequence[Fragment]], total: int) -> None:
    """That a plan is small enough to be worked out, so an impossible one is answered
    rather than waited on."""
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
    if total > MOST_ARRANGEMENTS:
        raise Impossible(
            f"The plan's parts and fragments make {total} arrangements, more than "
            f"{MOST_ARRANGEMENTS}: give fewer enzymes, or fewer parts."
        )


def _arrangements(
    choices: Sequence[Sequence[Fragment]],
) -> Iterator[tuple[Fragment, ...]]:
    """Each way of taking one fragment from each part and putting them in a circle: the
    first part first, the right way round, so one circle is offered once."""
    first, rest = choices[0], choices[1:]
    orders = list(itertools.permutations(range(len(rest))))
    flips = list(itertools.product((False, True), repeat=len(rest)))
    total = len(first) * len(orders) * len(flips)
    for part in rest:
        total *= max(len(part), 1)
    _within_limits(choices, total)
    for head in first:
        for order in orders:
            for picks in itertools.product(*[rest[i] for i in order]):
                for flip in flips:
                    turned = zip(picks, flip, strict=True)
                    yield (head, *(f.reversed() if turn else f for f, turn in turned))


def _chains(
    choices: Sequence[Sequence[Fragment]],
) -> Iterator[tuple[Fragment, ...]]:
    """Each way of taking one fragment from each part and laying them end to end: every
    order and every orientation.

    A circle has no first part and no front, so ``_arrangements`` fixes both. A linear
    molecule has two ends of its own, so which part is at which end matters and every
    part may be turned round. The whole chain turned round is the same molecule read from
    its other end, so of those two readings only one is offered -- the one whose parts
    come in the lesser order, unturned where that settles nothing -- since a chain and
    its reverse would otherwise each be written along its own strand, and the two
    spellings differ by the overhangs at its ends, which a duplex read as fully paired
    doesn't keep (ADR 0138).
    """
    orders = list(itertools.permutations(range(len(choices))))
    flips = list(itertools.product((False, True), repeat=len(choices)))
    total = len(orders) * len(flips)
    for part in choices:
        total *= max(len(part), 1)
    _within_limits(choices, total)
    for order in orders:
        for flip in flips:
            laid = tuple(zip(order, flip, strict=True))
            turned_round = tuple((i, not turn) for i, turn in reversed(laid))
            if laid > turned_round:
                continue
            for picks in itertools.product(*[choices[i] for i in order]):
                turned = zip(picks, flip, strict=True)
                yield tuple(f.reversed() if turn else f for f, turn in turned)


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


def ligate_in_a_line(choices: Sequence[Sequence[Fragment]]) -> list[Assembled]:
    """Every linear molecule one fragment of each part makes, laid end to end with each
    inner pair of ends ligated and the two outer ends left as they are.

    This is what a ligation leaves when its parts can't close: a Golden Gate construct
    whose outer overhangs don't pair, or a vector and an insert that join at one end
    only. The same molecule read from its other strand is given once.
    """
    made: dict[str, Assembled] = {}
    for arrangement in _chains(choices):
        joined = list(arrangement)
        if not all(one.right == other.left for one, other in itertools.pairwise(joined)):
            continue
        bases = "".join(fragment.bases for fragment in joined)
        key = canonical(bases, circular=False)
        if key in made or not bases:
            continue
        junctions = []
        offset = 0
        for one, other in itertools.pairwise(joined):
            offset += len(one.bases)
            junctions.append(
                {
                    "at": offset + 1,
                    "from": one.part,
                    "to": other.part,
                    "overhang": str(one.right),
                    "enzymes": sorted({*one.cut_with, *other.cut_with}),
                }
            )
        made[key] = Assembled(bases=bases, junctions=junctions, topology="linear")
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


def gibson_in_a_line(
    choices: Sequence[Sequence[Fragment]], least: int = LEAST_OVERLAP, most: int = MOST_OVERLAP
) -> list[Assembled]:
    """Every linear molecule the parts make by sharing their ends, each shared stretch
    counted once and the two outer ends sharing nothing with anything: a Gibson or Golden
    Gate assembly of fragments into a linear construct. One molecule found more than one
    way is given once."""
    made: dict[str, Assembled] = {}
    for arrangement in _chains(choices):
        joined = list(arrangement)
        overlaps = [
            overlap(one.bases, other.bases, least, most)
            for one, other in itertools.pairwise(joined)
        ]
        if not all(overlaps):
            continue
        bases = joined[0].bases
        # Where each shared stretch starts in the molecule being built: the last `shared`
        # bases of what's written so far, which the next fragment starts with.
        places: list[int] = []
        for fragment, shared in zip(joined[1:], overlaps, strict=True):
            places.append(len(bases) - shared)
            bases += fragment.bases[shared:]
        key = canonical(bases, circular=False)
        if key in made or not bases:
            continue
        junctions = [
            {
                "at": place + 1,
                "from": one.part,
                "to": other.part,
                "overlap": f"{shared} bp",
                "bases": other.bases[:shared],
            }
            for (one, other), shared, place in zip(
                itertools.pairwise(joined), overlaps, places, strict=True
            )
        ]
        made[key] = Assembled(bases=bases, junctions=junctions, topology="linear")
    return list(made.values())


@dataclass(frozen=True)
class Band:
    """One fragment of a virtual digest, as a gel and a map read it: its length, where it
    lies on the molecule's top strand (from 1; on a circle, a band across the origin
    ends before it starts), and what made each of its ends."""

    length: int
    start: int
    end: int
    left: str
    right: str


#: What a linear molecule's own end is called: no enzyme made it.
MOLECULE_END = "End of the molecule"


def bands(bases: str, circular: bool, names: Sequence[str]) -> list[Band]:
    """``bases`` cut by ``names``'s enzymes, each fragment as a band, largest first, as a
    gel reads from its wells and as a marker's sizes are listed; bands of one length in
    the order they lie along the molecule. An uncut circle gives none: it stays a circle.
    An uncut linear molecule is one band, itself."""
    found = _cut(bases, circular, names, want_bands=True)
    return [
        Band(one["length"], one["start"], one["end"], one["left"], one["right"])
        for one in found["bands"]
    ]

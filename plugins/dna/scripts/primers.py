"""Primers that make a stated change, designed by a published protocol (ADR 0204).

Three protocols, which is what the mutagenesis kits and the design tools offer:

**Overlapping primers** -- the QuikChange design: a pair of primers complementary to each
other, both carrying the change, with correct template on both sides of it. The whole
plasmid is amplified from them, the methylated template is digested with DpnI and the
nicked product is transformed (Braman, Papworth and Greener, *Methods Mol Biol* 57:31,
1996). The rules are the kit manual's (*QuikChange II Site-Directed Mutagenesis Kit
Instruction Manual*, Agilent, Cat. #200523, Revision E1): 25 to 45 bases long, the change
near the centre and at least 10 bases from either end, at least 40% GC, terminating in one
or more C or G at the 3' end, and a melting temperature of at least 78 °C by the manual's
own formula.

**Partially overlapping primers** -- Zheng, Baumann and Reymond (*Nucleic Acids Res*
32:e115, 2004) and Liu and Naismith (*BMC Biotechnol* 8:91, 2008): the two primers share
the stretch that carries the change at their 5' ends and each has bases of its own at its
3' end. Two primers that are wholly complementary anneal to one another as readily as to
the template, which limits how much of each cycle is amplification; primers whose 3' ends
are their own can only extend on the template. Zheng's rules are counted -- at least eight
non-overlapping bases at each 3' terminus, the change in both primers, and at least one G
or C at each end -- and Liu and Naismith's is a temperature: the non-overlapping part
anneals 5 to 10 °C above the part the two primers share, both by Wetmur's formula
(*Crit Rev Biochem Mol Biol* 26:227, 1991), which is the formula their paper uses.

**Back-to-back primers** -- Hemsley, Arnheim, Toney, Cortopassi and Galas (*Nucleic Acids
Res* 17:6545, 1989): the plasmid is amplified outwards by two primers that lie back to
back on opposite strands, 5' ends apposing, and the linear product is ligated blunt. The
change is carried on the 5' end of one primer, so it lands at the join; a deletion needs
no tail at all, since the primers simply start outside the bases that are to go. Hemsley
phosphorylated the product after amplification; the kits built on the method either
phosphorylate the primers or phosphorylate, circularise and digest the template in one
step, so the record says the product must be phosphorylated and leaves which way to the
lab.

Melting temperatures reported for a primer are Iki's own, the ones every `iki/dna.Primer`
carries: SantaLucia's unified nearest-neighbour parameters (*PNAS* 95:1460, 1998) at
50 mM Na+ and 50 nM of strands in all, computed by `scripts/sequences.py`, so a primer
designed here and a primer registered from a vendor's tube are measured the same way. The
two protocol formulas above are different things -- empirical rules those protocols are
written around -- so each is computed under its own name and reported as such, rather than
passed off as the primer's melting temperature.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sequences import gc_content, melting_temperature, reverse_complement


class Impossible(ValueError):  # noqa: N818 - reads as "the primer is impossible"
    """A primer the rules asked for that can't be built, said in words a lab can act on."""


@dataclass(frozen=True)
class Rules:
    """What a primer must be. The defaults are the protocols' own published ones."""

    protocol: str = "overlapping"
    #: The shortest and longest primer, in bases: the QuikChange manual's 25 to 45.
    shortest: int = 25
    longest: int = 45
    #: Correct template either side of the change, for an overlapping primer: the manual
    #: asks for the change to be at least 10 bases from either end.
    flank: int = 10
    #: The least GC share a primer may have, as the QuikChange manual asks.
    min_gc: float = 40.0
    #: The QuikChange acceptance temperature, by the manual's own formula.
    quikchange_tm: float = 78.0
    #: Zheng's least non-overlapping bases at each 3' terminus.
    least_own: int = 8
    #: Zheng's least bases between the change and a primer's own 5' terminus, which is
    #: each end of the stretch the two primers share.
    least_edge: int = 4
    #: Liu and Naismith's gap: the non-overlapping part anneals this much above the part
    #: the two primers share.
    gap: tuple[float, float] = (5.0, 10.0)
    #: What an outward primer anneals at, by SantaLucia's model, and the least of it that
    #: must match the template (the Q5 kit manual's ten bases).
    anneal_tm: float = 60.0
    least_match: int = 10
    #: A primer ends in G or C, so its 3' end is held while the polymerase starts.
    gc_clamp: bool = True
    #: The potassium a Wetmur temperature is computed at, in mol/L.
    potassium: float = 0.05


@dataclass(frozen=True)
class Designed:
    """One primer: its bases 5' to 3', which way it points, how much of it matches the
    template, and what the protocol's own measure of it says."""

    role: str
    bases: str
    direction: str
    matched: int
    tail: int = 0
    measures: tuple[tuple[str, float], ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def length(self) -> int:
        return len(self.bases)


def quikchange_tm(bases: str, mismatched: int, indel: bool) -> float:
    """The QuikChange manual's acceptance temperature.

    For a substitution: Tm = 81.5 + 0.41(%GC) - 675/N - %mismatch, N the primer's length.
    For an insertion or a deletion the manual gives Tm = 81.5 + 0.41(%GC) - 675/N with no
    mismatch term, "where N does not include the bases which are being inserted or
    deleted" -- so `mismatched` is taken off N instead of charged as mismatch.

    It is an empirical formula the kit's protocol is written around, not a
    nearest-neighbour model, which is why it is reported under its own name.
    """
    gc = gc_content(bases) or 0.0
    length = len(bases) - mismatched if indel else len(bases)
    if length <= 0:
        raise Impossible("A primer of nothing but the bases being inserted anneals nowhere.")
    found = 81.5 + 0.41 * gc - 675 / length
    return found if indel else found - 100 * mismatched / len(bases)


def wetmur_tm(bases: str, potassium: float, mismatched: int = 0) -> float:
    """Wetmur's formula, which is the one Liu and Naismith's protocol is written around:
    Tm = 81.5 + 16.6 log([K+]/(1 + 0.7[K+])) + 0.41(%GC) - 500/length - 1.0(%mismatch)
    (*Crit Rev Biochem Mol Biol* 26:227, 1991). The potassium is stated, since the
    formula's first term is nothing but the salt."""
    gc = gc_content(bases) or 0.0
    salt = 16.6 * math.log10(potassium / (1 + 0.7 * potassium))
    return 81.5 + salt + 0.41 * gc - 500 / len(bases) - 100 * mismatched / len(bases)


def _clamped(bases: str) -> bool:
    """Whether the primer's 3' end is a G or a C, which holds it while the polymerase
    starts."""
    return bases[-1].upper() in "GC"


def _read_round(mutant: str, start: int, circular: bool) -> tuple[str, int]:
    """The mutant's bases read so that the change can be reached from both sides: a
    circular molecule is turned so the change sits in the middle, which is what gives it
    template either side of it however near base 1 it lies; a linear one is read as it
    is."""
    if not circular:
        return mutant, start
    shift = (start - len(mutant) // 2) % len(mutant)
    return mutant[shift:] + mutant[:shift], (start - shift) % len(mutant)


def _room(read: str, at: int, end: int, circular: bool, wanted: int, what: str) -> None:
    if circular:
        return
    if at < wanted or len(read) - end < wanted:
        raise Impossible(
            f"The change at base {at + 1} has fewer than {wanted} bases of template on one "
            f"side of it, which {what} needs, and a linear template has no more to give."
        )


def overlapping(
    mutant: str,
    start: int,
    stop: int,
    rules: Rules,
    circular: bool,
    *,
    mismatched: int = 0,
    inserted: int = 0,
) -> list[Designed]:
    """The QuikChange pair: one primer carrying the change with correct template either
    side, and its own reverse complement.

    Among the pairs that meet the manual's rules, the one that puts the change nearest the
    centre is taken, and of those the shortest: a longer primer costs more and anneals no
    better once it is over the acceptance temperature.
    """
    read, at = _read_round(mutant, start, circular)
    end = at + (stop - start)
    _room(read, at, end, circular, rules.flank, "the QuikChange design")
    best: tuple[tuple[int, int], str] | None = None
    for left in range(rules.flank, rules.longest + 1):
        for right in range(rules.flank, rules.longest + 1):
            if at - left < 0 or end + right > len(read):
                continue
            bases = read[at - left : end + right]
            if not rules.shortest <= len(bases) <= rules.longest:
                continue
            if (gc_content(bases) or 0.0) < rules.min_gc:
                continue
            if rules.gc_clamp and not (_clamped(bases) and _clamped(reverse_complement(bases))):
                continue
            if quikchange_tm(bases, mismatched or inserted, bool(inserted)) < rules.quikchange_tm:
                continue
            rank = (abs(left - right), len(bases))
            if best is None or rank < best[0]:
                best = (rank, bases)
    if best is None:
        raise Impossible(
            "No pair of overlapping primers meets the QuikChange rules for this change: "
            f"{rules.shortest} to {rules.longest} bases, the change at least {rules.flank} "
            f"from either end, at least {rules.min_gc:g}% GC, ending in C or G, and at "
            f"least {rules.quikchange_tm:g} °C by the manual's formula. The template may be "
            "too AT-rich here, or the change too long to sit inside one primer."
        )
    bases = best[1]
    found = quikchange_tm(bases, mismatched or inserted, bool(inserted))
    measure = (("QuikChange Tm", round(found, 1)),)
    matched = len(bases) - mismatched - inserted
    return [
        Designed("forward", bases, "forward", matched, inserted, measure),
        Designed("reverse", reverse_complement(bases), "reverse", matched, inserted, measure),
    ]


def partially_overlapping(
    mutant: str,
    start: int,
    stop: int,
    rules: Rules,
    circular: bool,
    *,
    mismatched: int = 0,
    inserted: int = 0,
) -> list[Designed]:
    """Zheng's and Liu and Naismith's pair: both primers carry the change in a stretch
    they share at their 5' ends, and each has bases of its own at its 3' end.

    The shared stretch is the change with template either side of it, grown until the
    shortest pair of 3' ends that meets Zheng's eight bases anneals 5 to 10 °C above it,
    which is Liu and Naismith's rule. Both temperatures are Wetmur's, as their protocol
    computes them, and each primer's own end is a G or a C, which is Zheng's third rule.
    """
    read, at = _read_round(mutant, start, circular)
    end = at + (stop - start)
    _room(read, at, end, circular, rules.least_edge + rules.longest, "this design")
    low, high = rules.gap
    middle = (low + high) / 2
    best: tuple[tuple[float, int], tuple[str, str, float, float, float]] | None = None
    for left in range(rules.least_edge, rules.longest):
        for right in range(rules.least_edge, rules.longest):
            if at - left < 0 or end + right > len(read):
                continue
            together = read[at - left : end + right]
            if len(together) > rules.longest - rules.least_own:
                continue
            shared_tm = wetmur_tm(together, rules.potassium)
            ahead = _own_end(read[end + right :], shared_tm, rules, rules.longest - len(together))
            behind = _own_end(
                read[: at - left][::-1], shared_tm, rules, rules.longest - len(together), back=True
            )
            if ahead is None or behind is None:
                continue
            forward, reverse = together + ahead, behind + together
            if not all(rules.shortest <= len(each) <= rules.longest for each in (forward, reverse)):
                continue
            tms = (wetmur_tm(ahead, rules.potassium), wetmur_tm(behind, rules.potassium))
            rank = (
                max(abs(tm - shared_tm - middle) for tm in tms),
                len(forward) + len(reverse),
            )
            if best is None or rank < best[0]:
                best = (rank, (forward, reverse, shared_tm, *tms))
    if best is None:
        raise Impossible(
            "No partially overlapping pair meets the rules for this change: at least "
            f"{rules.least_own} bases of its own at each 3' end, each ending in G or C, and "
            f"a non-overlapping part annealing {low:g} to {high:g} °C above the part the two "
            "primers share. The template may be too AT-rich or too GC-rich here."
        )
    forward, reverse, shared_tm, ahead_tm, behind_tm = best[1]
    shared = round(shared_tm, 1)
    reversed_oligo = reverse_complement(reverse)

    def matched(bases: str) -> int:
        return len(bases) - mismatched - inserted

    return [
        Designed(
            "forward",
            forward,
            "forward",
            matched(forward),
            inserted,
            (("Wetmur Tm of its own 3' end", round(ahead_tm, 1)), ("Wetmur Tm shared", shared)),
            _unclamped(rules, forward),
        ),
        Designed(
            "reverse",
            reversed_oligo,
            "reverse",
            matched(reverse),
            inserted,
            (("Wetmur Tm of its own 3' end", round(behind_tm, 1)), ("Wetmur Tm shared", shared)),
            _unclamped(rules, reversed_oligo),
        ),
    ]


def _own_end(
    ahead: str, shared_tm: float, rules: Rules, longest: int, back: bool = False
) -> str | None:
    """A primer's own 3' end: the shortest stretch that anneals inside Liu and Naismith's
    window above the stretch the two primers share, ending in a G or a C where one is
    within reach.

    `ahead` is read outwards from the shared stretch -- for the reverse primer, that is
    the template read backwards -- so the last base of each candidate is always the one
    at the primer's 3' end, whose complement the polymerase starts from. The stretch
    comes back the way round it lies on the forward strand.
    """
    low, high = rules.gap
    found: str | None = None
    for length in range(rules.least_own, min(longest, len(ahead)) + 1):
        bases = ahead[:length]
        gap = wetmur_tm(bases, rules.potassium) - shared_tm
        if gap > high:
            break
        if gap < low:
            continue
        if found is None:
            found = bases
        if not rules.gc_clamp or bases[-1].upper() in "GC":
            return bases[::-1] if back else bases
    return None if found is None else (found[::-1] if back else found)


def back_to_back(
    mutant: str,
    start: int,
    stop: int,
    rules: Rules,
    circular: bool,
    *,
    mismatched: int = 0,
    inserted: int = 0,
) -> list[Designed]:
    """Hemsley's pair: two primers lying back to back on opposite strands, 5' ends
    apposing, which amplify the molecule outwards to be ligated blunt. The change is
    carried on the 5' end of the forward primer, so it lands at the join; a deletion needs
    no tail, since the primers start outside the bases that are to go.
    """
    if not circular:
        raise Impossible(
            "Back-to-back primers amplify a circle outwards and the product is ligated "
            "blunt, so they need a circular template. Use overlapping or partially "
            "overlapping primers on a linear one."
        )
    read, at = _read_round(mutant, start, circular)
    end = at + (stop - start)
    tail = read[at:end]
    if len(tail) > rules.longest - rules.least_match:
        raise Impossible(
            f"The change is {len(tail):,} bases, which leaves no room on a primer of at most "
            f"{rules.longest} for the {rules.least_match} that must match the template. Use "
            "overlapping or partially overlapping primers."
        )
    ahead = _annealing(read[end:], rules, rules.longest - len(tail))
    behind = _annealing(read[:at][::-1], rules, rules.longest, back=True)
    note = (
        "the product is ligated blunt, so it must be phosphorylated: on these primers, or "
        "on the product before ligation",
    )
    forward, reverse = tail + ahead, reverse_complement(behind)
    return [
        Designed(
            "forward",
            forward,
            "forward",
            len(ahead),
            len(tail),
            (("Tm where it matches", round(melting_temperature(ahead) or 0.0, 1)),),
            note + _unclamped(rules, forward),
        ),
        Designed(
            "reverse",
            reverse,
            "reverse",
            len(behind),
            0,
            (("Tm where it matches", round(melting_temperature(reverse) or 0.0, 1)),),
            note + _unclamped(rules, reverse),
        ),
    ]


def _unclamped(rules: Rules, bases: str) -> tuple[str, ...]:
    """A note where a primer's 3' end isn't a G or a C after all.

    The QuikChange manual and Zheng's protocol both ask for one, since a G:C pair holds
    the 3' end on the template while the polymerase starts. Where no stretch within the
    lengths allowed both meets the protocol's temperature and ends in one, the primer is
    designed anyway and says so: a primer that works less well is something a lab can
    weigh, and no primer at all for a change the template simply can't offer one for is
    not.
    """
    if not rules.gc_clamp or bases[-1:].upper() in {"G", "C"}:
        return ()
    return (
        f"Its 3' end is {bases[-1]}, not a G or a C: no stretch within "
        f"{rules.longest} bases that met the rest of the rules ended in one here",
    )


def _annealing(ahead: str, rules: Rules, longest: int, back: bool = False) -> str:
    """The shortest stretch of `ahead` that anneals at `rules.anneal_tm` by SantaLucia's
    model, ending in a G or a C where one is within reach.

    `ahead` is read outwards from the change, so the reverse primer's stretch is given
    backwards and its last base is the one at that primer's 3' end. SantaLucia's model
    reads a strand 5' to 3' -- a sequence and the same sequence backwards are different
    molecules with different temperatures -- so the temperature is taken on the stretch
    the way round it lies, and the stretch comes back that way too.
    """
    found: str | None = None
    for length in range(max(rules.least_match, 1), min(longest, len(ahead)) + 1):
        bases = ahead[:length]
        tm = melting_temperature(bases[::-1] if back else bases)
        if tm is None or tm < rules.anneal_tm:
            continue
        if found is None:
            found = bases
        if not rules.gc_clamp or _clamped(bases):
            return bases[::-1] if back else bases
    if found is None:
        raise Impossible(
            f"No stretch of {rules.least_match} to {longest} bases anneals at "
            f"{rules.anneal_tm:g} °C here: the template is very AT-rich."
        )
    return found[::-1] if back else found


DESIGNS = {
    "overlapping": overlapping,
    "partially_overlapping": partially_overlapping,
    "back_to_back": back_to_back,
}

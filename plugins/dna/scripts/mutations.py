"""Changes a lab states, read and applied to a sequence (ADR 0204).

A site-directed mutagenesis starts with a change said in words: "C to T at 1,234",
"delete codon 57", "put a FLAG tag in after 900". Iki already writes such changes in one
notation -- `align_reads` names every difference a read has from its reference as HGVS's
DNA recommendations name it (den Dunnen et al., *Hum Mutat* 37:564, 2016) -- so a design
states its changes the same way, and what a lab reads back from its sequencing is written
the way it asked for the change in the first place.

This module reads those names and applies them. `scripts/reads.py` holds the writing
side, and the names here go back through it, so a change is always reported in the place
HGVS puts it (3'-most where repeated bases allow it to move) however the lab wrote it.

What is read, on a genomic reference numbered from its first base -- `g.` for a linear
sequence, `o.` for a circular one, which are the two prefixes HGVS allows for a genomic
reference and the two `align_reads` writes:

- a substitution, `g.1234C>T`;
- a deletion, `g.1240del`, `g.1240_1242del`, each with the bases optionally stated
  (`g.1240_1242delCTG`), which are then checked;
- an insertion, `g.1250_1251insGGATCC`;
- a duplication, `g.1250dup`, `g.1250_1252dup`;
- an inversion, `g.3_6inv`;
- a deletion-insertion, `g.1260_1261delinsTA`.

Every base a name states is checked against the template, and a name whose base isn't the
template's base is refused rather than applied: a change stated against the wrong
reference is the one mistake in mutagenesis that costs a lab a round of cloning and a
week, and HGVS requires the reference's own base in a substitution for exactly that
reason.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Any

from Bio.Data import CodonTable
from reads import named

#: `[prefix.]`, where, what. The whole name, so nothing is read from a name with
#: anything else in it.
NAME = re.compile(
    r"""^\s*(?:(?P<prefix>[a-z])\.)?
        (?P<first>\d+)(?:_(?P<last>\d+))?
        (?P<what>
            [ACGTRYSWKMBDHVN]>[ACGTRYSWKMBDHVN]
          | delins[ACGTRYSWKMBDHVN]+
          | del[ACGTRYSWKMBDHVN]*
          | ins[ACGTRYSWKMBDHVN]+
          | dup[ACGTRYSWKMBDHVN]*
          | inv
        )\s*$""",
    re.VERBOSE,
)
BASES = frozenset("ACGTRYSWKMBDHVN")
COMPLEMENT = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")


class Refused(ValueError):  # noqa: N818 - reads as "the change is refused"
    """A change Iki won't apply, said in words a lab can act on."""


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


@dataclass(frozen=True)
class Stated:
    """One change, as the lab stated it and as it lies on the template.

    `start` and `stop` are 0-based and end-exclusive over the template's bases, counting
    on round the origin of a circular one, so a change across the origin has
    `stop > len(template)`. `bases` is what the template has there instead; none for a
    deletion. An insertion replaces nothing, so its `start` and `stop` are equal.
    """

    said: str
    kind: str
    start: int
    stop: int
    bases: str

    @property
    def replaced(self) -> int:
        return self.stop - self.start

    @property
    def difference(self) -> int:
        """How much longer the sequence gets, which is negative for a deletion."""
        return len(self.bases) - self.replaced


def _place(first: int, last: int, length: int, circular: bool, said: str) -> tuple[int, int]:
    """A name's 1-based `first` and `last` as 0-based places, end exclusive, counting on
    round a circular template's origin where the range runs across it."""
    if first < 1 or last < 1:
        raise Refused(f"{said} numbers a base before 1: a sequence's first base is 1.")
    if first > length or last > length:
        raise Refused(
            f"{said} names base {max(first, last)}, past the template's {length:,} bases."
        )
    if last < first:
        if not circular:
            raise Refused(
                f"{said} runs from {first} back to {last}: only a circular sequence's "
                "range may run across its origin."
            )
        return first - 1, last + length
    return first - 1, last


def stated(said: str, template: str, circular: bool) -> Stated:
    """One change, read from its HGVS name and placed on the template, with every base it
    states checked against the template's own.

    The prefix is checked too: `g.` numbers a linear genomic reference and `o.` a
    circular one, so a name with the wrong one is a name written against some other
    molecule.
    """
    found = NAME.match(said)
    if not found:
        raise Refused(
            f"{said!r} isn't a change Iki can read. Changes are named as HGVS names them "
            "on a genomic reference, such as g.1234C>T, g.1240_1242del, "
            "g.1250_1251insGGATCC, g.1250dup, g.3_6inv or g.1260_1261delinsTA."
        )
    wanted = "o" if circular else "g"
    prefix = found["prefix"]
    if prefix and prefix != wanted:
        was, now = ("circular", "linear") if circular else ("linear", "circular")
        raise Refused(
            f"{said} is numbered {prefix}., which is a {now} genomic reference's "
            f"numbering; the template is {was}, so its changes are {wanted}."
        )
    length = len(template)
    first = int(found["first"])
    last = int(found["last"] or found["first"])
    what = found["what"]
    if ">" in what:
        was, now = what.split(">")
        if found["last"]:
            raise Refused(f"{said} is a substitution of one base, so it names one place.")
        start, stop = _place(first, first, length, circular, said)
        _check(said, template, start, stop, was)
        return Stated(said, "substitution", start, stop, now)
    if what.startswith("delins"):
        start, stop = _place(first, last, length, circular, said)
        return Stated(said, "deletion-insertion", start, stop, what[len("delins") :])
    if what.startswith("del"):
        start, stop = _place(first, last, length, circular, said)
        if stated_bases := what[len("del") :]:
            _check(said, template, start, stop, stated_bases)
        return Stated(said, "deletion", start, stop, "")
    if what.startswith("ins"):
        if last != first + 1 and not (circular and first == length and last == 1):
            raise Refused(
                f"{said} inserts between two bases, so it names them both, side by side, "
                "such as g.1250_1251insG."
            )
        start, _ = _place(first, first, length, circular, said)
        return Stated(said, "insertion", start + 1, start + 1, what[len("ins") :])
    if what.startswith("dup"):
        start, stop = _place(first, last, length, circular, said)
        copied = _stretch(template, start, stop)
        if stated_bases := what[len("dup") :]:
            _check(said, template, start, stop, stated_bases)
        # A duplication puts a second copy after the first: the places it replaces are
        # the ones copied, and what goes there is both copies.
        return Stated(said, "duplication", start, stop, copied + copied)
    start, stop = _place(first, last, length, circular, said)
    if stop - start < 2:
        raise Refused(f"{said} inverts one base, which is a substitution: name it C>G.")
    inverted = reverse_complement(_stretch(template, start, stop))
    return Stated(said, "inversion", start, stop, inverted)


def _stretch(template: str, start: int, stop: int) -> str:
    """The template's bases from 0-based `start` to `stop`, round the origin."""
    length = len(template)
    return "".join(template[k % length] for k in range(start, stop))


def _check(said: str, template: str, start: int, stop: int, bases: str) -> None:
    """Refuses a name whose stated bases aren't the template's there. HGVS has a
    substitution state the reference's own base so that a change written against the
    wrong sequence, or against the other strand, is caught before anyone orders a
    primer."""
    found = _stretch(template, start, stop)
    if len(bases) != len(found):
        raise Refused(
            f"{said} states {len(bases)} base(s) where it names {len(found)} of the template."
        )
    if bases.upper() != found.upper():
        where = start + 1 if stop - start == 1 else f"{start + 1}_{(stop - 1) % len(template) + 1}"
        raise Refused(
            f"{said} says the template has {bases} at {where}, and it has {found}. "
            "Check the change is written against this sequence, on this strand."
        )


def read(names: list[str], template: str, circular: bool) -> list[Stated]:
    """Every change, read and placed, in the order they lie along the template, with two
    that touch the same bases refused: a sequence can't have both."""
    found = sorted((stated(said, template, circular) for said in names), key=_order)
    for before, after in itertools.pairwise(found):
        across = max(before.stop, before.start + 1) > after.start
        if across or (before.start == after.start and before.stop == after.stop):
            raise Refused(
                f"{before.said} and {after.said} change the same bases of the template: "
                "name them as one change, such as a deletion-insertion."
            )
    return found


def _order(change: Stated) -> tuple[int, int]:
    return change.start, change.stop


def named_change(template: str, change: Stated, circular: bool) -> str:
    """The change's HGVS name where it lies, which is where HGVS puts it: as far 3' as the
    bases allow (den Dunnen et al., 2016, the 3' rule). `scripts/reads.py` writes the
    names Iki reports for the differences sequencing finds, and a change asked for here is
    reported in exactly that form, so what was asked for and what was read back are the
    same string."""
    return named(template, (change.start, change.stop, change.bases), circular)


def applied(template: str, changes: list[Stated]) -> tuple[str, int]:
    """The template with every change made, and which of the template's bases the product
    begins at (0 unless a change ran across a circular origin).

    Changes are made from the last to the first, so each one's places are still the
    template's when it is made. At most one change can run across a circular template's
    origin, since two would touch the same bases; it is made last, and the product is
    written from the first base of that change, so the change is in one piece rather than
    split between the two ends of the molecule. Nothing else moves the origin.
    """
    length = len(template)
    across = [change for change in changes if change.stop > length]
    within = sorted((c for c in changes if c.stop <= length), key=_order, reverse=True)
    out = template
    for change in within:
        out = out[: change.start] + change.bases + out[change.stop :]
    if not across:
        return out, 0
    change = across[0]
    over = change.stop - length
    # The changes made above all lie between the origin-crossing change's two ends, so
    # they moved the bases between `over` and `start`, and only by how much they changed.
    moved = sum(c.difference for c in within if c.start < change.start)
    kept = out[over : change.start + moved]
    # Where the product's first base came from: the change's own first base, or, where
    # the change put nothing there, the first base the change left.
    return change.bases + kept, change.start if change.bases else over


# --- what the change does to the protein -------------------------------------------


#: One-letter codes to HGVS's three-letter ones, which is how HGVS writes a protein
#: change (den Dunnen et al., 2016, which recommends the three-letter code).
THREE = {
    "A": "Ala", "R": "Arg", "N": "Asn", "D": "Asp", "C": "Cys", "Q": "Gln", "E": "Glu",
    "G": "Gly", "H": "His", "I": "Ile", "L": "Leu", "K": "Lys", "M": "Met", "F": "Phe",
    "P": "Pro", "S": "Ser", "T": "Thr", "W": "Trp", "Y": "Tyr", "V": "Val", "*": "Ter",
}  # fmt: skip


def genetic_code(number: int) -> Any:  # noqa: ANN401 - Biopython's CodonTable
    """An NCBI translation table, by its number, from Biopython's copy of NCBI's."""
    try:
        return CodonTable.unambiguous_dna_by_id[number]
    except KeyError as why:
        raise Refused(f"There is no NCBI translation table {number}.") from why


def residue_of(codon: str, table: Any) -> str:  # noqa: ANN401 - Biopython's CodonTable
    """The amino acid a codon codes for in `table`, or `*` for a stop."""
    codon = codon.upper()
    if codon in table.stop_codons:
        return "*"
    found: str | None = table.forward_table.get(codon)
    if found is None:
        raise Refused(f"{codon} isn't a codon of A, C, G and T.")
    return found


def translate(bases: str, table: Any) -> str:  # noqa: ANN401 - Biopython's CodonTable
    return "".join(
        residue_of(bases[at : at + 3], table) for at in range(0, len(bases) - len(bases) % 3, 3)
    )


def _trimmed_protein(before: str, after: str) -> tuple[int, str, str]:
    """The stretch of the two proteins that differs: where it starts (0-based), what was
    there and what is there now, with the shared ends taken off and the change moved as
    far 3' as the residues allow, which is where HGVS names a protein change."""
    start = 0
    while start < len(before) and start < len(after) and before[start] == after[start]:
        start += 1
    end_before, end_after = len(before), len(after)
    while (
        end_before > start and end_after > start and before[end_before - 1] == after[end_after - 1]
    ):
        end_before -= 1
        end_after -= 1
    was, now = before[start:end_before], after[start:end_after]
    # A deletion or an insertion alone slides 3' through repeated residues, as HGVS asks.
    if bool(was) != bool(now):
        moving, fixed = (was, before) if was else (now, before)
        while (
            moving and start + len(moving) < len(fixed) and moving[0] == fixed[start + len(moving)]
        ):
            moving = moving[1:] + moving[:1]
            start += 1
        was, now = (moving, "") if was else ("", moving)
    return start, was, now


def protein_change(before: str, after: str) -> tuple[str, str]:
    """The change to the protein, as (what it is, its HGVS name).

    Both proteins are given as translated to their first stop, that stop included, since
    where a protein now stops is part of what a change does. Names follow HGVS's protein
    recommendations (den Dunnen et al., *Hum Mutat* 37:564, 2016) in three-letter code:
    a substitution `p.Glu6Val`, no change `p.Glu6=`, a stop gained `p.Glu6Ter`, an
    in-frame deletion `p.Glu6_Lys8del`, an insertion `p.Glu6_Lys7insGlyGly`, a
    deletion-insertion `p.Glu6_Lys8delinsArg`, and a frameshift `p.Glu6ValfsTer17`, whose
    Ter counts the new reading frame's residues from the first one that changed.
    """
    if before == after:
        return "silent", "p.(=)"
    first = 0
    while first < len(before) and first < len(after) and before[first] == after[first]:
        first += 1
    # A stop where a residue was ends the protein there: nothing after it is translated,
    # so the change is that one residue's, whatever the bases downstream now read.
    if first < len(after) and after[first] == "*" and first < len(before) and before[first] != "*":
        return "nonsense", f"p.{THREE[before[first]]}{first + 1}Ter"
    if first < len(before) and before[first] == "*":
        now = after[first] if first < len(after) else "?"
        return "stop lost", f"p.Ter{first + 1}{THREE.get(now, 'Xaa')}extTer?"
    start, was, now = _trimmed_protein(before, after)
    at = start + 1
    three = lambda residues: "".join(THREE[r] for r in residues)  # noqa: E731
    if len(was) == 1 and len(now) == 1:
        if at == 1:
            return "start lost", f"p.{THREE[was]}1{THREE[now]}"
        return "missense", f"p.{THREE[was]}{at}{THREE[now]}"
    if was and not now:
        return "in-frame deletion", f"p.{_span(was, at)}del"
    if now and not was:
        # An insertion is named between the two residues it goes between.
        left, right = before[start - 1], before[start]
        return (
            "in-frame insertion",
            f"p.{THREE[left]}{start}_{THREE[right]}{start + 1}ins{three(now)}",
        )
    return "in-frame deletion-insertion", f"p.{_span(was, at)}delins{three(now)}"


def _span(residues: str, at: int) -> str:
    """Where a stretch of residues is, as HGVS writes it: one residue, or its first and
    its last."""
    if len(residues) == 1:
        return f"{THREE[residues[0]]}{at}"
    return f"{THREE[residues[0]]}{at}_{THREE[residues[-1]]}{at + len(residues) - 1}"


def frameshift(before: str, after: str) -> tuple[str, str]:
    """A frameshift's name: the first residue that changed, what is there now, and where
    the new reading frame's stop is, counting that residue as 1 (HGVS's `fsTer`). Both
    proteins are given with their stops, since where the new one stops is the point."""
    start = 0
    while start < len(before) and start < len(after) and before[start] == after[start]:
        start += 1
    if start >= len(after):
        return "frameshift", f"p.{THREE[before[start]]}{start + 1}fs"
    stop = after.find("*", start)
    tail = "Ter?" if stop < 0 else f"Ter{stop - start + 1}"
    was = THREE[before[start]] if start < len(before) else "Ter"
    return "frameshift", f"p.{was}{start + 1}{THREE[after[start]]}fs{tail}"

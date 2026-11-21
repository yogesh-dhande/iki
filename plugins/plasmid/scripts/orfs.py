"""Open reading frames along a DNA sequence, in all six frames.

An ORF here is a start codon and the in-frame stop codon that ends it, with at least
``least`` codons between them, the start's included and the stop's not: one ORF for each
stop, from the first start codon after the stop before it in the same frame, as NCBI's
ORFfinder and EMBOSS's getorf report them. A shorter ORF nested in a longer one, from a
later start before the same stop, is part of the longer one and isn't listed again.

The genetic code is one of NCBI's translation tables, as Biopython holds them
(``Bio.Data.CodonTable``): it says which codons stop and what the others code for, and
which codons may start. ATG alone starts an ORF unless alternative starts are asked for;
then each of the table's start codons does (table 11's TTG, CTG, ATT, ATC, ATA and GTG as
well as ATG), and the start is translated as methionine, as an initiator tRNA reads it.
Tables 27, 28 and 31 aren't offered: in each, a stop codon is read as a sense codon in some
places and as a stop in others, and the bases alone don't say which.

A circle has no ends, so an ORF may run across its origin, and it is followed round as
the molecule is read; an ORF is at most the molecule's length, start to stop. A linear
molecule's two ends are ends (ADR 0168): nothing reads across them, and a frame that runs
off an end with no stop has no ORF there, since nothing says where its protein would end.

Codons with an ambiguous base (N, R, Y and the rest) neither start nor stop, and are
translated X.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from Bio.Data import CodonTable

COMPLEMENT = str.maketrans("ACGTRYKMBVDHNSWacgtrykmbvdhnsw", "TGCAYRMKVBHDNSWtgcayrmkvbhdnsw")
#: The fewest codons an ORF has by default, its start's included and its stop's not: 75,
#: as SnapGene's ORFs and NCBI ORFfinder's 75 nt choice suggest for a protein worth
#: drawing. Fewer find the short ORFs any stretch of DNA has by chance.
LEAST_CODONS = 75
#: What a run records, at most. A genome has thousands of ORFs this long; more than this
#: and the run says to raise the length rather than keep some and not others.
MOST_ORFS = 5000
#: Tables whose stops are read as sense codons in some places (NCBI's notes to tables 27,
#: 28 and 31), which the bases alone can't settle.
CONTEXT_DEPENDENT = frozenset({27, 28, 31})


class Refused(ValueError):  # noqa: N818 - reads as "the run refuses"
    """Why the ORFs can't be found as asked."""


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def tables() -> dict[int, Any]:
    """NCBI's translation tables that the bases alone can be read by."""
    return {
        number: table
        for number, table in CodonTable.unambiguous_dna_by_id.items()
        if number not in CONTEXT_DEPENDENT
    }


def table_name(number: int) -> str:
    table = tables()[number]
    return ", ".join(name for name in table.names if name and not name.startswith("SGC"))


def genetic_code(number: int) -> Any:  # noqa: ANN401 - Biopython's CodonTable
    found = tables().get(number)
    if found is None:
        offered = ", ".join(str(n) for n in sorted(tables()))
        if number in CONTEXT_DEPENDENT:
            raise Refused(
                f"Translation table {number}'s stop codons are read as sense codons in some "
                f"places, and the bases alone don't say which. Choose one of {offered}."
            )
        raise Refused(f"There's no NCBI translation table {number}: choose one of {offered}.")
    return found


@dataclass(frozen=True)
class Orf:
    """One ORF, where it lies on the molecule as a map reads it: ``start`` and ``end``,
    counting from 1, are its lowest and highest bases on the forward strand, and an
    ``end`` before ``start`` runs across a circle's origin. On the reverse strand it is
    read from ``end`` down to ``start``."""

    start: int
    end: int
    strand: str
    frame: int
    codons: int
    start_codon: str
    translation: str

    @property
    def bases(self) -> int:
        """Its length in bases, its stop codon's included."""
        return 3 * self.codons + 3

    def value(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "strand": self.strand,
            "frame": self.frame,
            "length": self.bases,
            "amino_acids": self.codons,
            "start_codon": self.start_codon,
            "translation": self.translation,
        }


def _codon(bases: str, at: int, circular: bool) -> str:
    length = len(bases)
    if circular:
        return "".join(bases[(at + i) % length] for i in range(3))
    return bases[at : at + 3]


def _translate(codons: list[str], table: Any) -> str:  # noqa: ANN401
    return "M" + "".join(table.forward_table.get(codon, "X") for codon in codons[1:])


def _strand(
    bases: str,
    circular: bool,
    table: Any,  # noqa: ANN401 - Biopython's CodonTable
    starts: frozenset[str],
    least: int,
) -> list[tuple[int, list[str]]]:
    """The ORFs read along one strand, 5' to 3': each as the place its start codon's first
    base is (from 0) and its codons, the stop's left out."""
    length = len(bases)
    stops = frozenset(table.stop_codons)
    found: list[tuple[int, list[str]]] = []
    if circular:
        # The codons a frame reads round a circle: one frame of every third base when the
        # length is a multiple of three, so three frames; otherwise a single frame that
        # goes round three times before it meets itself.
        cycles = [list(range(f, length, 3)) for f in range(3)] if length % 3 == 0 else []
        if length % 3:
            cycles = [[(3 * k) % length for k in range(length)]]
        for cycle in cycles:
            codons = [_codon(bases, at, True) for at in cycle]
            where = [i for i, codon in enumerate(codons) if codon in stops]
            if not where:
                continue  # no stop all the way round: nothing ends a protein
            n = len(cycle)
            for i, stop in enumerate(where):
                previous = where[i - 1] if len(where) > 1 else stop
                # The codons after the previous stop, up to this one, round the cycle.
                gap = (stop - previous - 1) % n if len(where) > 1 else n - 1
                # At most the molecule's length from start to stop, its stop's included.
                most = min(gap, length // 3 - 1)
                for back in range(most, least - 1, -1):
                    first = (stop - back) % n
                    if codons[first] in starts:
                        run = [codons[(first + k) % n] for k in range(back)]
                        found.append((cycle[first], run))
                        break
        return found
    for frame in range(3):
        codons = [bases[at : at + 3] for at in range(frame, length - 2, 3)]
        since = 0  # the first codon after the last stop
        for i, codon in enumerate(codons):
            if codon not in stops:
                continue
            for first in range(since, i - least + 1):
                if codons[first] in starts:
                    found.append((frame + 3 * first, codons[first:i]))
                    break
            since = i + 1
    return found


def find(
    bases: str,
    circular: bool,
    *,
    table_number: int = 1,
    alternative_starts: bool = False,
    least: int = LEAST_CODONS,
) -> list[Orf]:
    """Every ORF of at least ``least`` codons on both strands, by the map's coordinates,
    in order along the molecule."""
    if least < 1:
        raise Refused("An ORF has at least one codon.")
    bases = bases.upper().replace("U", "T")
    length = len(bases)
    table = genetic_code(table_number)
    starts = frozenset(table.start_codons) if alternative_starts else frozenset({"ATG"})
    orfs: list[Orf] = []
    for strand, read in (("forward", bases), ("reverse", reverse_complement(bases))):
        for at, codons in _strand(read, circular, table, starts, least):
            span = 3 * len(codons) + 3
            first, last = at, (at + span - 1) % length if circular else at + span - 1
            if strand == "forward":
                start, end = first + 1, last + 1
                frame = at % 3 + 1
            else:
                # Base i of the reverse strand pairs with base length - 1 - i of the forward.
                start, end = length - last, length - first
                frame = -(at % 3 + 1)
            orfs.append(
                Orf(
                    start=start,
                    end=end,
                    strand=strand,
                    frame=frame,
                    codons=len(codons),
                    start_codon=codons[0],
                    translation=_translate(codons, table),
                )
            )
    orfs.sort(key=lambda orf: (orf.start, orf.end, orf.strand))
    if len(orfs) > MOST_ORFS:
        raise Refused(
            f"It has {len(orfs):,} ORFs of at least {least} codons, more than the "
            f"{MOST_ORFS:,} a run records: ask for longer ones."
        )
    return orfs


def described(table_number: int, alternative_starts: bool, least: int, circular: bool) -> str:
    """What the ORFs were found with, in words, for the record that keeps them."""
    starts = "any of its start codons" if alternative_starts else "ATG only"
    ends = "read across its origin" if circular else "not read across its two ends"
    return (
        f"ORFs of at least {least} codons, translation table {table_number} "
        f"({table_name(table_number)}), starting at {starts}; {ends}"
    )

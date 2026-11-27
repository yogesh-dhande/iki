"""Annotating a plasmid from the library of common vector parts (``data/features.json``).

Each part of the library is the bases of a feature of a public-domain INSDC record (see
``data/README.md``). A part is looked for in the plasmid on both strands, and round its
origin if it's circular, in steps, the first two as BLAST-based annotators (pLannotate)
do:

1. **Seeds.** The plasmid's 18-mers are indexed, and each 18-mer of the part looked up.
   A hit places the part at one offset along the plasmid. A part shorter than a seed
   is looked for as a plain exact match instead.
2. **Alignment.** Round each placement, the part is aligned locally to a window of the
   plasmid (Biopython's ``PairwiseAligner``, BLASTN's scores: match 2, mismatch -3, a
   gap of n bases 7 + 2(n - 1), which is BLASTN's own 5 + 2n), and the alignment's
   identity is the bases of the part that match, over the part's length.
3. **The rest of the plasmid.** A seed is an exact stretch, so a part whose mismatches
   fall closer together than one in every 18 bases has none, however well it aligns (a
   90-base part with a mismatch every 15 bases is 93% identical). So each part 40 bases
   or longer is also aligned locally to the whole plasmid, on each strand, outside what
   its seeds found: the best alignment is a hit if it passes, and the part is looked for
   again outside it, until the best doesn't pass. Where the best scores less than any
   alignment that could pass, it isn't traced back at all, so at the default identity
   this is one scoring of the library against the plasmid, about half a second for a
   10 kb plasmid.

A part is annotated where it's aligned over at least ``min_coverage`` of its length with
at least ``min_identity`` identity; a part under 40 bp must match exactly, since at that
length a few mismatches say nothing. Overlapping placements of one part keep the best,
so a part annotates each place it's found once: attR1 and attR2, or two LTRs, are each
their own hit.

Vector parts drift between vectors (pUC19's beta-lactamase differs from pBR322's at two
bases), so what identity a hit has is kept with it, and shown.

A part that is a gene is only taken to say what the plasmid does where its reading frame
is whole in the plasmid's own bases: a ura3 nonsense allele is 99.75% identical to URA3
and selects nothing. Such a hit is still annotated, with its frame recorded as
interrupted.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from Bio import Align
from Bio.Seq import Seq

#: The seed, long enough that a 4^18 chance is nothing in a plasmid, short enough that a
#: part with mismatches every 20 bases still has one.
SEED = 18
#: Under this many bases, only an exact match is annotated.
EXACT_UNDER = 40
#: A base no part aligns with, put where a part was found so it isn't found there again.
MASK = "N"
#: BLASTN's scores. Biopython charges `open` for a gap's first base and `extend` for each
#: after it, so these give 7 + 2(n - 1) for a gap of n bases, which is BLASTN's own
#: existence 5 plus extension 2 per base (5 + 2n).
MATCH, MISMATCH, OPEN, EXTEND = 2.0, -3.0, -7.0, -2.0
#: The stop codons of the standard genetic code (NCBI translation table 1).
STOPS = frozenset({"TAA", "TAG", "TGA"})
#: The roles whose parts only say what a plasmid does where their gene can be translated.
CODING_ROLES = frozenset({"resistance", "selection"})

COMPLEMENT = str.maketrans("ACGTRYKMBVDHNSWacgtrykmbvdhnsw", "TGCAYRMKVBHDNSWtgcayrmkvbhdnsw")


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


@dataclass(frozen=True)
class Part:
    """One part of the library: a stretch of bases, what it is, and what it says about a
    plasmid that has it."""

    name: str
    label: str
    type: str
    role: str
    bases: str
    source: dict[str, str]
    implies: dict[str, str]


@dataclass(frozen=True)
class Library:
    version: str
    licence: str
    parts: tuple[Part, ...]

    @staticmethod
    def read(path: Path) -> Library:
        held = json.loads(path.read_text(encoding="utf-8"))
        return Library(
            version=held["version"],
            licence=held["licence"],
            parts=tuple(
                Part(
                    name=part["name"],
                    label=part.get("label", part["name"]),
                    type=part["type"],
                    role=part["role"],
                    bases=part["bases"].upper(),
                    source=part["source"],
                    implies=part.get("implies", {}),
                )
                for part in held["parts"]
            ),
        )


@dataclass(frozen=True)
class Hit:
    """A part found along a plasmid: its first and last base, counting from 1, with the
    last before the first where it crosses a circular plasmid's origin."""

    part: Part
    start: int
    end: int
    strand: str
    identity: float
    #: For a part that is a gene, whether its reading frame is whole in the plasmid's own
    #: bases: "whole" or "interrupted". Empty for a part that isn't a gene.
    reading_frame: str = ""


def _aligner() -> Align.PairwiseAligner:
    aligner = Align.PairwiseAligner(mode="local")
    aligner.match_score = MATCH
    aligner.mismatch_score = MISMATCH
    aligner.open_gap_score = OPEN
    aligner.extend_gap_score = EXTEND
    return aligner


def _seeds(bases: str) -> dict[str, list[int]]:
    """Where each 18-mer of ``bases`` starts, counting from 0."""
    index: dict[str, list[int]] = {}
    for i in range(len(bases) - SEED + 1):
        index.setdefault(bases[i : i + SEED], []).append(i)
    return index


def _identity(aligner: Align.PairwiseAligner, part: str, window: str) -> tuple[float, int, int]:
    """The best local alignment of ``part`` to ``window``: the share of the part's bases
    that match, and where in the window the alignment lies (from 0, end exclusive)."""
    try:
        alignments = aligner.align(Seq(window), Seq(part))
    except ValueError:  # pragma: no cover - a window shorter than the aligner can take
        return 0.0, 0, 0
    if not len(alignments):  # pragma: no cover - no alignment at all
        return 0.0, 0, 0
    best = alignments[0]
    counts = best.counts()
    return counts.identities / len(part), int(best.aligned[0][0][0]), int(best.aligned[0][-1][-1])


def _at(bases: str, hit: Hit) -> str:
    """The plasmid's own bases where a hit lies, 5' to 3' along the hit's strand, round
    the origin where its end comes before its start."""
    length = len(bases)
    span = hit.end - hit.start + 1 if hit.end >= hit.start else length - hit.start + 1 + hit.end
    got = (bases + bases)[hit.start - 1 : hit.start - 1 + span]
    return reverse_complement(got) if hit.strand == "reverse" else got


def _reading_frame(hit: Hit, bases: str) -> str:
    """Whether a gene the annotation found reads as a gene in the plasmid's own bases: a
    whole number of codons, from ATG to a stop, with no stop inside it. A ura3 nonsense
    allele is 99.75% identical to URA3 and selects nothing, so what a plasmid is selected
    with is only claimed where the gene is there to be translated. Empty for a part that
    isn't a gene."""
    if hit.part.type != "CDS" or hit.part.role not in CODING_ROLES:
        return ""
    got = _at(bases, hit)
    if len(got) % 3 or len(got) < 6 or got[:3] != "ATG" or got[-3:] not in STOPS:
        return "interrupted"
    inside = (got[i : i + 3] for i in range(3, len(got) - 3, 3))
    return "interrupted" if any(codon in STOPS for codon in inside) else "whole"


def _placements(index: dict[str, list[int]], doubled: str, probe: str) -> set[int]:
    """Where the probe may lie along the plasmid, counting from 0.

    Every one of the probe's 18-mers is looked up, so any exact stretch that long places
    it. A probe too short to hold a seed, or one whose every 18-mer carries a mismatch,
    has no seed at all: those are looked for as an exact match instead, which the
    library's short parts need (cPPT is 16 bases and the T3 promoter 17). A longer probe
    without a seed is still found where it aligns well enough (:func:`_unseeded`).
    """
    found: set[int] = set()
    for offset in range(max(len(probe) - SEED, -1) + 1):
        for at in index.get(probe[offset : offset + SEED], ()):
            found.add(at - offset)
    if not found:
        at = doubled.find(probe)
        while at >= 0:
            found.add(at)
            at = doubled.find(probe, at + 1)
    return found


def annotate(
    bases: str,
    circular: bool,
    library: Library,
    min_identity: float = 0.9,
    min_coverage: float = 0.8,
) -> list[Hit]:
    """Every part of ``library`` found along ``bases``, best hit first."""
    bases = bases.upper()
    length = len(bases)
    longest = max((len(part.bases) for part in library.parts), default=0)
    # A circular plasmid is read as if it went round again, so a part across its origin is
    # found in one piece; a part longer than the plasmid can't be in it at all.
    doubled = bases + bases[: min(longest, length)] if circular else bases
    index = _seeds(doubled)
    aligner = _aligner()
    hits: list[Hit] = []
    for part in library.parts:
        if len(part.bases) > length:
            continue
        exact = len(part.bases) < EXACT_UNDER
        for strand, probe in (("forward", part.bases), ("reverse", reverse_complement(part.bases))):
            found: list[Hit] = []
            for placement in sorted(_placements(index, doubled, probe)):
                slack = max(5, len(probe) // 20)
                start = max(placement - slack, 0)
                window = doubled[start : placement + len(probe) + slack]
                if not window:
                    continue
                if exact:
                    at = window.find(probe)
                    if at < 0:
                        continue
                    identity, first, last = 1.0, at, at + len(probe)
                else:
                    identity, first, last = _identity(aligner, probe, window)
                    if identity < min_identity or (last - first) < min_coverage * len(probe):
                        continue
                found.append(
                    Hit(
                        part=part,
                        start=(start + first) % length + 1,
                        end=(start + last - 1) % length + 1,
                        strand=strand,
                        identity=round(identity, 4),
                    )
                )
            kept = _best_of_overlapping(found, length)
            if not exact:
                kept += _unseeded(
                    aligner, doubled, length, part, strand, probe, kept, min_identity, min_coverage
                )
            hits.extend(kept)
    hits = _best_of_shadowed(hits, length)
    hits = [replace(hit, reading_frame=_reading_frame(hit, bases)) for hit in hits]
    hits.sort(key=lambda hit: (hit.start, -hit.identity, hit.part.name))
    return hits


def _floor(length: int, min_identity: float) -> float:
    """The least an alignment of a part of ``length`` bases scores when it passes: its
    matches are at least ``min_identity`` of it, and each of its other bases costs at most
    a one-base gap's opening (BLASTN's 7; a mismatch costs 3, and a longer gap less a
    base). An alignment that also needs the plasmid's own bases inserted between the
    part's costs more than that, so one that needs many is found only by its seeds."""
    matches = math.ceil(min_identity * length)
    return MATCH * matches + OPEN * (length - matches)


def _unseeded(
    aligner: Align.PairwiseAligner,
    doubled: str,
    length: int,
    part: Part,
    strand: str,
    probe: str,
    found: list[Hit],
    min_identity: float,
    min_coverage: float,
) -> list[Hit]:
    """The probe's hits outside those its seeds found: its best local alignment to the
    rest of the plasmid, as long as it passes, each kept and then masked so the next is
    looked for elsewhere. It stops at the first that doesn't pass, without tracing back
    one that scores less than any alignment that could pass, or after as many as the
    plasmid has room for."""
    masked = list(doubled)

    def mask(hit: Hit) -> None:
        for position in _covered(hit, length):
            for at in (position - 1, position - 1 + length):
                if at < len(masked):
                    masked[at] = MASK

    for hit in found:
        mask(hit)
    floor = _floor(len(probe), min_identity)
    more: list[Hit] = []
    for _ in range(len(doubled) // len(probe) + 1):
        plasmid = "".join(masked)
        if aligner.score(plasmid, probe) < floor:
            break
        identity, first, last = _identity(aligner, probe, plasmid)
        hit = Hit(
            part=part,
            start=first % length + 1,
            end=(last - 1) % length + 1,
            strand=strand,
            identity=round(identity, 4),
        )
        if identity < min_identity or (last - first) < min_coverage * len(probe):
            break
        more.append(hit)
        mask(hit)
    return more


def _best_of_overlapping(found: list[Hit], length: int) -> list[Hit]:
    """Of hits of one part at overlapping places, the one with the highest identity: one
    placement of a part is one annotation, however many seeds put it there."""
    kept: list[Hit] = []
    for hit in sorted(found, key=lambda h: (-h.identity, h.start)):
        if not any(_overlap(hit, other, length) for other in kept):
            kept.append(hit)
    return kept


def _best_of_shadowed(hits: list[Hit], length: int) -> list[Hit]:
    """Where two hits of the same kind of part lie almost wholly on the same bases, the
    better match alone: two parts of a family are alike enough to find each other's
    places (Gateway's attR1 and attR2 are 95% identical), and only one of them is there.

    Only alternative readings of the same thing compete, which parts of the same INSDC
    feature key and the same role are. A part nested inside a part of another kind means
    something of its own and stays, as the SV40 origin within the SV40 early promoter
    does, or a lac operator within a promoter. Hits are ranked by how many bases they
    cover, counted round the origin, so where a plasmid's record begins makes no
    difference to which survive.
    """
    kept: list[Hit] = []
    for hit in sorted(hits, key=lambda h: (-h.identity, -len(_covered(h, length)), h.part.name)):
        mine = _covered(hit, length)

        def shadowed(other: Hit, hit: Hit = hit, mine: set[int] = mine) -> bool:
            if (other.part.type, other.part.role) != (hit.part.type, hit.part.role):
                return False
            theirs = _covered(other, length)
            return len(mine & theirs) >= 0.9 * min(len(mine), len(theirs))

        if any(shadowed(other) for other in kept):
            continue
        kept.append(hit)
    return kept


def _overlap(one: Hit, other: Hit, length: int) -> bool:
    return bool(_covered(one, length) & _covered(other, length))


def _covered(hit: Hit, length: int) -> set[int]:
    if hit.end >= hit.start:
        return set(range(hit.start, hit.end + 1))
    return set(range(hit.start, length + 1)) | set(range(1, hit.end + 1))


def features(hits: list[Hit], library: Library) -> list[dict[str, Any]]:
    """``hits`` as a sequence viewer's features (ADR 0085), each saying which part of the
    library it is, how much of it matched, and the record the part's bases are from."""
    out = []
    for hit in hits:
        source = hit.part.source
        qualifiers = {
            "label": hit.part.label,
            "role": hit.part.role,
            "identity": f"{100 * hit.identity:.1f}%",
            "library": f"iki/plasmid features {library.version}",
            "source": (
                f"{source['database']} {source['accession']} {source['location']}"
                f" ({source['record']})"
            ),
        }
        if hit.reading_frame:
            qualifiers["reading_frame"] = hit.reading_frame
        out.append(
            {
                "type": hit.part.type,
                "name": hit.part.name,
                "start": hit.start,
                "end": hit.end,
                "strand": hit.strand,
                "qualifiers": qualifiers,
            }
        )
    return out


def implied(hits: list[Hit]) -> dict[str, list[str]]:
    """What the parts found say about the plasmid: its origins, the antibiotics it's
    selected with in bacteria, its markers in the cells it's expressed in, and its
    promoters. Each value once, in the order the parts lie along the plasmid.

    A gene whose reading frame is interrupted in the plasmid's own bases says nothing of
    what the plasmid is selected with, however well it aligns: the hit stays, and its
    implication doesn't.
    """
    out: dict[str, list[str]] = {}
    for hit in hits:
        if hit.reading_frame == "interrupted":
            continue
        for field, value in hit.part.implies.items():
            values = out.setdefault(field, [])
            if value not in values:
                values.append(value)
    return out

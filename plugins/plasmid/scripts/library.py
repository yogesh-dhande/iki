"""Annotating a plasmid from the library of common vector parts (``data/features.json``).

Each part of the library is the bases of a feature of a public-domain INSDC record (see
``data/README.md``). A part is looked for in the plasmid on both strands, and round its
origin if it's circular, in two steps, as BLAST-based annotators (pLannotate) do:

1. **Seeds.** The plasmid's 18-mers are indexed, and each 18-mer of the part looked up.
   A hit places the part at one offset along the plasmid; a part with no seed isn't
   there at the identity we keep.
2. **Alignment.** Round each placement, the part is aligned locally to a window of the
   plasmid (Biopython's ``PairwiseAligner``, BLASTN's scores: match 2, mismatch -3, a
   gap of n bases 5 + 2(n - 1)), and the alignment's identity is the bases of the part
   that match, over the part's length.

A part is annotated where it's aligned over at least ``min_coverage`` of its length with
at least ``min_identity`` identity; a part under 40 bp must match exactly, since at that
length a few mismatches say nothing. Overlapping placements of one part keep the best,
so a part annotates each place it's found once: attR1 and attR2, or two LTRs, are each
their own hit.

Vector parts drift between vectors (pUC19's beta-lactamase differs from pBR322's at two
bases), so what identity a hit has is kept with it, and shown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from Bio import Align
from Bio.Seq import Seq

#: The seed, long enough that a 4^18 chance is nothing in a plasmid, short enough that a
#: part with mismatches every 20 bases still has one.
SEED = 18
#: Under this many bases, only an exact match is annotated.
EXACT_UNDER = 40
#: BLASTN's scores, as the DNA plugin's read alignments use them: Iki's gap opening cost
#: is the first base's, as EMBOSS and Biopython count it.
MATCH, MISMATCH, OPEN, EXTEND = 2.0, -3.0, -7.0, -2.0

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

    @property
    def span(self) -> int:
        return self.end - self.start


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
            placements: set[int] = set()
            for offset in range(0, max(len(probe) - SEED, 0) + 1, max(SEED // 2, 1)):
                for at in index.get(probe[offset : offset + SEED], ()):
                    placements.add(at - offset)
            found: list[Hit] = []
            for placement in sorted(placements):
                slack = max(5, len(probe) // 20)
                start = max(placement - slack, 0)
                window = doubled[start : placement + len(probe) + slack]
                if len(window) < SEED:
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
            hits.extend(_best_of_overlapping(found, length))
    hits = _best_of_shadowed(hits, length)
    hits.sort(key=lambda hit: (hit.start, -hit.identity, hit.part.name))
    return hits


def _best_of_overlapping(found: list[Hit], length: int) -> list[Hit]:
    """Of hits of one part at overlapping places, the one with the highest identity: one
    placement of a part is one annotation, however many seeds put it there."""
    kept: list[Hit] = []
    for hit in sorted(found, key=lambda h: (-h.identity, h.start)):
        if not any(_overlap(hit, other, length) for other in kept):
            kept.append(hit)
    return kept


def _best_of_shadowed(hits: list[Hit], length: int) -> list[Hit]:
    """Where one part's hit lies almost wholly within another's, the better match alone:
    two parts of a family are alike enough to find each other's places (Gateway's attR1
    and attR2 are 95% identical), and only one of them is there. Parts that merely
    overlap stay, as the SV40 origin within the SV40 early promoter does."""
    kept: list[Hit] = []
    for hit in sorted(hits, key=lambda h: (-h.identity, -h.span, h.part.name)):
        mine = _covered(hit, length)

        def shadowed(other: Hit, mine: set[int] = mine) -> bool:
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
        out.append(
            {
                "type": hit.part.type,
                "name": hit.part.name,
                "start": hit.start,
                "end": hit.end,
                "strand": hit.strand,
                "qualifiers": {
                    "label": hit.part.label,
                    "role": hit.part.role,
                    "identity": f"{100 * hit.identity:.1f}%",
                    "library": f"iki/plasmid features {library.version}",
                    "source": (
                        f"{source['database']} {source['accession']} {source['location']}"
                        f" ({source['record']})"
                    ),
                },
            }
        )
    return out


def implied(hits: list[Hit]) -> dict[str, list[str]]:
    """What the parts found say about the plasmid: its origins, the antibiotics it's
    selected with in bacteria, its markers in the cells it's expressed in, and its
    promoters. Each value once, in the order the parts lie along the plasmid."""
    out: dict[str, list[str]] = {}
    for hit in hits:
        for field, value in hit.part.implies.items():
            values = out.setdefault(field, [])
            if value not in values:
                values.append(value)
    return out

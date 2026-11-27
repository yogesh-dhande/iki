"""Whole-plasmid verification: a plasmid prep's long reads, or a sequencing service's
consensus of them, compared with the plasmid's own registered sequence.

Two actions run this script, `verify_plasmid_reads` and `verify_plasmid_consensus`; the
run's inputs say which. Each creates one `iki/plasmid_sequencing.WholePlasmidAlignment`, an
`iki/core.Alignment` of the plasmid's sequence and the consensus, which the lots plugin's
`record_plasmid_verification` takes as a prep's evidence (ADR 0184). It holds:

- the depth of reads at each base of the plasmid, round its circle, binned for the map,
  with every base in the artifact `coverage.tsv`;
- the consensus, in the reference's frame, from its origin (`consensus.fasta`);
- each difference from the reference, named as HGVS names it, with the features it falls
  in and the reads that support it on each strand (`differences.tsv`).

**Aligning.** minimap2 (Li 2018, https://doi.org/10.1093/bioinformatics/bty191) aligns reads
with its `map-ont` or `map-hifi` preset, and a consensus with `asm5`, and writes each
alignment's differences as its `cs` tag (minimap2's manual, "The cs optional tag"). A
circular sequence is given to minimap2 twice over, end to end, so a read that starts anywhere
on the circle, as a whole-plasmid library's reads do (each molecule is cut once, at random,
before it's read), lies along one stretch of it; each place is then folded back onto the
circle (position mod length). Secondary alignments are left out; supplementary ones (the rest
of a read longer than the plasmid, such as a dimer's) are kept, as more of what was read. A
read that lies wholly within the plasmid aligns as well to either copy, so minimap2 gives it a
mapping quality of 0: folding makes the two places one, so mapping quality isn't used.

**Each read's indels moved 3'.** In a run of repeated bases an aligner may put a read's
deletion or insertion at any place along the run, and reads of one molecule would then each
say a different base is gone, none by a majority. Each indel is moved as far 3' as the read's
matching bases allow before it's counted, the end HGVS's 3' rule names a change at (den
Dunnen et al. 2016, https://doi.org/10.1002/humu.22981; HGVS, "General recommendations": "the
most 3' position possible"). VCF's normalization goes the other way, left (Tan et al. 2015),
so a VCF record of the same deletion starts at the run's first base.

**The consensus (Iki majority consensus, version 1).** This is the plugin's own caller,
named on each result as `iki_majority_1` and on its run as software; a lab with another
validated caller compares that caller's consensus instead. At each base, the reads that
observe it (with a base, or a deletion) are its depth, each read once however many times it
passes the base: a read longer than the plasmid reads some of it twice, but it's one
molecule's evidence, so its passes vote among themselves first, and a read whose passes tie
is one of the reads there that says nothing of which allele. With at least `min_depth`
reads, the consensus is the base, or the deletion, more than half of them have; with no
majority it's N, unresolved. Between two bases, the consensus has an insertion where more
than half the reads that read across that place insert something, and has the inserted bases
most of those reads have; where two sets of bases tie, the insertion is N, as many as most
of them inserted. A base fewer than `min_depth` reads observe is N. Reads disagree most
inside a long insertion, where no single read's bases are a majority's, so the reads are
aligned again to that consensus and it's called again, until it no longer changes (at most
four rounds), as reference-guided polishing does (racon: Vaser et al. 2017,
https://doi.org/10.1101/gr.214270.116).

**Comparing.** The consensus, the reads' or a service's, is then aligned to the reference
as one sequence, with a stretch of its own circle either side so its cut ends don't hide a
difference, and each place on the reference is taken once. Where the reference has an IUPAC
ambiguity code, a consensus base the code allows isn't a difference. Side-by-side changes
are one change. Each is named as HGVS's DNA recommendations name it, numbered from the
reference's first base: `o.` for a circular reference, `g.` for a linear one (HGVS,
"Reference sequences"). It lies in each of the reference's features that holds all of it
(part by part, for a join). Its reads are those across it, each once, and its support those
whose own bases there are closer to its whole allele than to the reference's (see
`support_of`), so a change of several bases is supported only by reads that have all of it.

Every base of the consensus must be placed on the reference once. Bases no alignment holds
(more sequence than the plasmid has) and bases placed where earlier ones already were (such
as a second lap of the circle) are listed as unplaced, and the consensus is then kept as it
was given rather than in the reference's frame.

The result's verdict is `matches` where every base is called, none differs and the whole
consensus is placed once; `differs` where any differs or some of the consensus isn't placed
once; and `incomplete` where none differs but some weren't called.
"""

from __future__ import annotations

import csv
import gzip
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TextIO

import iki_action as iki

#: The bases each IUPAC code stands for (IUBMB, 1985,
#: https://iupac.qmul.ac.uk/misc/naseq.html).
IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip
#: minimap2's preset for each kind of reads (minimap2's manual, `-x`).
PRESETS = {"nanopore": "map-ont", "pacbio_hifi": "map-hifi"}
#: For one consensus against its reference: an assembly that differs from it by well
#: under 1%. Its z-drop and bandwidth are widened so a long insertion or deletion stays
#: inside one alignment rather than ending it.
COMPARE = ["-x", "asm5", "-z", "100000,100000", "-r", "100000,100000"]
#: The most rounds of aligning the reads to their own consensus.
ROUNDS = 4
#: The bins the coverage track is drawn in, at most.
BINS = 360
#: The most differences the record lists: past that, the consensus isn't this plasmid.
MAX_DIFFERENCES = 500
#: The longest sequence compared (a BAC is at most about 300 kb).
MAX_REFERENCE = 1_000_000
#: The circle either side of a consensus, so its cut ends align (at most half the plasmid).
OVERHANG = 1000
#: Inserted bases a difference's name lists; a longer insertion is named by where its bases
#: are in the consensus, as HGVS names one copied from another sequence.
MAX_NAMED = 100
#: The bases a difference's record lists; it keeps its full length.
MAX_SHOWN = 200
#: The run-output schema's limit on a message.
MAX_TEXT = 2000
CS = re.compile(r":[0-9]+|\*[a-z][a-z]|[=+\-][A-Za-z]+|~[a-z]{2}[0-9]+[a-z]{2}")
COMPLEMENT = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")


def fail(why: str) -> NoReturn:
    raise iki.Refused(why)


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


# --- minimap2's alignments ---------------------------------------------------------------


@dataclass
class Event:
    """One of an alignment's differences from its target: the target's bases from `start`
    to `stop` (equal for an insertion) replaced by `bases` (none for a deletion), which are
    the query's from `qstart` to `qstop`."""

    start: int
    stop: int
    bases: str
    qstart: int
    qstop: int


@dataclass
class Hit:
    """One alignment: where it lies on the target and the query, its strand, and its
    differences from the target, in order."""

    read: str
    start: int
    stop: int
    qstart: int
    qstop: int
    forward: bool
    matches: int
    events: list[Event] = field(default_factory=list)


def hits(paf: Iterable[str]) -> Iterator[Hit]:
    """The primary and supplementary alignments in minimap2's PAF output with `--cs`.

    The cs tag runs along the target; on the reverse strand the query bases it gives are
    the query's reverse complement, and the query positions here are along that."""
    for line in paf:
        cols = line.rstrip("\n").split("\t")
        if len(cols) < 12:
            continue
        tags = {tag[:2]: tag[5:] for tag in cols[12:] if len(tag) > 5 and tag[2] == ":"}
        if tags.get("tp") == "S":
            continue
        if "cs" not in tags:
            fail("minimap2 gave an alignment without its cs tag.")
        forward = cols[4] == "+"
        length = int(cols[1])
        qstart = int(cols[2]) if forward else length - int(cols[3])
        hit = Hit(cols[0], int(cols[7]), int(cols[8]), qstart, 0, forward, int(cols[9]))
        spot, q = hit.start, qstart
        for op in CS.findall(tags["cs"]):
            kind, rest = op[0], op[1:]
            if kind == ":":
                spot, q = spot + int(rest), q + int(rest)
            elif kind == "=":
                spot, q = spot + len(rest), q + len(rest)
            elif kind == "*":
                hit.events.append(Event(spot, spot + 1, rest[1].upper(), q, q + 1))
                spot, q = spot + 1, q + 1
            elif kind == "+":
                hit.events.append(Event(spot, spot, rest.upper(), q, q + len(rest)))
                q += len(rest)
            elif kind == "-":
                hit.events.append(Event(spot, spot + len(rest), "", q, q))
                spot += len(rest)
            else:
                fail("minimap2 gave a spliced alignment, which a plasmid's reads can't have.")
        if spot != hit.stop:
            fail(f"minimap2's cs tag for {hit.read} doesn't span its alignment.")
        hit.qstop = q
        yield hit


def normalized(hit: Hit, target: str) -> list[Event]:
    """The hit's events, each deletion and insertion moved as far 3' as the matching bases
    after it allow (HGVS's 3' rule): never into the next event, nor past the alignment."""
    out: list[Event] = []
    for index, each in enumerate(hit.events):
        limit = hit.events[index + 1].start if index + 1 < len(hit.events) else hit.stop
        start, stop, bases, qstart, qstop = (
            each.start,
            each.stop,
            each.bases,
            each.qstart,
            each.qstop,
        )
        if not bases and stop > start:
            while stop < limit and target[start] == target[stop]:
                start, stop, qstart, qstop = start + 1, stop + 1, qstart + 1, qstop + 1
        elif bases and stop == start:
            while start < limit and bases[0] == target[start]:
                bases, start, qstart = bases[1:] + target[start], start + 1, qstart + 1
            stop, qstop = start, qstart + len(bases)
        out.append(Event(start, stop, bases, qstart, qstop))
    return out


def minimap2(args: list[str]) -> list[str]:
    try:
        done = subprocess.run(  # noqa: S603 - minimap2 from the environment, on files made here
            ["minimap2", *args],  # noqa: S607 - the environment's
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        fail("minimap2 isn't installed where this runs.")
    if done.returncode != 0:
        fail(f"minimap2 failed: {done.stderr.strip()[-1500:]}")
    return done.stdout.splitlines(keepends=True)


def minimap2_version() -> str:
    done = subprocess.run(
        ["minimap2", "--version"],  # noqa: S607 - the environment's
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or "unknown"


def threads() -> list[str]:
    return ["-t", str(max(1, min(4, os.cpu_count() or 1)))]


def write_fasta(path: Path, name: str, bases: str) -> Path:
    path.write_text(f">{name}\n{bases}\n", encoding="utf-8")
    return path


# --- The pileup --------------------------------------------------------------------------

#: A read whose passes over one place disagree, as many for each allele: it's one of the
#: reads there, but says nothing of which allele the place has.
UNSURE = "?"


@dataclass
class Pileup:
    """What reads say of each place on a sequence of `size` bases, one vote per read however
    many times it passes the place (a read longer than the plasmid reads some of it more
    than once): `calls[k]`, what they have at base k (a base, or `-` for a deletion), with
    their strand; `inserted[k]`, the bases they insert before base k; and `across[k]`, the
    reads, by strand, that read across the place before base k, so would show an insertion
    there. A read whose passes disagree as much one way as another votes `UNSURE`."""

    size: int
    target: str
    calls: list[Counter[tuple[str, bool]]]
    inserted: list[Counter[tuple[str, bool]]]
    across: list[Counter[bool]]
    reads: dict[str, list[Hit]] = field(default_factory=dict)

    @classmethod
    def of(cls, size: int, target: str = "") -> Pileup:
        return cls(
            size,
            target,
            [Counter() for _ in range(size)],
            [Counter() for _ in range(size)],
            [Counter() for _ in range(size)],
        )

    @property
    def aligned(self) -> set[str]:
        return set(self.reads)

    def depth(self, k: int) -> int:
        return sum(self.calls[k].values())

    def add(self, hit: Hit, target: str) -> None:
        """One alignment of a read; with `add_read`, all of a read's at once."""
        self.add_read([hit], target)

    def add_read(self, passes: list[Hit], target: str) -> None:
        """All of one read's alignments, primary and supplementary: what it has at each
        place, from as many passes as cross it, counted as one read."""
        self.reads.setdefault(passes[0].read, []).extend(passes)
        size = self.size
        bases: dict[int, Counter[tuple[str, bool]]] = {}
        places: dict[int, Counter[tuple[str, bool]]] = {}
        for hit in passes:
            strand, spot = hit.forward, hit.start
            events = normalized(hit, target)
            inserts = {e.start: e.bases for e in events if e.start == e.stop}
            for each in [*events, None]:
                until = each.start if each else hit.stop
                observed = [(t, target[t]) for t in range(spot, until)]
                if each is not None and each.stop > each.start:
                    observed += [
                        (t, each.bases[o] if each.bases else "-")
                        for o, t in enumerate(range(each.start, each.stop))
                    ]
                for t, allele in observed:
                    bases.setdefault(t % size, Counter())[allele, strand] += 1
                    if t > hit.start:
                        places.setdefault(t % size, Counter())[inserts.get(t, ""), strand] += 1
                if each is None:
                    break
                spot = each.stop
        for k, counts in bases.items():
            allele, strand = vote(counts)
            self.calls[k][allele, strand] += 1
        for k, counts in places.items():
            allele, strand = vote(counts)
            self.across[k][strand] += 1
            if allele:
                self.inserted[k][allele, strand] += 1


def vote(counts: Counter[tuple[str, bool]]) -> tuple[str, bool]:
    """One read's allele at a place, from its passes over it: the allele most of them have,
    on the strand most of those were read on. Where two alleles tie, the read is unsure of
    a base (`UNSURE`); of an insertion, it's unsure which bases if every tied allele is
    one, and otherwise inserts nothing it's sure of (""), so it never adds to a majority."""
    ranked = alleles(counts).most_common()
    tied = [a for a, n in ranked if n == ranked[0][1]]
    if len(tied) == 1:
        allele = tied[0]
        return allele, counts[allele, True] >= counts[allele, False]
    allele = UNSURE if all(tied) or "" not in tied else ""
    forward = sum(n for (_, strand), n in counts.items() if strand)
    return allele, 2 * forward >= sum(counts.values())


def alleles(counts: Counter[tuple[str, bool]]) -> Counter[str]:
    out: Counter[str] = Counter()
    for (allele, _), n in counts.items():
        out[allele] += n
    return out


@dataclass
class Call:
    """The consensus at a base, or at the place before it: its allele (a base, `-`, N, or
    inserted bases), the reads there, and those that have it, on each strand."""

    allele: str
    depth: int
    forward: int = 0
    reverse: int = 0

    @property
    def support(self) -> int:
        return self.forward + self.reverse


def base_call(pileup: Pileup, k: int, min_depth: int) -> Call:
    """The allele more than half the reads at base k have; N with no majority, or with
    fewer than `min_depth` reads."""
    counts = pileup.calls[k]
    depth = sum(counts.values())
    if depth == 0 or depth < min_depth:
        return Call("N", depth)
    ranked = [(a, n) for a, n in alleles(counts).most_common() if a != UNSURE]
    if not ranked or 2 * ranked[0][1] <= depth:
        return Call("N", depth)
    allele = ranked[0][0]
    return Call(allele, depth, counts[allele, True], counts[allele, False])


def insert_call(pileup: Pileup, k: int, min_depth: int) -> Call | None:
    """The insertion the consensus has before base k: where more than half of the reads
    across that place insert something, the bases most of those reads inserted. Where the
    most inserted bases are a tie, which bases is unresolved: N, as many as most of those
    reads inserted. Its support is then the reads that insert something there."""
    across = sum(pileup.across[k].values())
    found = pileup.inserted[k]
    present = sum(found.values())
    if across < min_depth or 2 * present <= across:
        return None
    ranked = [(a, n) for a, n in alleles(found).most_common() if a != UNSURE]
    if ranked and (len(ranked) == 1 or ranked[1][1] < ranked[0][1]):
        bases = ranked[0][0]
        return Call(bases, across, found[bases, True], found[bases, False])
    lengths = Counter(len(a) for a, n in ranked for _ in range(n))
    longest = max(lengths.values()) if lengths else 0
    length = min((n for n, c in lengths.items() if c == longest), default=1)
    forward = sum(n for (_, strand), n in found.items() if strand)
    return Call("N" * length, across, forward, present - forward)


def grouped(found: Iterable[Hit]) -> list[list[Hit]]:
    """Each read's alignments, together, in the order the reads first appear."""
    reads: dict[str, list[Hit]] = {}
    for hit in found:
        reads.setdefault(hit.read, []).append(hit)
    return list(reads.values())


def pileup_of(
    paths: list[str], draft: str, circular: bool, preset: str, work: Path
) -> tuple[Pileup, list[str]]:
    target = draft + draft if circular else draft
    ref = write_fasta(work / "draft.fasta", "draft", target)
    paf = minimap2(["-x", preset, "-c", "--cs", "--secondary=no", *threads(), str(ref), *paths])
    pileup = Pileup.of(len(draft), target)
    for passes in grouped(hits(paf)):
        pileup.add_read(passes, target)
    return pileup, paf


def consensus_of(pileup: Pileup, min_depth: int) -> tuple[str, list[Call]]:
    """The consensus a pileup gives, and the call for each of its bases (an insertion's
    bases each take the insertion's call)."""
    bases: list[str] = []
    calls: list[Call] = []
    for k in range(pileup.size):
        inserted = insert_call(pileup, k, min_depth)
        if inserted is not None:
            bases.append(inserted.allele)
            calls.extend([inserted] * len(inserted.allele))
        call = base_call(pileup, k, min_depth)
        if call.allele != "-":
            bases.append(call.allele)
            calls.append(call)
    return "".join(bases), calls


def within(a: str, b: str, limit: int) -> int:
    """The edit distance between `a` and `b`, N matching any base, or `limit + 1` if it's
    more than `limit` (Ukkonen's band: only cells within `limit` of the diagonal)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    over = limit + 1
    previous = [j if j <= limit else over for j in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        low, high = max(1, i - limit), min(len(b), i + limit)
        current = [over] * (len(b) + 1)
        current[0] = i if i <= limit else over
        for j in range(low, high + 1):
            same = a[i - 1] == b[j - 1] or "N" in (a[i - 1], b[j - 1])
            current[j] = min(
                previous[j - 1] + (0 if same else 1), previous[j] + 1, current[j - 1] + 1, over
            )
        previous = current
    return min(previous[len(b)], over)


def segment(events: list[Event], target: str, u: int, length: int) -> tuple[str, int]:
    """What one pass of a read has from base `u` of its target for `length` bases, with
    what it inserts at each place from just before base u to just after the last: the
    read's own bases between the two either side. And the cost of its alignment there (a
    base for each substitution, deleted base and inserted base; none for a base the
    target has as N), which is at least its edit distance from the target's bases."""
    inserts = {e.start: e.bases for e in events if e.start == e.stop}
    changed: dict[int, str] = {}
    for e in events:
        for o, t in enumerate(range(e.start, e.stop)):
            changed[t] = e.bases[o] if e.bases else ""
    out: list[str] = []
    cost = 0
    for t in range(u, u + length + 1):
        out.append(inserts.get(t, ""))
        cost += len(inserts.get(t, ""))
        if t < u + length:
            here = changed.get(t, target[t])
            out.append(here)
            cost += here != target[t] and target[t] != "N"
    return "".join(out), cost


def support_of(pileup: Pileup, qstart: int, qstop: int, was: str, bases: str) -> Call:
    """The reads across one of the consensus's differences, its bases `qstart` to `qstop`
    (none for a deletion), and those that have its whole allele `bases`.

    A read is across it where one of its passes reads the bases either side. A pass has the
    allele where what it read there is closer to `bases` than to the reference's `was`, in
    edit distance with N matching any base; it has the reference's where it's closer to
    that. A read with more passes for the allele than against supports it, on the strand
    most of those passes were read on; a read with as many each way, or closer to neither,
    is one of the reads there but not one of its support. Each read counts once."""
    size, target = pileup.size, pileup.target
    circular = len(target) == 2 * size
    length = qstop - qstart
    forward = reverse = depth = 0
    for passes in pileup.reads.values():
        tally: Counter[tuple[int, bool]] = Counter()
        for hit in passes:
            events = normalized(hit, target)
            for u in range(qstart, len(target), size):
                before = u > hit.start or (u == 0 == hit.start and not circular)
                after = u + length < hit.stop or (
                    u + length == hit.stop == len(target) and not circular
                )
                if not (before and after):
                    continue
                read, cost = segment(events, target, u, length)
                near = within(read, bases, cost)
                far = within(read, was, near)
                tally[(near < far) - (far < near), hit.forward] += 1
        if not tally:
            continue
        depth += 1
        agree, disagree = tally[1, True] + tally[1, False], tally[-1, True] + tally[-1, False]
        if agree > disagree:
            if tally[1, True] >= tally[1, False]:
                forward += 1
            else:
                reverse += 1
    return Call(bases, depth, forward, reverse)


# --- Comparing a consensus with the reference ---------------------------------------------


@dataclass
class Change:
    """A difference from the reference: its bases `start` to `stop` (0-based, end
    exclusive, past its end for one round a circular origin; equal for an insertion)
    replaced by `bases`, which are the consensus's `qstart` to `qstop`."""

    start: int
    stop: int
    bases: str
    qstart: int
    qstop: int


@dataclass
class Comparison:
    """What a consensus says of each base of the reference: `aligned[k]`, its base there
    (`-` where it has none, `.` where it doesn't reach); `before[k]`, the bases it has
    before base k that the reference doesn't; and its changes."""

    aligned: list[str]
    before: list[str]
    changes: list[Change]
    consensus: str
    #: The consensus's bases (0-based, end exclusive, as it was given or turned round to the
    #: reference's strand) that aren't placed on the reference once: `unaligned`, those no
    #: alignment holds, and `repeated`, those placed where earlier bases already were, such as
    #: a second lap of the circle.
    unplaced: list[tuple[int, int, str]] = field(default_factory=list)
    #: The consensus as given, on the reference's strand.
    oriented: str = ""


def compare(reference: str, consensus: str, circular: bool, work: Path) -> Comparison:
    size = len(reference)
    found = len(consensus)
    if circular:
        target = reference * 3
        flank = min(OVERHANG, found // 2, size // 2)
        query = consensus[found - flank :] + consensus + consensus[:flank] if flank else consensus
    else:
        target, flank, query = reference, 0, consensus
    ref = write_fasta(work / "reference.fasta", "reference", target)

    def best(sequence: str) -> list[Hit]:
        path = write_fasta(work / "consensus-query.fasta", "consensus", sequence)
        paf = minimap2([*COMPARE, "-c", "--cs", "--secondary=no", *threads(), str(ref), str(path)])
        return sorted(hits(paf), key=lambda h: -h.matches)

    aligned = best(query)
    if aligned and not aligned[0].forward:
        consensus = reverse_complement(consensus)
        query = reverse_complement(query)
        aligned = best(query)
    # The primary alignment, and supplementary ones on the same strand that don't overlap it.
    kept: list[Hit] = []
    for hit in aligned:
        if hit.forward and all(hit.qstop <= h.qstart or hit.qstart >= h.qstop for h in kept):
            kept.append(hit)

    seen = ["."] * size
    before = [""] * size
    changes: list[Change] = []
    window = range(flank, flank + found)
    # Each of the consensus's bases: placed on the reference once (1), placed where earlier
    # ones already were (2), or not placed (0).
    placed = [0] * found
    taken = [False] * size

    def take(q: int, k: int) -> bool:
        """Places the consensus's base q (in the query) at reference base k, unless an
        earlier one is there: then it's repeated."""
        if q not in window:
            return False
        if taken[k]:
            placed[q - flank] = max(placed[q - flank], 2)
            return False
        taken[k] = True
        placed[q - flank] = max(placed[q - flank], 1)
        return True

    for hit in sorted(kept, key=lambda h: h.qstart):
        t, q = hit.start, hit.qstart
        for each in [*normalized(hit, target), None]:
            until = each.start if each else hit.stop
            for step in range(until - t):
                if take(q + step, (t + step) % size):
                    seen[(t + step) % size] = target[t + step]
            q += until - t
            if each is None:
                break
            here = each.qstart if each.qstop > each.qstart else each.qstart - 1
            if here in window:
                at = each.start % size
                fresh = True
                if each.start == each.stop:
                    for k in range(each.qstart, each.qstop):
                        if k in window:
                            placed[k - flank] = max(placed[k - flank], 1)
                    before[at] += each.bases
                else:
                    for offset in range(each.stop - each.start):
                        k = (each.start + offset) % size
                        query = each.qstart + offset if each.bases else here
                        if taken[k]:
                            fresh = False
                            if query in window:
                                placed[query - flank] = max(placed[query - flank], 2)
                            continue
                        taken[k] = True
                        if each.bases and query in window:
                            placed[query - flank] = max(placed[query - flank], 1)
                        seen[k] = each.bases[offset] if each.bases else "-"
                if fresh:
                    changes.append(
                        Change(
                            each.start,
                            each.stop,
                            each.bases,
                            each.qstart - flank,
                            each.qstop - flank,
                        )
                    )
            t, q = each.stop, each.qstop
    # Runs of the consensus's bases not placed once, each of one kind.
    split: list[tuple[int, int, str]] = []
    for k in range(found):
        if placed[k] == 1:
            continue
        kind = "unaligned" if placed[k] == 0 else "repeated"
        if split and split[-1][1] == k and split[-1][2] == kind:
            split[-1] = (split[-1][0], k + 1, kind)
        else:
            split.append((k, k + 1, kind))
    # The consensus in the reference's frame, from its origin, read off what aligned.
    rebuilt = "".join(before[k] + (seen[k] if seen[k] not in "-." else "") for k in range(size))
    return Comparison(
        seen, before, merged(reference, changes), rebuilt or consensus, split, consensus
    )


def merged(reference: str, changes: list[Change]) -> list[Change]:
    """Changes side by side as one; a consensus base an ambiguous reference code allows
    isn't a change, and nor is an N in the consensus (it's uncalled)."""
    real = [
        c
        for c in changes
        if not (
            c.stop - c.start == 1
            and len(c.bases) == 1
            and (c.bases == "N" or c.bases in IUPAC.get(reference[c.start % len(reference)], ""))
        )
    ]
    out: list[Change] = []
    for c in sorted(real, key=lambda c: (c.start, c.stop)):
        if out and out[-1].stop == c.start and out[-1].qstop == c.qstart:
            last = out[-1]
            out[-1] = Change(last.start, c.stop, last.bases + c.bases, last.qstart, c.qstop)
        else:
            out.append(c)
    return out


# --- HGVS names --------------------------------------------------------------------------


def numbered(circular: bool) -> str:
    """HGVS's prefix for a genomic reference numbered from its first base: `o.` for a
    circular one, `g.` for a linear one (HGVS, "Reference sequences")."""
    return "o." if circular else "g."


def stretch(reference: str, start: int, stop: int) -> str:
    size = len(reference)
    return "".join(reference[k % size] for k in range(start, stop))


def shifted(
    reference: str, start: int, stop: int, bases: str, circular: bool
) -> tuple[int, int, str]:
    """A deletion or insertion moved as far 3' as the bases allow, where HGVS names it;
    round a circular reference's origin, which is no end."""
    size = len(reference)
    if bool(bases) == (stop > start):
        return start, stop, bases
    for _ in range(size):
        if not circular and stop >= size:
            break
        after = reference[stop % size]
        if bases:
            if bases[0] != after:
                break
            bases = bases[1:] + after
        elif after != reference[start % size]:
            break
        start, stop = start + 1, stop + 1
    return start, stop, bases


def place(first: int, last: int) -> str:
    return f"{first + 1}" if first == last else f"{first + 1}_{last + 1}"


def named(reference: str, change: Change, circular: bool, consensus_name: str) -> str:
    """A change's HGVS name: a substitution (`o.12A>G`), deletion (`o.12del`,
    `o.12_14del`), insertion (`o.12_13insT`), duplication (`o.12dup`), inversion
    (`o.12_15inv`), or deletion-insertion (`o.12_13delinsTA`). Inserted bases past
    MAX_NAMED are named by where they are in the consensus (`o.12_13ins[X:o.40_540]`)."""
    size = len(reference)
    prefix = numbered(circular)
    start, stop, bases = shifted(reference, change.start, change.stop, change.bases, circular)

    def written(bases: str) -> str:
        if len(bases) <= MAX_NAMED:
            return bases
        return f"[{consensus_name}:{prefix}{change.qstart + 1}_{change.qstop}]"

    if not bases:
        return f"{prefix}{place(start % size, (stop - 1) % size)}del"
    if start == stop:
        n = len(bases)
        if n <= size and (circular or start >= n) and stretch(reference, start - n, start) == bases:
            return f"{prefix}{place((start - n) % size, (start - 1) % size)}dup"
        if not circular and start >= size:
            start, bases = start - 1, reference[start - 1] + bases[:-1]
        k = start % size
        return f"{prefix}{k if k else size}_{k + 1}ins{written(bases)}"
    was = stretch(reference, start, stop)
    if len(was) == 1 and len(bases) == 1:
        return f"{prefix}{start % size + 1}{was}>{bases}"
    where = place(start % size, (stop - 1) % size)
    if len(bases) == len(was) > 1 and bases == reverse_complement(was):
        return f"{prefix}{where}inv"
    return f"{prefix}{where}delins{written(bases)}"


def homopolymer(reference: str, change: Change, circular: bool) -> int:
    """The longest run of one base in the reference that the change replaces bases of or,
    for an insertion, lies beside: where nanopore reads' systematic errors fall most, as
    their length is hard to read from the signal (Delahaye & Nicolas 2021,
    https://doi.org/10.1371/journal.pone.0257521)."""
    size = len(reference)
    spots = (
        range(change.start, change.stop)
        if change.stop > change.start
        else (change.start - 1, change.start)
    )
    longest = 1
    for k in spots:
        if not circular and not 0 <= k < size:
            continue
        base, run, left, right = reference[k % size], 1, k - 1, k + 1
        while run < size and (circular or left >= 0) and reference[left % size] == base:
            run, left = run + 1, left - 1
        while run < size and (circular or right < size) and reference[right % size] == base:
            run, right = run + 1, right + 1
        longest = max(longest, run)
    return longest


def kind_of(name: str) -> str:
    """A change's kind, as its HGVS name says it (HGVS's DNA recommendations)."""
    if name.endswith("dup"):
        return "duplication"
    if name.endswith("inv"):
        return "inversion"
    if "delins" in name:
        return "deletion_insertion"
    if name.endswith("del"):
        return "deletion"
    if "ins" in name:
        return "insertion"
    return "substitution"


# --- Features ----------------------------------------------------------------------------


def features_of(sequence: dict[str, Any], plasmid: dict[str, Any]) -> list[dict[str, Any]]:
    """The sequence's own features (as GenBank lists them) and those the plasmid's
    annotation found, each once, with their strand as given (`mixed` where a join's parts
    lie on both strands, `none` where it has none) and, for a join or an order, its parts:
    only those hold its bases, not the gaps between them (INSDC Feature Table, "location")."""
    seen: set[tuple[str, str, int, int]] = set()
    out = []
    for each in [*(sequence.get("features") or []), *(plasmid.get("annotated_features") or [])]:
        name = each.get("name") or (each.get("qualifiers") or {}).get("label") or each["type"]
        key = (each["type"], name, each["start"], each["end"])
        if key in seen:
            continue
        seen.add(key)
        strand = each.get("strand")
        strand = {-1: "reverse", 1: "forward", None: "none"}.get(strand, strand)
        found: dict[str, Any] = {
            "type": each["type"],
            "name": name,
            "start": each["start"],
            "end": each["end"],
            "strand": strand if strand in ("forward", "reverse", "mixed", "none") else "none",
        }
        parts = [
            {"start": p["start"], "end": p["end"]}
            for p in each.get("parts") or []
            if not p.get("between")
        ]
        if parts:
            found["parts"] = parts
        out.append(found)
    return out


def holds(feature: dict[str, Any], first: int, last: int, size: int) -> bool:
    """Whether a feature (1-based and inclusive, its end before its start where it wraps
    the origin; part by part where it has parts) holds every base from `first` to `last`
    (0-based, round the origin past the end)."""

    def within(part: dict[str, Any], k: int) -> bool:
        start = part["start"] - 1
        return (k - start) % size <= (part["end"] - 1 - start) % size

    parts = feature.get("parts") or [feature]
    if "parts" not in feature:
        start = feature["start"] - 1
        span = (feature["end"] - 1 - start) % size
        return (first - start) % size + (last - first) <= span
    return all(any(within(p, k % size) for p in parts) for k in range(first, last + 1))


def falls_in(features: list[dict[str, Any]], first: int, last: int, size: int) -> list[str]:
    return [f"{f['name']} ({f['type']})" for f in features if holds(f, first, last, size)][:20]


# --- Coverage ----------------------------------------------------------------------------


def bins(depths: list[int]) -> dict[str, Any]:
    width = max(1, -(-len(depths) // BINS))
    mean, least = [], []
    for start in range(0, len(depths), width):
        part = depths[start : start + width]
        mean.append(round(sum(part) / len(part), 1))
        least.append(min(part))
    return {"bin_size": width, "mean": mean, "min": least}


def regions(spots: list[int], size: int, circular: bool) -> list[tuple[int, int]]:
    """Runs of positions (0-based), one run round a circular origin."""
    out: list[tuple[int, int]] = []
    for k in sorted(spots):
        if out and out[-1][1] == k - 1:
            out[-1] = (out[-1][0], k)
        else:
            out.append((k, k))
    if circular and len(out) > 1 and out[0][0] == 0 and out[-1][1] == size - 1:
        out[0] = (out[-1][0], out[0][1])
        out.pop()
    return out


def spans(found: list[dict[str, int]]) -> str:
    return ", ".join(
        f"{r['start']:,}–{r['end']:,}" if r["start"] != r["end"] else f"{r['start']:,}"
        for r in found
    )


# --- Files -------------------------------------------------------------------------------


def opened(path: Path) -> TextIO:
    with path.open("rb") as raw:
        gzipped = raw.read(2) == b"\x1f\x8b"
    return (
        gzip.open(path, "rt", encoding="utf-8", errors="replace")
        if gzipped
        else path.open(encoding="utf-8", errors="replace")
    )


def fasta_records(path: Path) -> list[tuple[str, str]]:
    """The records of a FASTA file, gzipped or not: name and bases."""
    out: list[tuple[str, list[str]]] = []
    with opened(path) as handle:
        for line in handle:
            if line.startswith(">"):
                out.append(((line[1:].split() or ["consensus"])[0], []))
            elif line.strip() and not line.startswith(";"):
                if not out:
                    fail("The consensus file isn't FASTA: it doesn't start with a > header.")
                out[-1][1].append(line.strip())
    return [(name, "".join(parts).upper().replace("U", "T")) for name, parts in out]


def count_reads(paths: list[Path]) -> int:
    """The reads in FASTQ files, gzipped or not: each record a header (`@`), its bases on
    one line or several, a `+` line, and as many quality characters as bases, on one line
    or several (Cock et al. 2010, https://doi.org/10.1093/nar/gkp1137)."""
    total = 0
    for path in paths:
        with opened(path) as handle:
            lines = (line.rstrip("\r\n") for line in handle)
            for header in lines:
                if not header:
                    continue
                if not header.startswith("@"):
                    fail(f"{path.name} isn't FASTQ: a record starts {header[:20]!r}.")
                bases = 0
                for line in lines:
                    if line.startswith("+"):
                        break
                    bases += len(line.strip())
                else:
                    fail(f"{path.name} ends inside a record.")
                quality = 0
                while quality < bases:
                    line = next(lines, None)
                    if line is None:
                        fail(f"{path.name} ends inside a record's qualities.")
                    quality += len(line.strip())
                if quality != bases:
                    fail(f"{path.name} has a record with more qualities than bases.")
                total += 1
    return total


def unplaced_text(unplaced: list[dict[str, Any]], length: int, hrid: str) -> str:
    """What the consensus has that isn't placed on the reference once, in words."""
    if not unplaced:
        return ""
    kinds = {
        "unaligned": "align nowhere on",
        "repeated": "repeat bases already placed on",
    }
    parts = [
        f"{sum(r['bases'] for r in unplaced if r['why'] == why):,} "
        f"({spans([r for r in unplaced if r['why'] == why][:20])}) {kinds[why]} {hrid}"
        for why in kinds
        if any(r["why"] == why for r in unplaced)
    ]
    return f"Of the consensus's {length:,} bases, " + "; ".join(parts) + "."


def wrap(bases: str, width: int = 70) -> str:
    return "".join(bases[i : i + width] + "\n" for i in range(0, len(bases), width)) or "\n"


# --- The run -----------------------------------------------------------------------------


def verify(run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    (plasmid,) = run["inputs"]["plasmid"]
    (sequence,) = plasmid["included"]["sequence"]
    values = sequence["values"]
    reference = re.sub(r"\s", "", values["bases"]).upper().replace("U", "T")
    circular = values.get("topology") == "circular"
    size = len(reference)
    hrid = sequence["hrid"]
    if not size:
        fail(f"{hrid} has no bases to compare with.")
    if size > MAX_REFERENCE:
        fail(f"{hrid} has {size:,} bases, over the {MAX_REFERENCE:,} this compares.")
    if set(reference) - set(IUPAC):
        fail(f"{hrid} has characters that aren't IUPAC bases.")
    params = run.get("params") or {}
    read_sets = run["inputs"].get("reads") or []
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    title = plasmid["values"].get("name") or plasmid["hrid"]
    consensus_name = f"{plasmid['hrid']}_consensus"
    messages: list[dict[str, str]] = []

    if read_sets:
        (read_set,) = read_sets
        source = "reads"
        platform = params.get("platform") or "nanopore"
        preset = PRESETS[platform]
        min_depth = int(params.get("min_depth") or 10)
        file_ids = [read_set["values"]["read_1"]]
        if read_set["values"].get("read_2"):
            file_ids.append(read_set["values"]["read_2"])
        paths = [run["files"][f]["path"] for f in file_ids]
        print("::progress 0.05 Aligning the reads to the plasmid", flush=True)
        first, paf = pileup_of(paths, reference, circular, preset, work)
        with gzip.open(out_dir / "alignments.paf.gz", "wt", encoding="utf-8") as kept:
            kept.writelines(paf)
        if not first.aligned:
            fail(f"No read aligned to {hrid}: check the reads are this plasmid's.")
        depths = [first.depth(k) for k in range(size)]
        consensus, _ = consensus_of(first, min_depth)
        pileup = first
        rounds = 1
        while rounds < ROUNDS:
            print(
                f"::progress {0.1 + 0.15 * rounds:.2f} Polishing the consensus, round {rounds + 1}",
                flush=True,
            )
            again, _ = pileup_of(paths, consensus, circular, preset, work)
            polished, _ = consensus_of(again, min_depth)
            pileup, rounds = again, rounds + 1
            if polished == consensus:
                break
            consensus = polished
        if pileup.size != len(consensus):
            pileup, _ = pileup_of(paths, consensus, circular, preset, work)
        reads_total = count_reads([Path(p) for p in paths])
        version = minimap2_version()
        method = (
            f"minimap2 {version} (-x {preset} -c --cs --secondary=no)"
            + (", round the circle" if circular else "")
            + f"; Iki majority consensus 1 of at least {min_depth} reads, indels moved 3', "
            f"polished against the reads in {rounds} round{'s' if rounds != 1 else ''}; "
            f"compared with minimap2 -x asm5"
        )
    else:
        source = "consensus"
        given = run["files"][params["consensus"]]
        found = fasta_records(Path(given["path"]))
        if len(found) != 1:
            fail(
                f"{given['name']} holds {len(found)} sequences: give the one consensus the "
                "service assembled for this prep."
            )
        consensus = found[0][1]
        if not consensus or set(consensus) - set(IUPAC):
            fail(f"{given['name']} isn't a DNA sequence in IUPAC codes.")
        min_depth = 1
        depths = []
        method = ""

    print("::progress 0.8 Comparing the consensus with the plasmid", flush=True)
    comparison = compare(reference, consensus, circular, work)
    if source == "consensus":
        method = f"minimap2 {minimap2_version()} (-x asm5 -c --cs), the supplied consensus" + (
            ", round the circle" if circular else ""
        )
        depths = [0 if base == "." else 1 for base in comparison.aligned]
    if len(comparison.changes) > MAX_DIFFERENCES:
        fail(
            f"The consensus differs from {hrid} in {len(comparison.changes):,} places: it "
            "isn't this plasmid."
        )
    features = features_of(values, plasmid["values"])
    listed: list[dict[str, Any]] = []
    for change in comparison.changes:
        was = stretch(reference, change.start, change.stop)
        if change.stop > change.start:
            first_base, last_base = change.start, change.stop - 1
        else:
            first_base, last_base = change.start - 1, change.start
        name = named(reference, change, circular, consensus_name)
        row: dict[str, Any] = {
            "hgvs": name,
            "kind": kind_of(name),
            "start": first_base % size + 1
            if change.stop > change.start
            else change.start % size or size,
            "end": last_base % size + 1,
            "reference": was[:MAX_SHOWN],
            "observed": change.bases[:MAX_SHOWN],
            "length_change": len(change.bases) - len(was),
            "homopolymer": homopolymer(reference, change, circular),
            "features": falls_in(features, first_base, last_base, size),
        }
        if source == "reads":
            # The reads that have the whole of the consensus's allele, each read once.
            call = support_of(pileup, change.qstart, change.qstop, was, change.bases)
            row.update(
                depth=call.depth,
                support=call.support,
                forward=call.forward,
                reverse=call.reverse,
                fraction=round(call.support / call.depth, 3) if call.depth else 0.0,
            )
        listed.append(row)

    unplaced = [
        {"start": a + 1, "end": b, "bases": b - a, "why": why} for a, b, why in comparison.unplaced
    ][:200]
    uncalled = [k for k in range(size) if comparison.aligned[k] in ("N", ".")]
    low = [k for k in range(size) if depths[k] < min_depth] if source == "reads" else uncalled
    low_ranges = regions(low, size, circular)
    low_regions = [{"start": a + 1, "end": b + 1} for a, b in low_ranges[:200]]

    gapped_ref, gapped_con = [], []
    for k in range(size):
        inserted = comparison.before[k]
        gapped_ref.append("-" * len(inserted) + reference[k])
        gapped_con.append(inserted + comparison.aligned[k])
    (out_dir / "alignment.fasta").write_text(
        f">{hrid}\n{wrap(''.join(gapped_ref))}>{consensus_name}\n{wrap(''.join(gapped_con))}",
        encoding="utf-8",
    )
    if unplaced:
        # Only the bases placed once have a place in the reference's frame: keep it whole.
        (out_dir / "consensus.fasta").write_text(
            f">{consensus_name} {title}: the consensus "
            f"{'of the reads' if source == 'reads' else 'the service sent'}, as it was, on "
            f"{hrid}'s strand ({sum(r['bases'] for r in unplaced):,} of its bases aren't "
            f"placed on {hrid} once)\n{wrap(comparison.oriented)}",
            encoding="utf-8",
        )
    else:
        (out_dir / "consensus.fasta").write_text(
            f">{consensus_name} {title}: the consensus "
            f"{'of the reads' if source == 'reads' else 'the service sent'}, in {hrid}'s "
            f"frame\n{wrap(comparison.consensus)}",
            encoding="utf-8",
        )
    with (out_dir / "coverage.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["position", "reference", "depth", "consensus"])
        for k in range(size):
            writer.writerow(
                [k + 1, reference[k], depths[k], comparison.before[k] + comparison.aligned[k]]
            )
    with (out_dir / "differences.tsv").open("w", encoding="utf-8", newline="") as handle:
        columns = ["hgvs", "kind", "start", "end", "reference", "observed", "length_change"]
        columns += ["homopolymer"]
        if source == "reads":
            columns += ["depth", "support", "forward", "reverse", "fraction"]
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow([*columns, "features"])
        for row in listed:
            writer.writerow([*(row[c] for c in columns), "; ".join(row["features"])])

    if listed or unplaced:
        verdict = "differs"
    elif uncalled:
        verdict = "incomplete"
    else:
        verdict = "matches"
    called_percent = round(100 * (size - len(uncalled)) / size, 2)
    summary = {
        "matches": f"The consensus matches {hrid} at all {size:,} bases.",
        "differs": " ".join(
            part
            for part in (
                (
                    f"The consensus differs from {hrid} in {len(listed)} "
                    f"place{'s' if len(listed) != 1 else ''}: "
                    + ", ".join(row["hgvs"] for row in listed[:12])
                    + (", …" if len(listed) > 12 else "")
                    + "."
                )
                if listed
                else "",
                unplaced_text(unplaced, len(comparison.oriented), hrid),
            )
            if part
        ),
        "incomplete": (
            f"No differences from {hrid} where called, but {len(uncalled):,} of its "
            f"{size:,} bases weren't called."
        ),
    }[verdict]
    messages.append(
        {"level": "info" if verdict == "matches" else "warning", "text": summary[:MAX_TEXT]}
    )
    if low_regions:
        text = (
            f"Fewer than {min_depth} reads cover {spans(low_regions[:40])}."
            if source == "reads"
            else (f"The consensus doesn't reach {spans(low_regions[:40])}.")
        )
        if len(low_ranges) > 40:
            text = f"Showing 40 of {len(low_ranges)} stretches. Full coverage: coverage.tsv. {text}"
        messages.append({"level": "warning", "text": text[:MAX_TEXT]})

    entity: dict[str, Any] = {
        "name": (params.get("name") or "").strip()[:200] or f"{title}: whole-plasmid {source}",
        "description": summary[:10000],
        # A registered type's references hold its registry entry (ADRs 0105 and 0115).
        "reference": plasmid["values"]["sequence"],
        "members": [plasmid["values"]["sequence"]],
        "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-na-fasta"},
        "method": method[:500],
        "plasmid": iki.ref(plasmid),
        "plasmid_record_id": plasmid["id"],
        "plasmid_revision": plasmid["revision"],
        "source": source,
        "verdict": verdict,
        "consensus_caller": "iki_majority_1" if source == "reads" else "supplied",
        "reference_length": size,
        "topology": "circular" if circular else "linear",
        "called_percent": called_percent,
        "min_depth": min_depth,
        "coverage": {**bins(depths), "low_count": len(low_ranges)},
        "low_coverage": low_regions,
        "differences": listed,
        "consensus_length": len(comparison.oriented),
        "unplaced": unplaced,
        "features": features[:2000],
        "consensus": {"$file": "consensus.fasta", "media_type": "chemical/seq-na-fasta"},
    }
    derived = [plasmid["id"]]
    metrics: dict[str, Any] = {"differences": len(listed), "called_percent": called_percent}
    if source == "reads":
        library = read_set["included"]["library"][0]
        mean_depth = round(sum(depths) / size, 1)
        entity.update(
            read_set=read_set["id"],
            read_set_revision=read_set["revision"],
            sample=library["values"]["sample"],
            platform=platform,
            reads_total=reads_total,
            reads_aligned=len(first.aligned),
            mean_depth=mean_depth,
            least_depth=min(depths),
            polishing_rounds=rounds,
        )
        derived.append(read_set["id"])
        metrics.update(reads=reads_total, aligned=len(first.aligned), mean_depth=mean_depth)
    else:
        entity.update(consensus_given=params["consensus"])
        if (params.get("provider") or "").strip():
            entity["provider"] = params["provider"].strip()[:200]
    return {
        "creates": [
            {
                "type": "iki/plasmid_sequencing.WholePlasmidAlignment",
                "values": entity,
                "derived_from": derived,
            }
        ],
        "metrics": metrics,
        "messages": messages,
    }


if __name__ == "__main__":
    iki.main(lambda run: verify(run, run.out_dir))

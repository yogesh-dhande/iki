"""CRISPR guide design: where a nuclease can cut, and how particular each guide is
(ADR 0205).

A guide RNA takes a nuclease to one place by base pairing, but only where the DNA also
carries that nuclease's protospacer adjacent motif, which the guide cannot change. So
finding guides is: read both strands for the motif, take the protospacer beside each one,
and then say two things about each -- whether it is likely to work, and whether it is
likely to cut anywhere else.

**What each nuclease needs.**

- *Streptococcus pyogenes* Cas9 (SpCas9) reads `NGG` 3' of a 20-base protospacer and cuts
  both strands bluntly between the third and fourth base 5' of the PAM (Jinek et al.,
  _Science_ 337:816, 2012; Gasiunas et al., _PNAS_ 109:E2579, 2012). It also cuts, less
  well, at `NAG` (Hsu et al., _Nat Biotechnol_ 31:827, 2013), which is why an off-target
  search must look for `NAG` sites even though no design should place a guide on one.
- *Staphylococcus aureus* Cas9 (SaCas9) reads `NNGRRT` 3' of a 21- to 23-base protospacer.
  `NNGRR` is enough for cleavage, so design asks for the T and the off-target search does
  not (Ran et al., _Nature_ 520:186, 2015).
- Cas12a (Cpf1) reads its PAM 5' of the protospacer and cuts staggered, after the 18th
  base of the protospacer strand and the 23rd of the other, leaving a five-base 5'
  overhang (Zetsche et al., _Cell_ 163:759, 2015). _Acidaminococcus_ and _Lachnospiraceae_
  Cas12a read `TTTV` (Kim HK et al., _Nat Methods_ 14:153, 2017); _Francisella_ Cas12a
  reads `TTN` (Zetsche et al., 2015).

**Whether a guide is likely to work.** Each property is reported under the paper that
established it, and none is rolled into one number. Iki does not compute Doench's Rule Set
2 score: its weights were never published, only shipped as a pickled model, so a number
called "the Doench score" here would be a number nobody could check (ADR 0205).

**Whether a guide cuts anywhere else.** Every place in the sequences a design names that
matches the protospacer within so many mismatches, beside a PAM that nuclease cleaves, is
listed with where its mismatches are. For SpCas9 each is scored by the formula the CRISPR
Design tool at crispr.mit.edu published in its documentation, and the guide's specificity
by that site's aggregate formula; both are the site's, not the Hsu 2013 paper's, and are
reported that way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from sequences import gc_content, reverse_complement

#: The IUPAC codes, as the sets of bases they stand for: a PAM is written in them.
IUPAC = {
    "A": "A",
    "C": "C",
    "G": "G",
    "T": "T",
    "R": "AG",
    "Y": "CT",
    "S": "CG",
    "W": "AT",
    "K": "GT",
    "M": "AC",
    "B": "CGT",
    "D": "AGT",
    "H": "ACT",
    "V": "ACG",
    "N": "ACGT",
}


class Nothing(ValueError):  # noqa: N818 - reads as "nothing to design from"
    """A design that can't be worked out, said in words a lab can act on."""


@dataclass(frozen=True)
class Nuclease:
    """One nuclease: what it reads, where it cuts, and what it is cited from."""

    #: What a design calls it.
    name: str
    #: Its own name, for a record a person reads.
    label: str
    #: The PAM a design places a guide on, in IUPAC codes.
    pam: str
    #: The PAM it is known to cleave, which an off-target search must allow. It is the
    #: design PAM or looser, never tighter.
    cleaves: tuple[str, ...]
    #: Whether the PAM lies 3' of the protospacer (Cas9) or 5' of it (Cas12a).
    pam_after: bool
    #: The protospacer lengths a design may use, shortest first.
    spacers: tuple[int, ...]
    #: Where it cuts, counted from the PAM-proximal end of the protospacer: for a blunt
    #: cutter one offset, for a staggered one the two strands' offsets.
    cuts: tuple[int, ...]
    #: Whether the two strands are cut in the same place.
    blunt: bool
    #: Where each of those facts is from.
    cited: str


#: The nucleases a design may name. Each is placed on the PAM its own paper reports, and
#: searched for off-targets on the looser PAM that paper reports it cleaving.
NUCLEASES = {
    "spcas9": Nuclease(
        name="spcas9",
        label="SpCas9 (Streptococcus pyogenes)",
        pam="NGG",
        cleaves=("NGG", "NAG"),
        pam_after=True,
        spacers=(20,),
        cuts=(3,),
        blunt=True,
        cited=(
            "Jinek et al., Science 337:816, 2012, and Gasiunas et al., PNAS 109:E2579, "
            "2012, for the NGG PAM and the blunt cut 3 bp from it; Hsu et al., Nat "
            "Biotechnol 31:827, 2013, for cleavage at NAG"
        ),
    ),
    "sacas9": Nuclease(
        name="sacas9",
        label="SaCas9 (Staphylococcus aureus)",
        pam="NNGRRT",
        cleaves=("NNGRR",),
        pam_after=True,
        spacers=(21, 22, 23),
        cuts=(3,),
        blunt=True,
        cited=(
            "Ran et al., Nature 520:186, 2015, for the NNGRRT PAM, the 21 to 23 base "
            "spacer, and cleavage at NNGRR"
        ),
    ),
    "ascas12a": Nuclease(
        name="ascas12a",
        label="AsCas12a / LbCas12a (Cpf1)",
        pam="TTTV",
        cleaves=("TTTV",),
        pam_after=False,
        spacers=(23,),
        cuts=(18, 23),
        blunt=False,
        cited=(
            "Zetsche et al., Cell 163:759, 2015, for the staggered cut after the 18th "
            "and 23rd bases and the 5-base 5' overhang; Kim HK et al., Nat Methods "
            "14:153, 2017, for the TTTV PAM of AsCas12a and LbCas12a"
        ),
    ),
    "fncas12a": Nuclease(
        name="fncas12a",
        label="FnCas12a (Francisella novicida Cpf1)",
        pam="TTN",
        cleaves=("TTN",),
        pam_after=False,
        spacers=(23,),
        cuts=(18, 23),
        blunt=False,
        cited=(
            "Zetsche et al., Cell 163:759, 2015, for the TTN PAM of FnCpf1 and the "
            "staggered cut after the 18th and 23rd bases"
        ),
    ),
}

#: The weights the CRISPR Design tool at crispr.mit.edu published in its own "Scores of
#: single hits" documentation, one per position of a 20-base protospacer, read from the
#: archived page (https://web.archive.org/web/20181016081208/http://crispr.mit.edu:80/about;
#: the same values in every snapshot from 2013 to 2018). They are the website's: the page
#: attributes the measurements to Hsu et al. (2013), but the paper's text never prints the
#: vector.
#:
#: The website numbers position 1 as the PAM-distal end -- the opposite of the paper,
#: which numbers positions "1-19 directly 5' of the requisite NGG PAM" -- so the vector is
#: written here PAM-distal first, as the website gives it, and read that way. Reading it
#: the paper's way round gives every weight to the wrong base (ADR 0205).
MIT_WEIGHTS = (
    0.000,
    0.000,
    0.014,
    0.000,
    0.000,
    0.395,
    0.317,
    0.000,
    0.389,
    0.079,
    0.445,
    0.508,
    0.613,
    0.851,
    0.732,
    0.828,
    0.615,
    0.804,
    0.685,
    0.583,
)

#: What Pol III reads as a terminator, so a guide carrying it is transcribed short
#: (Bogenhagen and Brown, Cell 24:261, 1981; Doench et al., Nat Biotechnol 32:1262, 2014).
TERMINATOR = "TTTT"
#: The GC band CHOPCHOP keeps a guide inside (Labun et al., Nucleic Acids Res 44:W272,
#: 2016). It is that tool's rule, not a number from the efficiency papers.
GC_BAND = (40.0, 80.0)
#: The longest stretch of a guide that may pair with its own reverse complement before the
#: guide is called self-complementary: a hairpin in the spacer competes with the DNA it is
#: meant to find.
LONGEST_STEM = 4


def matches(bases: str, pattern: str) -> bool:
    """Whether a stretch is one the pattern stands for, reading IUPAC codes in the
    pattern and plain bases in the stretch."""
    if len(bases) != len(pattern):
        return False
    return all(base in IUPAC.get(code, "") for base, code in zip(bases, pattern, strict=True))


def expanded(pattern: str) -> str:
    """A PAM as a regular expression, for finding every place it could be."""
    return "".join(f"[{IUPAC.get(code, 'ACGT')}]" for code in pattern)


@dataclass(frozen=True)
class Site:
    """A place a nuclease could cut: the protospacer, its PAM, and where it lies."""

    #: The protospacer, read 5' to 3' on the strand it lies on -- which is the guide's own
    #: sequence, as DNA.
    protospacer: str
    #: The PAM beside it, read on the same strand.
    pam: str
    #: Which strand it lies on.
    strand: str
    #: Where the protospacer's first base is on the given sequence, counting from 1 on the
    #: forward strand whichever strand the site is on.
    start: int
    #: Where its last base is, counting from 1 the same way.
    end: int
    #: Where the nuclease cuts, counting from 1: one place for a blunt cutter, two for a
    #: staggered one, each the base the cut falls after on the forward strand.
    cuts: tuple[int, ...]


@dataclass(frozen=True)
class Hit:
    """A place in a searched sequence a guide could also cut."""

    #: Which sequence it is in, as the run names it.
    where: str
    #: The protospacer there, on its own strand.
    protospacer: str
    pam: str
    strand: str
    start: int
    #: Which bases of the guide differ there, counting from 1 at the PAM-distal end.
    mismatched: tuple[int, ...]
    #: The single-site score by the CRISPR Design tool's formula, out of 100, where it
    #: applies. A place that matches the guide exactly scores 100.
    score: float | None = None


@dataclass(frozen=True)
class Guide:
    """A guide a design offers, with everything known about it."""

    protospacer: str
    pam: str
    strand: str
    start: int
    end: int
    cuts: tuple[int, ...]
    gc: float
    #: Why a guide may be worse than another, each under the paper that says so.
    flags: tuple[str, ...] = field(default_factory=tuple)
    #: Everywhere else it could cut, nearest first.
    hits: tuple[Hit, ...] = field(default_factory=tuple)
    #: The aggregate specificity by the CRISPR Design tool's formula, where it applies.
    specificity: float | None = None


def sites(bases: str, nuclease: Nuclease, spacer: int, circular: bool) -> list[Site]:
    """Every place in a sequence the nuclease could cut, on both strands.

    A circular sequence has no end, so the search reads past its last base into its first:
    a PAM across the origin is a PAM.
    """
    found: list[Site] = []
    length = len(bases)
    reach = spacer + len(nuclease.pam)
    read = bases + bases[: reach - 1] if circular and length >= reach else bases
    for strand in ("forward", "reverse"):
        strand_bases = read if strand == "forward" else reverse_complement(read)
        for at, protospacer, pam in _places(strand_bases, nuclease, spacer):
            if strand == "forward":
                start, end = at + 1, at + spacer
            else:
                # Read back onto the forward strand's numbering.
                start, end = len(strand_bases) - (at + spacer) + 1, len(strand_bases) - at
            if start > length:
                continue
            found.append(
                Site(
                    protospacer=protospacer,
                    pam=pam,
                    strand=strand,
                    start=(start - 1) % length + 1,
                    end=(end - 1) % length + 1,
                    cuts=_cuts(nuclease, spacer, start, end, strand, length),
                )
            )
    found.sort(key=lambda site: (site.start, site.strand))
    return found


def _places(bases: str, nuclease: Nuclease, spacer: int) -> list[tuple[int, str, str]]:
    """Where a protospacer and its PAM lie on one strand, read 5' to 3' along it."""
    pattern = expanded(nuclease.pam)
    out: list[tuple[int, str, str]] = []
    for found in re.finditer(f"(?=({pattern}))", bases):
        at = found.start()
        pam = found.group(1)
        if nuclease.pam_after:
            start = at - spacer
            if start < 0:
                continue
            out.append((start, bases[start:at], pam))
        else:
            start = at + len(pam)
            if start + spacer > len(bases):
                continue
            out.append((start, bases[start : start + spacer], pam))
    return [place for place in out if set(place[1]) <= set("ACGT")]


def _cuts(
    nuclease: Nuclease, spacer: int, start: int, end: int, strand: str, length: int
) -> tuple[int, ...]:
    """Where the nuclease cuts, as bases of the forward strand: each is the base the cut
    falls after, counting from 1.

    Cas9 cuts a set number of bases in from the PAM-proximal end of the protospacer;
    Cas12a cuts a set number out from the PAM-proximal end, which for it is the other end.
    Which end that is on the forward strand depends on the strand the site is on.
    """
    places = []
    for offset in nuclease.cuts:
        if nuclease.pam_after:
            at = end - offset if strand == "forward" else start + offset - 1
        else:
            at = start + offset - 1 if strand == "forward" else end - offset
        places.append((at - 1) % length + 1)
    return tuple(places)


def flags_for(protospacer: str, band: tuple[float, float]) -> tuple[str, ...]:
    """What is known to make a guide work less well, each under the work that says so.

    None of these is a score: they are the properties a design can see without a model,
    and a lab weighs them itself.
    """
    out = []
    gc = gc_content(protospacer) or 0.0
    if gc < band[0]:
        out.append(f"{gc:.0f}% GC, below the {band[0]:g} to {band[1]:g}% CHOPCHOP keeps to")
    elif gc > band[1]:
        out.append(f"{gc:.0f}% GC, above the {band[0]:g} to {band[1]:g}% CHOPCHOP keeps to")
    if TERMINATOR in protospacer:
        out.append(
            "it carries TTTT, which RNA polymerase III reads as a terminator, so the "
            "guide is transcribed short (Bogenhagen and Brown 1981; Doench et al. 2014)"
        )
    if protospacer[0] != "G":
        out.append(
            "it doesn't begin with a G: the U6 promoter starts transcription at a purine, "
            "so a G is usually added or the first base changed (Mali et al. 2013; Ran et "
            "al., Nat Protoc 8:2281, 2013)"
        )
    stem = self_complementary(protospacer)
    if stem:
        out.append(
            f"{stem} of its bases pair with its own reverse complement, which competes "
            "with the DNA the guide is to find"
        )
    return tuple(out)


def self_complementary(protospacer: str) -> int:
    """The longest stretch of the guide that pairs with another stretch of itself, or 0
    where nothing longer than the limit does."""
    back = reverse_complement(protospacer)
    longest = 0
    for length in range(LONGEST_STEM + 1, len(protospacer) // 2 + 1):
        for at in range(len(protospacer) - length + 1):
            stretch = protospacer[at : at + length]
            where = back.find(stretch)
            # A stretch always finds itself where the two halves overlap; only a pair of
            # stretches that don't overlap can actually fold.
            if where >= 0 and where + length <= len(protospacer) - at - length:
                longest = max(longest, length)
    return longest


def mismatches(guide: str, found: str) -> tuple[int, ...]:
    """Which bases differ, counting from 1 at the PAM-distal end of the protospacer, which
    is how the CRISPR Design tool's weights are numbered."""
    return tuple(
        at + 1 for at, (one, other) in enumerate(zip(guide, found, strict=True)) if one != other
    )


#: The span the distance term is taken over: the 19 gaps between the 20 positions the
#: weights cover. It is a constant in the published formula, not the guide's own length.
MIT_SPAN = 19


def mit_single(places: tuple[int, ...]) -> float:
    """One off-target's score by the formula the CRISPR Design tool published, on that
    tool's own scale of 0 to 100.

    Three terms multiplied, as the page writes them:

        prod over mismatched e of (1 - W[e])  x  1 / ((19 - d) / 19 x 4 + 1)  x  1 / n^2

    where `d` is how far apart the mismatches lie -- the mean of the gaps between them,
    one after another -- and `n` is how many there are. The second term is 1 where there
    are fewer than two mismatches, since there is then no distance to take; a place that
    matches the guide exactly scores 100.

    The page's formula gives a fraction; the tool itself showed these scores out of 100,
    which is also the scale its specificity formula adds up, so that is the scale here.

    The weights are numbered from the PAM-distal end, so `places` must be too. The PAM
    itself is not scored: it is matched or it is not, and which PAM a place has is
    reported beside the score rather than folded into it (ADR 0205).
    """
    first = 1.0
    for at in places:
        first *= 1.0 - MIT_WEIGHTS[min(at, len(MIT_WEIGHTS)) - 1]
    count = len(places)
    if count < 2:
        return first * 100.0
    apart = (places[-1] - places[0]) / (count - 1)
    second = 1.0 / (((MIT_SPAN - apart) / MIT_SPAN) * 4 + 1)
    return first * second / (count * count) * 100.0


def mit_specificity(scores: list[float]) -> float:
    """A guide's specificity by the same tool's aggregate formula,
    `100 / (100 + sum of its off-targets' scores)`, read as a percentage.

    A guide with nothing else to cut scores 100; one with a single perfect match
    elsewhere scores 50. The guide's own site is not one of its off-targets and is left
    out of the sum.
    """
    return 100.0 / (100.0 + sum(scores)) * 100.0


def search(
    guide: str,
    nuclease: Nuclease,
    where: str,
    bases: str,
    circular: bool,
    most: int,
    skip: tuple[str, int, int] | None = None,
) -> list[Hit]:
    """Everywhere in one sequence the guide could also cut: every place matching it within
    `most` mismatches beside a PAM the nuclease is known to cleave.

    The guide's own place is left out, named by `skip`, since a guide is not its own
    off-target.
    """
    spacer = len(guide)
    out: list[Hit] = []
    for pam in nuclease.cleaves:
        for site in sites(bases, replace(nuclease, pam=pam), spacer, circular):
            places = mismatches(guide, site.protospacer)
            if len(places) > most:
                continue
            if skip == (site.strand, site.start, site.end) and site.protospacer == guide:
                continue
            out.append(
                Hit(
                    where=where,
                    protospacer=site.protospacer,
                    pam=site.pam,
                    strand=site.strand,
                    start=site.start,
                    mismatched=places,
                    score=mit_single(places) if nuclease.name == "spcas9" else None,
                )
            )
    # The same place can match on two of the cleaved PAMs (NGG is also an NAG's N): keep
    # one of each, the one with the tighter PAM, which sorting by PAM length gives.
    best: dict[tuple[str, int], Hit] = {}
    for hit in out:
        key = (hit.strand, hit.start)
        if key not in best or len(hit.pam) > len(best[key].pam):
            best[key] = hit
    return sorted(best.values(), key=lambda hit: (len(hit.mismatched), hit.where, hit.start))


def designed(
    bases: str,
    circular: bool,
    nuclease: Nuclease,
    spacer: int,
    region: tuple[int, int] | None,
    searching: list[tuple[str, str, bool]],
    most_mismatches: int,
    band: tuple[float, float],
) -> list[Guide]:
    """Every guide a design offers, worst specificity last.

    `region` is where the cut must fall, counting from 1; a design that names none takes
    the whole sequence. `searching` is the sequences the off-target search reads, each a
    name, its bases and whether it is circular; a design that names none is a design whose
    guides have no specificity to report, which the run says rather than implying the
    guide is particular.
    """
    if spacer not in nuclease.spacers:
        raise Nothing(
            f"{nuclease.label} takes a protospacer of "
            f"{' or '.join(str(each) for each in nuclease.spacers)} bases, not {spacer}."
        )
    found = sites(bases, nuclease, spacer, circular)
    if region is not None:
        first, last = region
        found = [site for site in found if any(first <= cut <= last for cut in site.cuts)]
    if not found:
        where = f"bases {region[0]:,} to {region[1]:,}" if region else "this sequence"
        raise Nothing(
            f"No {nuclease.label} site lies in {where}: it cuts only beside "
            f"{nuclease.pam}, and there is none here on either strand."
        )
    out = []
    for site in found:
        hits: list[Hit] = []
        for name, other, other_circular in searching:
            hits += search(
                site.protospacer,
                nuclease,
                name,
                other,
                other_circular,
                most_mismatches,
                skip=(site.strand, site.start, site.end),
            )
        scores = [hit.score for hit in hits if hit.score is not None]
        out.append(
            Guide(
                protospacer=site.protospacer,
                pam=site.pam,
                strand=site.strand,
                start=site.start,
                end=site.end,
                cuts=site.cuts,
                gc=round(gc_content(site.protospacer) or 0.0, 1),
                flags=flags_for(site.protospacer, band),
                hits=tuple(hits),
                specificity=(
                    round(mit_specificity(scores), 1)
                    if searching and nuclease.name == "spcas9"
                    else None
                ),
            )
        )
    out.sort(
        key=lambda guide: (
            -(guide.specificity if guide.specificity is not None else 0.0),
            len(guide.flags),
            guide.start,
        )
    )
    return out

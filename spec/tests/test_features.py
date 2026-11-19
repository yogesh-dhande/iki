"""A sequence viewer's features, placed along the same molecule from another origin or
strand (ADR 0138), on real circular genomes: every part covers the same bases after.

SV40 (NC_001669.1, 5,243 bp) has its replication origin and the early leader protein's
CDS across the origin of its numbering, and its large T antigen spliced from two exons on
the minus strand. pBR322 (J01749.1, 4,361 bp) has its EcoRI site across its origin, tet on
the plus strand and bla on the minus. Their locations are GenBank's.
"""

import random
from pathlib import Path
from typing import Any

import pytest

from iki_spec.features import place_feature, place_location
from iki_spec.identity import Placement, place

SEQUENCES = Path(__file__).parent / "sequences"
COMPLEMENT = str.maketrans("ACGT", "TGCA")
CODONS = {
    a + b + c: aa
    for (a, b, c), aa in zip(
        ((a, b, c) for a in "TCAG" for b in "TCAG" for c in "TCAG"),
        "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        strict=True,
    )
}


def genome(accession: str) -> str:
    lines = (SEQUENCES / f"{accession}.fasta").read_text().splitlines()
    return "".join(lines[1:])


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def part(start: int, end: int, strand: str = "forward") -> dict[str, Any]:
    return {"start": start, "end": end, "strand": strand}


def feature(
    kind: str,
    name: str,
    start: int,
    end: int,
    strand: str,
    location: str | None = None,
    *parts: dict[str, Any],
) -> dict[str, Any]:
    """A feature as the dna plugin's GenBank import keeps it."""
    out: dict[str, Any] = {"type": kind, "name": name, "start": start, "end": end}
    out["strand"] = strand
    if parts:
        out["parts"] = list(parts)
    if location is not None:
        out["qualifiers"] = {"location": location}
    return out


SV40 = genome("NC_001669.1")
SV40_FEATURES = [
    # Across the origin of its numbering, on the plus strand.
    feature(
        "rep_origin",
        "ori",
        5191,
        31,
        "forward",
        "join(5191..5243,1..31)",
        part(5191, 5243),
        part(1, 31),
    ),
    # Across it on the minus strand: read 16 to 1, then 5,243 to 5,188.
    feature(
        "CDS",
        "SELP",
        5188,
        16,
        "reverse",
        "complement(join(5188..5243,1..16))",
        part(1, 16, "reverse"),
        part(5188, 5243, "reverse"),
    ),
    # Two exons on the minus strand, the second (in the genome's order) read first.
    feature(
        "CDS",
        "large T antigen",
        2691,
        5163,
        "reverse",
        "complement(join(2691..4571,4918..5163))",
        part(4918, 5163, "reverse"),
        part(2691, 4571, "reverse"),
    ),
    feature("CDS", "small t antigen", 4639, 5163, "reverse"),
    feature("CDS", "VP1", 1499, 2593, "forward"),
]
PBR322 = genome("J01749.1")
PBR322_FEATURES = [
    # GAATTC: GAA its last three bases, TTC its first three.
    feature(
        "misc_feature",
        "EcoRI",
        4359,
        3,
        "forward",
        "join(4359..4361,1..3)",
        part(4359, 4361),
        part(1, 3),
    ),
    feature("CDS", "tet", 86, 1276, "forward"),
    feature("CDS", "ROP", 1915, 2106, "forward"),
    feature("CDS", "bla", 3293, 4153, "reverse"),
]


def stretch(bases: str, start: int, end: int) -> str:
    """Bases ``start`` to ``end`` (from 1), round the origin if the end comes first."""
    return bases[start - 1 : end] if start <= end else bases[start - 1 :] + bases[:end]


def read(f: dict[str, Any], bases: str) -> str:
    """What a feature covers, 5' to 3' as it's read: its parts in their order, each from
    its strand."""
    out = []
    for p in f.get("parts", [f]):
        piece = stretch(bases, p["start"], p["end"])
        out.append(reverse_complement(piece) if p["strand"] == "reverse" else piece)
    return "".join(out)


def translated(cds: str) -> str:
    return "".join(CODONS[cds[i : i + 3]] for i in range(0, len(cds) - 2, 3))


def test_the_genbank_features_cover_what_genbank_says() -> None:
    """The fixtures are right: each CDS translates to NCBI's protein."""
    by_name = {f["name"]: read(f, SV40) for f in SV40_FEATURES}
    assert translated(by_name["SELP"]) == "MQRPRPPRPLSYSRSSEEAFLEA*"
    large_t = translated(by_name["large T antigen"])
    assert (len(large_t), large_t[:12], large_t[-13:]) == (709, "MDKVLNREESLQ", "KKPPTPPPEPET*")
    assert translated(by_name["VP1"]).startswith("MKMAPTKRKGSC")
    pbr = {f["name"]: read(f, PBR322) for f in PBR322_FEATURES}
    assert pbr["EcoRI"] == "GAATTC"
    assert translated(pbr["bla"]).startswith("MSIQHFRVALIP")
    assert translated(pbr["tet"]).startswith("MKSNNALIVILG")


# --- Spelling the same molecule otherwise, independently of what's tested ---------------------


def spelled(f: dict[str, Any], length: int, offset: int, reverse: bool) -> dict[str, Any]:
    """``f``, along the entry, written along the same molecule read from its other strand
    if ``reverse``, then from its base ``offset + 1`` on: a base at a time, then in INSDC's
    parts (a run of bases that's one stretch on one strand, split at that origin)."""

    def at(q: int) -> int:
        if reverse:
            q = length + 1 - q
        return (q - 1 - offset) % length + 1

    def flip(strand: str) -> str:
        return {"forward": "reverse", "reverse": "forward"}[strand] if reverse else strand

    bases: list[tuple[int, str]] = []
    for p in f.get("parts", [f]):
        span = [*range(p["start"], p["end"] + 1)]
        if p["start"] > p["end"]:  # round the origin
            span = [*range(p["start"], length + 1), *range(1, p["end"] + 1)]
        ordered = span[::-1] if p["strand"] == "reverse" else span
        bases.extend((at(q), flip(p["strand"])) for q in ordered)
    runs: list[list[tuple[int, str]]] = []
    for q, strand in bases:
        before = runs[-1][-1] if runs else None
        step = -1 if strand == "reverse" else 1
        if before is not None and before[1] == strand and q == before[0] + step:
            runs[-1].append((q, strand))
        else:
            runs.append([(q, strand)])
    parts = [part(min(r[0][0], r[-1][0]), max(r[0][0], r[-1][0]), r[0][1]) for r in runs]
    first, last = at(f["start"]), at(f["end"])
    out = {**f, "start": last if reverse else first, "end": first if reverse else last}
    out["strand"] = flip(f["strand"])
    out.pop("parts", None)
    if len(parts) > 1:
        out["parts"] = parts
    if "qualifiers" in f:
        out["qualifiers"] = {"location": insdc(parts)}
    return out


def insdc(parts: list[dict[str, Any]]) -> str:
    """Parts, in the order they're read, as GenBank writes them."""

    def stretch(p: dict[str, Any]) -> str:
        return f"{p['start']}..{p['end']}"

    def stranded(p: dict[str, Any]) -> str:
        return f"complement({stretch(p)})" if p["strand"] == "reverse" else stretch(p)

    if len(parts) == 1:
        return stranded(parts[0])
    if all(p["strand"] == "reverse" for p in parts):
        return f"complement(join({','.join(stretch(p) for p in reversed(parts))}))"
    return f"join({','.join(stranded(p) for p in parts)})"


def given(bases: str, offset: int, reverse: bool) -> str:
    """The molecule read from its other strand if ``reverse``, then from ``offset + 1``."""
    strand = reverse_complement(bases) if reverse else bases
    return strand[offset:] + strand[:offset]


# --- Placing -------------------------------------------------------------------------------------


GENOMES = [
    pytest.param(SV40, SV40_FEATURES, id="SV40"),
    pytest.param(PBR322, PBR322_FEATURES, id="pBR322"),
]


def placed_back(
    bases: str, features: list[dict[str, Any]], offset: int, reverse: bool
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """The molecule spelled otherwise, its features along it, and those placed back."""
    text = given(bases, offset, reverse)
    placement = place("dna_sequence", text, bases, circular=True, double=True)
    assert placement is not None
    along = [spelled(f, len(bases), offset, reverse) for f in features]
    return text, along, [place_feature(f, placement, len(text), circular=True) for f in along]


@pytest.mark.parametrize(("bases", "features"), GENOMES)
@pytest.mark.parametrize("reverse", [False, True], ids=["rotated", "other strand"])
def test_every_part_lands_on_the_bases_it_was_given_on(
    bases: str, features: list[dict[str, Any]], reverse: bool
) -> None:
    """Started a third of the way round (and from the other strand): the origin-spanning
    parts join, the exons the new origin doesn't cross stay apart, and each part's strand
    and order are as GenBank would write them along the entry."""
    text, along, back = placed_back(bases, features, len(bases) // 3, reverse)
    assert back == features
    for f, original in zip(along, back, strict=True):
        assert read(original, bases) == read(f, text)


def test_an_exon_across_the_given_origin_is_one_part_along_the_entry() -> None:
    """SV40 given from its base 5,001, whose origin falls in the large T antigen's first
    exon: complement(join(2934..4814,5161..5243,1..163)) there, three parts, is its two
    exons along the entry."""
    text = given(SV40, 5000, reverse=False)
    placement = place("dna_sequence", text, SV40, circular=True, double=True)
    # The entry starts at its base 244: 5,243 - 5,000 bases on.
    assert placement == Placement(offset=243)
    large_t = feature(
        "CDS",
        "large T antigen",
        2934,
        163,
        "reverse",
        "complement(join(2934..4814,5161..5243,1..163))",
        part(1, 163, "reverse"),
        part(5161, 5243, "reverse"),
        part(2934, 4814, "reverse"),
    )
    assert place_feature(large_t, placement, len(text), circular=True) == SV40_FEATURES[2]


def test_a_stretch_across_the_entrys_origin_is_two_parts_along_it() -> None:
    """pBR322's EcoRI site given from base 4,001 is one stretch, 359..364. Along the entry
    it crosses the origin, so it's two parts there, as INSDC writes it; and given as
    join(4361,1..5), from base 4,360, it's the same two."""
    text = given(PBR322, 4000, reverse=False)
    placement = place("dna_sequence", text, PBR322, circular=True, double=True)
    assert placement is not None
    site = feature("misc_feature", "EcoRI", 359, 364, "forward", "359..364")
    assert place_feature(site, placement, len(text), circular=True) == PBR322_FEATURES[0]
    text = given(PBR322, 4359, reverse=False)
    placement = place("dna_sequence", text, PBR322, circular=True, double=True)
    assert placement is not None
    across = feature(
        "misc_feature",
        "EcoRI",
        4361,
        5,
        "forward",
        "join(4361,1..5)",
        part(4361, 4361),
        part(1, 5),
    )
    assert place_feature(across, placement, len(text), circular=True) == PBR322_FEATURES[0]


def test_a_site_between_two_bases_stays_between_them() -> None:
    """EcoRI cuts pBR322 between G and AATTC: 4,359^4,360. From the other strand, it's
    between the same two bases, which read the other way."""
    cut = {
        "type": "misc_feature",
        "start": 1,
        "end": 2,
        "strand": "reverse",
        "parts": [{"start": 1, "end": 2, "strand": "reverse", "between": True}],
        "qualifiers": {"location": "complement(1^2)"},
    }
    # Along the other strand from its second base, the entry's 4,360 is 1 and 4,359 is 2.
    text = given(PBR322, 1, reverse=True)
    placement = place("dna_sequence", text, PBR322, circular=True, double=True)
    assert placement == Placement(offset=1, reverse=True)
    assert place_feature(cut, placement, len(text), circular=True) == {
        "type": "misc_feature",
        "start": 4359,
        "end": 4360,
        "strand": "forward",
        "parts": [{"start": 4359, "end": 4360, "strand": "forward", "between": True}],
        "qualifiers": {"location": "4359^4360"},
    }
    # Between the last base and the first, round the origin.
    across = {**cut, "parts": [{"start": 1, "end": 2, "between": True}]}
    out = place_feature(across, Placement(offset=1), len(PBR322), circular=True)
    assert out["parts"] == [{"start": 4361, "end": 1, "between": True}]


def test_a_feature_whose_parts_cross_the_entrys_origin_lists_them() -> None:
    """SV40's ori, given from base 5,001 as one stretch (191..1,274), crosses the entry's
    origin, so it's in two parts there, as INSDC writes it."""
    text = given(SV40, 5000, reverse=False)
    placement = place("dna_sequence", text, SV40, circular=True, double=True)
    assert placement is not None
    ori = {"type": "rep_origin", "name": "ori", "start": 191, "end": 274, "strand": "forward"}
    assert place_feature(ori, placement, len(text), circular=True) == {
        **ori,
        "start": 5191,
        "end": 31,
        "parts": [part(5191, 5243), part(1, 31)],
    }


def test_fuzzy_ends_stay_on_the_ends_they_were_on() -> None:
    """VP1 from a partial clone, <1,499..2,593, given from the other strand: its fuzzy
    start is the 3' end there, complement(2,651..>3,745)."""
    text = given(SV40, 0, reverse=True)
    placement = place("dna_sequence", text, SV40, circular=True, double=True)
    assert placement == Placement(reverse=True)
    assert place_location("complement(2651..>3745)", placement, 5243, circular=True) == (
        "<1499..2593"
    )
    assert place_location("complement(>4)", placement, 5243, circular=True) == "<5240"
    # Back the other way, a mixed join keeps its parts in the order they're read.
    mixed = "join(1..5,complement(10..20))"
    assert place_location(mixed, placement, 5243, circular=True) == (
        "join(complement(5239..5243),5224..5234)"
    )


def test_a_partial_end_stays_on_the_end_it_was_on_in_the_features_parts() -> None:
    """VP1 from a partial clone, <1,499..2,593 along SV40 (INSDC: its start lies beyond
    base 1,499), given from the other strand, is complement(2,651..>3,745) there: placed
    back, its part says its start is partial again, so the viewer writes it, and it isn't
    taken for an exact one-part feature."""
    text = given(SV40, 0, reverse=True)
    placement = place("dna_sequence", text, SV40, circular=True, double=True)
    assert placement == Placement(reverse=True)
    vp1 = {
        "type": "CDS",
        "name": "VP1",
        "start": 2651,
        "end": 3745,
        "strand": "reverse",
        "parts": [{**part(2651, 3745, "reverse"), "partial_end": True}],
        "qualifiers": {"location": "complement(2651..>3745)"},
    }
    back = place_feature(vp1, placement, len(text), circular=True)
    assert back == {
        "type": "CDS",
        "name": "VP1",
        "start": 1499,
        "end": 2593,
        "strand": "forward",
        "parts": [{**part(1499, 2593), "partial_start": True}],
        "qualifiers": {"location": "<1499..2593"},
    }
    assert read(back, SV40) == read(vp1, text)
    assert translated(read(back, SV40)).startswith("MKMAPTKRKGSC")
    # Rotated so its partial start is across the given origin: two parts there, each with
    # its own end, and one again along the entry, the partial start on it.
    text = given(SV40, 2000, reverse=False)
    placement = place("dna_sequence", text, SV40, circular=True, double=True)
    assert placement is not None
    across = {
        "type": "CDS",
        "start": 1499 - 2000 + 5243,
        "end": 593,
        "strand": "forward",
        "parts": [{**part(1499 - 2000 + 5243, 5243), "partial_start": True}, part(1, 593)],
    }
    assert place_feature(across, placement, len(text), circular=True) == {
        "type": "CDS",
        "start": 1499,
        "end": 2593,
        "strand": "forward",
        "parts": [{**part(1499, 2593), "partial_start": True}],
    }


def test_an_orders_parts_are_never_made_one() -> None:
    """INSDC's order(): its elements are found in that order, and nothing is said of
    joining them. Given pBR322 from its base 4,001, two elements either side of that
    origin, 4,300..4,361 and 1..10, are the entry's 3,939..4,000 and 4,001..4,010: two
    still, though a join of them is one stretch."""
    text = given(PBR322, 4000, reverse=False)
    placement = place("dna_sequence", text, PBR322, circular=True, double=True)
    # The entry's first base is its 362nd.
    assert placement == Placement(offset=361)
    parts = [part(4300, 4361), part(1, 10)]
    ordered = {
        "type": "misc_feature",
        "start": 4300,
        "end": 10,
        "strand": "forward",
        "operator": "order",
        "parts": parts,
        "qualifiers": {"location": "order(4300..4361,1..10)"},
    }
    back = place_feature(ordered, placement, len(text), circular=True)
    assert back == {
        **ordered,
        "start": 3939,
        "end": 4010,
        "parts": [part(3939, 4000), part(4001, 4010)],
        "qualifiers": {"location": "order(3939..4000,4001..4010)"},
    }
    assert read(back, PBR322) == read(ordered, text)
    # A join of the same two is one stretch along the entry, so it lists no parts.
    joined = {**ordered, "operator": "join", "qualifiers": {"location": "join(4300..4361,1..10)"}}
    out = place_feature(joined, placement, len(text), circular=True)
    assert out == {
        "type": "misc_feature",
        "start": 3939,
        "end": 4010,
        "strand": "forward",
        "operator": "join",
        "qualifiers": {"location": "3939..4010"},
    }


@pytest.mark.parametrize(
    "text",
    [
        "J02400.1:100..200",  # on another record
        "join(1..2,order(3..4,5..6))",  # join and order at once
        "complement(1..2),complement(3..4)",  # not one location
        "1..6000",  # off its end
        "nucleus",  # not a location at all
    ],
)
def test_text_that_isnt_a_location_here_is_left_as_it_is(text: str) -> None:
    f = {"type": "misc_feature", "start": 1, "end": 2, "qualifiers": {"location": text}}
    out = place_feature(f, Placement(offset=10), 5243, circular=True)
    assert out["qualifiers"] == {"location": text}
    assert place_location(text, Placement(offset=10), 5243, circular=True) is None


def test_a_linear_duplex_from_its_other_strand_only_reverses() -> None:
    """A linear sequence has no origin to cross: a join's parts are only reflected."""
    cds = {
        "type": "CDS",
        "start": 2,
        "end": 12,
        "strand": "forward",
        "parts": [part(2, 4), part(10, 12)],
    }
    assert place_feature(cds, Placement(reverse=True), 12, circular=False) == {
        "type": "CDS",
        "start": 1,
        "end": 11,
        "strand": "reverse",
        "parts": [part(9, 11, "reverse"), part(1, 3, "reverse")],
    }


def test_what_isnt_a_feature_is_left_as_it_is() -> None:
    placement = Placement(offset=3)
    assert place_feature("a note", placement, 10, circular=True) == "a note"
    off = {"type": "CDS", "start": 0, "end": 4}
    assert place_feature(off, placement, 10, circular=True) == off
    broken = {"type": "CDS", "start": 1, "end": 4, "parts": [{"start": 1}]}
    assert place_feature(broken, placement, 10, circular=True)["parts"] == [{"start": 1}]


@pytest.mark.parametrize(("bases", "features"), GENOMES)
def test_from_any_origin_on_either_strand_every_part_covers_the_same_bases(
    bases: str, features: list[dict[str, Any]]
) -> None:
    """A property, over many origins on both strands (fixed seed, so a failure repeats):
    placed back along the entry, every feature is as GenBank writes it there, and reads
    the same bases as it did along what was given."""
    choices = random.Random(138)  # noqa: S311 - not for security
    length = len(bases)
    offsets = [0, 1, length - 1, *choices.sample(range(2, length - 1), 60)]
    # Each origin-spanning feature's own ends, as given origins, split them every way.
    offsets += [f[end] - 1 for f in features for end in ("start", "end")]
    for offset in offsets:
        for reverse in (False, True):
            if offset == 0 and not reverse:
                continue  # the entry itself: there's nothing to place
            text, along, back = placed_back(bases, features, offset, reverse)
            assert back == features, (offset, reverse)
            for f, original in zip(along, back, strict=True):
                assert read(original, bases) == read(f, text), (offset, reverse, f)

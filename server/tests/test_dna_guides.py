"""The DNA plugin's CRISPR guide design, against the papers each part of it is from
(ADR 0205).

Three things have to be right, and each is checked against its own source:

- **Where a nuclease can cut.** SpCas9 reads `NGG` 3' of a 20-base protospacer and cuts
  bluntly between the third and fourth base 5' of the PAM (Jinek et al., _Science_
  337:816, 2012; Gasiunas et al., _PNAS_ 109:E2579, 2012). SaCas9 reads `NNGRRT` 3' of a
  21- to 23-base one (Ran et al., _Nature_ 520:186, 2015). Cas12a reads `TTTV` 5' of the
  protospacer and cuts staggered, after the 18th base of the protospacer strand and the
  23rd of the other, leaving a five-base 5' overhang (Zetsche et al., _Cell_ 163:759,
  2015; Kim HK et al., _Nat Methods_ 14:153, 2017, for `TTTV`). Every site found here is
  read back off the sequence it was found in, so a wrong offset can't pass.
- **What is known to make a guide work less well**, each flagged under the work that says
  so and none of it rolled into a score.
- **Where else a guide could cut**, and how particular that makes it. The formulas are the
  ones the CRISPR Design tool published at crispr.mit.edu, read from the archived page;
  they are checked here against values worked from that published formula by hand, since
  the tool itself is gone and the one surviving implementation of it is not open source
  (ADR 0205).

The sequences are real: pUC19 (GenBank L09137.2) and pBR322 (J01749.1), which the
repository already keeps. pUC19's ampicillin resistance gene and its origin came from
pBR322, so a guide in either has real, checkable matches in the other -- which is exactly
what an off-target search has to find.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from Bio import SeqIO

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "dna" / "scripts"
DATA = Path(__file__).resolve().parent / "data"


def _module(name: str) -> ModuleType:
    """The plugin's own script, loaded as the action loads it: its scripts import each
    other by bare name, so the directory goes on the path first."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    found = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, found)
    spec.loader.exec_module(found)
    return found


guides = _module("guides")
sequences = _module("sequences")

PUC19 = "".join(
    line.strip()
    for line in (DATA / "L09137.2.fasta").read_text().splitlines()
    if not line.startswith(">")
)
PBR322 = str(
    SeqIO.read(DATA / "plasmids" / "J01749.1.gb", "genbank").seq  # type: ignore[no-untyped-call]
).upper()


def at(bases: str, start: int, length: int) -> str:
    """The stretch of a circular sequence starting at `start`, counting from 1."""
    read = bases + bases
    return read[start - 1 : start - 1 + length]


def on_sequence(site: Any, bases: str) -> str:
    """The site's protospacer read back off the sequence it was found in, on the strand it
    was found on: a site whose coordinates are wrong can't survive this."""
    length = site.end - site.start + 1 if site.end >= site.start else len(bases) - site.start + 1
    found = at(bases, site.start, max(length, len(site.protospacer)))[: len(site.protospacer)]
    return found if site.strand == "forward" else sequences.reverse_complement(found)


# --- the published formulas ----------------------------------------------------------


def test_the_weights_are_the_twenty_the_tool_published_pam_distal_first() -> None:
    """The vector as the archived crispr.mit.edu page gives it. Its first positions carry
    almost no weight and its last carry most, which is the seed region next to the PAM:
    read the other way round, every weight would be on the wrong base (ADR 0205)."""
    assert len(guides.MIT_WEIGHTS) == 20
    assert guides.MIT_WEIGHTS[:5] == (0.0, 0.0, 0.014, 0.0, 0.0)
    assert guides.MIT_WEIGHTS[-3:] == (0.804, 0.685, 0.583)
    assert sum(guides.MIT_WEIGHTS[:5]) < sum(guides.MIT_WEIGHTS[-5:])


def test_a_place_that_matches_the_guide_exactly_scores_the_most() -> None:
    """No mismatch means every term of the published formula is 1, so the score is the
    whole 100 the tool's scale allows."""
    assert guides.mit_single(()) == pytest.approx(100.0)


def test_one_mismatch_scores_one_minus_that_positions_weight() -> None:
    """With fewer than two mismatches there is no distance between them to take, so the
    second term is 1 and the third is 1: the score is the first term alone."""
    # The PAM-proximal end carries the most weight, so a mismatch there costs the most.
    assert guides.mit_single((20,)) == pytest.approx(41.7, abs=0.05)
    assert guides.mit_single((20,)) == pytest.approx(100 * (1 - 0.583), abs=1e-9)
    # The PAM-distal end carries none at all, so a mismatch there costs nothing.
    assert guides.mit_single((1,)) == pytest.approx(100.0)
    assert guides.mit_single((11,)) == pytest.approx(100 * (1 - 0.445), abs=1e-9)


def test_two_mismatches_take_the_distance_and_the_count_terms_too() -> None:
    """The published formula, worked by hand for mismatches at 6 and 9:
    (1 - 0.395)(1 - 0.389) x 1 / (((19 - 3) / 19) x 4 + 1) x 1 / 4, times 100."""
    apart = 3.0
    by_hand = (1 - 0.395) * (1 - 0.389) * (1 / (((19 - apart) / 19) * 4 + 1)) * (1 / 4) * 100
    assert guides.mit_single((6, 9)) == pytest.approx(by_hand, abs=1e-9)


def test_the_distance_is_the_mean_of_the_gaps_one_after_another() -> None:
    """Which is not the mean over every pair: for mismatches at 1, 2 and 11 the gaps are 1
    and 9, so the mean is 5, where the mean over all three pairs would be 6 and two-thirds.
    The published formula's own tool used the gaps, and the two give different scores."""
    one_after_another = (11 - 1) / 2
    assert one_after_another == pytest.approx(5.0)
    first = (1 - guides.MIT_WEIGHTS[0]) * (1 - guides.MIT_WEIGHTS[1]) * (1 - guides.MIT_WEIGHTS[10])
    expected = first * (1 / (((19 - 5.0) / 19) * 4 + 1)) * (1 / 9) * 100
    assert guides.mit_single((1, 2, 11)) == pytest.approx(expected, abs=1e-9)


def test_specificity_is_a_hundred_over_a_hundred_plus_what_else_it_could_cut() -> None:
    """A guide with nothing else to cut is wholly particular; one with a single perfect
    match elsewhere is half as particular; adding more only ever makes it less."""
    assert guides.mit_specificity([]) == pytest.approx(100.0)
    assert guides.mit_specificity([100.0]) == pytest.approx(50.0)
    assert guides.mit_specificity([100.0, 100.0]) == pytest.approx(100 / 3, abs=0.01)
    assert guides.mit_specificity([10.0]) == pytest.approx(100 / 1.1, abs=0.01)


# --- where each nuclease cuts --------------------------------------------------------


def test_spcas9_reads_ngg_after_a_20_base_protospacer() -> None:
    found = guides.sites(PUC19, guides.NUCLEASES["spcas9"], 20, circular=True)
    assert found
    for site in found:
        assert len(site.protospacer) == 20
        assert guides.matches(site.pam, "NGG")
        assert on_sequence(site, PUC19) == site.protospacer


def test_spcas9_cuts_bluntly_three_bases_from_the_pam() -> None:
    """Jinek et al. 2012: the cut falls between the third and fourth base 5' of the PAM,
    in the same place on both strands. The record keeps the base the cut falls after, on
    the forward strand, whichever strand the site is on."""
    nuclease = guides.NUCLEASES["spcas9"]
    for site in guides.sites(PUC19, nuclease, 20, circular=True)[:40]:
        assert len(site.cuts) == 1
        cut = site.cuts[0]
        if site.strand == "forward":
            # The protospacer runs start..end with the PAM after it, so the cut is three
            # bases back from its last base.
            assert cut == site.end - 3
        else:
            # On the reverse strand the PAM is before the site's forward start, so the cut
            # is three bases in from that end.
            assert cut == site.start + 2


def test_sacas9_reads_nngrrt_after_a_longer_protospacer() -> None:
    """Ran et al. 2015: a 21- to 23-base spacer and a PAM whose third base is G, fourth
    and fifth are purines and sixth is T."""
    nuclease = guides.NUCLEASES["sacas9"]
    found = guides.sites(PUC19, nuclease, 21, circular=True)
    assert found
    for site in found:
        assert len(site.protospacer) == 21
        assert len(site.pam) == 6
        assert site.pam[2] == "G"
        assert site.pam[3] in "AG"
        assert site.pam[4] in "AG"
        assert site.pam[5] == "T"
        assert on_sequence(site, PUC19) == site.protospacer
    # And 22 and 23 are allowed too, which is what the paper reports.
    assert nuclease.spacers == (21, 22, 23)


def test_cas12a_reads_its_motif_before_the_protospacer() -> None:
    """Kim HK et al. 2017 for TTTV; Zetsche et al. 2015 for the motif lying 5' of the
    protospacer, which is the opposite end from Cas9's."""
    nuclease = guides.NUCLEASES["ascas12a"]
    assert nuclease.pam_after is False
    found = guides.sites(PUC19, nuclease, 23, circular=True)
    assert found
    for site in found:
        assert len(site.protospacer) == 23
        assert site.pam.startswith("TTT")
        assert site.pam[3] in "ACG"  # V is not T
        assert on_sequence(site, PUC19) == site.protospacer


def test_cas12a_cuts_staggered_and_leaves_a_five_base_5_prime_overhang() -> None:
    """Zetsche et al. 2015: after the 18th base of the protospacer strand and the 23rd of
    the other, which is five bases apart however the site lies."""
    nuclease = guides.NUCLEASES["ascas12a"]
    assert nuclease.blunt is False
    found = guides.sites(PUC19, nuclease, 23, circular=True)
    assert found
    for site in found:
        assert len(site.cuts) == 2
        # On a circle the two can lie either side of the origin, so the five bases are
        # counted round it: a site at base 1 cuts after 2,686 and after 5.
        apart = (site.cuts[0] - site.cuts[1]) % len(PUC19)
        assert min(apart, len(PUC19) - apart) == 5


def test_fncas12a_reads_the_looser_ttn_that_its_own_paper_reports() -> None:
    """Zetsche et al. 2015 reports TTN for Francisella Cas12a; the TTTV of the others is
    Kim HK et al. 2017's, for AsCas12a and LbCas12a. They are different enzymes and Iki
    doesn't give one the other's motif."""
    assert guides.NUCLEASES["fncas12a"].pam == "TTN"
    assert guides.NUCLEASES["ascas12a"].pam == "TTTV"
    found = guides.sites(PUC19, guides.NUCLEASES["fncas12a"], 23, circular=True)
    # TTN is looser than TTTV, so it finds more places, and each one is still read back.
    assert len(found) > len(guides.sites(PUC19, guides.NUCLEASES["ascas12a"], 23, circular=True))
    for site in found[:20]:
        assert site.pam.startswith("TT")
        assert on_sequence(site, PUC19) == site.protospacer


def test_a_circle_has_sites_across_its_origin() -> None:
    """A plasmid's last base is next to its first, so a PAM that straddles the join is a
    PAM: a search that read the sequence as a line would miss every guide there."""
    found = guides.sites(PUC19, guides.NUCLEASES["spcas9"], 20, circular=True)
    across = [site for site in found if site.end < site.start]
    assert across, "pUC19 has sites across its origin"
    for site in across:
        assert on_sequence(site, PUC19) == site.protospacer
    # Read as a line, those sites aren't there.
    straight = guides.sites(PUC19, guides.NUCLEASES["spcas9"], 20, circular=False)
    assert len(found) > len(straight)


def test_a_site_with_an_ambiguous_base_in_it_is_not_offered() -> None:
    """A guide has to be ordered as bases, and N is not a base: a stretch Iki doesn't know
    is a stretch no oligo can be made for."""
    bases = "AAAAAAAAAANAAAAAAAAAAAAAAGGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGG"
    found = guides.sites(bases, guides.NUCLEASES["spcas9"], 20, circular=False)
    assert all("N" not in site.protospacer for site in found)
    assert found, "the second site, which has no N in it, is still there"


# --- what makes a guide work less well -----------------------------------------------


def test_a_guide_outside_the_gc_band_is_flagged_with_the_tool_that_set_it() -> None:
    low = guides.flags_for("ATATATATATATATATATAT", guides.GC_BAND)
    assert any("% GC, below" in flag and "CHOPCHOP" in flag for flag in low)
    high = guides.flags_for("GCGCGCGCGCGCGCGCGCGC", guides.GC_BAND)
    assert any("% GC, above" in flag for flag in high)
    middle = guides.flags_for("GACGATCGATCGATCGATGC", guides.GC_BAND)
    assert not any("GC," in flag for flag in middle)


def test_four_ts_in_a_row_are_flagged_as_a_polymerase_iii_terminator() -> None:
    """RNA polymerase III stops at a run of T, so a guide carrying one is transcribed
    short however good its target is (Bogenhagen and Brown 1981; Doench et al. 2014)."""
    flags = guides.flags_for("GACGATCTTTTGATCGATGC", guides.GC_BAND)
    assert any("TTTT" in flag and "terminator" in flag for flag in flags)
    assert not any(
        "TTTT" in flag for flag in guides.flags_for("GACGATCTTTGATCGATTGC", guides.GC_BAND)
    )


def test_a_guide_not_beginning_with_g_is_flagged_for_the_u6_promoter() -> None:
    """U6 starts its transcript at a purine, so a guide not beginning with G is usually
    given one (Mali et al. 2013; Ran et al., Nat Protoc 8:2281, 2013)."""
    flags = guides.flags_for("AACGATCGATCGATCGATGC", guides.GC_BAND)
    assert any("doesn't begin with a G" in flag for flag in flags)
    assert not any(
        "doesn't begin with a G" in flag
        for flag in guides.flags_for("GACGATCGATCGATCGATGC", guides.GC_BAND)
    )


def test_a_guide_that_folds_on_itself_is_flagged() -> None:
    """A stretch of the spacer that pairs with another stretch of itself competes with the
    DNA the guide is meant to find."""
    folded = guides.self_complementary("GGGGGCATGCATAACCCCCA")
    assert folded >= 5
    assert any(
        "pair with its own reverse complement" in flag
        for flag in guides.flags_for("GGGGGCATGCATAACCCCCA", guides.GC_BAND)
    )
    # Nothing is claimed for a guide whose only self-pairings are short.
    assert guides.self_complementary("GATCAAGTTACGAGTTAGTA") == 0


# --- where else a guide could cut ----------------------------------------------------


def test_a_guide_in_a_gene_two_plasmids_share_finds_itself_in_the_other() -> None:
    """pUC19's ampicillin resistance gene came from pBR322, so a guide placed in it has a
    perfect match there. One perfect match elsewhere halves the guide's specificity."""
    nuclease = guides.NUCLEASES["spcas9"]
    found = guides.designed(
        PUC19,
        True,
        nuclease,
        20,
        (1951, 1975),
        [("pUC19", PUC19, True), ("pBR322", PBR322, True)],
        3,
        guides.GC_BAND,
    )
    assert found
    perfect = [guide for guide in found if any(not hit.mismatched for hit in guide.hits)]
    assert perfect, "the shared gene's guides match pBR322 exactly"
    for guide in perfect:
        hit = next(hit for hit in guide.hits if not hit.mismatched)
        assert hit.where == "pBR322"
        assert hit.protospacer == guide.protospacer
        assert hit.score == pytest.approx(100.0)
        assert guide.specificity == pytest.approx(50.0, abs=0.1)
        # And it is really there, in the other plasmid, on the strand the hit says.
        there = at(PBR322, hit.start, 20)
        if hit.strand == "reverse":
            there = sequences.reverse_complement(there)
        assert there == hit.protospacer


def test_a_single_base_different_scores_by_where_it_lies() -> None:
    """The two plasmids' shared stretch has single-base differences in it, so the same
    search gives off-targets with one mismatch -- and a mismatch nearer the PAM costs the
    guide less specificity than one further from it, which is what the weights say."""
    found = guides.designed(
        PUC19,
        True,
        guides.NUCLEASES["spcas9"],
        20,
        (1924, 1956),
        [("pBR322", PBR322, True)],
        3,
        guides.GC_BAND,
    )
    one = [
        (guide, guide.hits[0])
        for guide in found
        if len(guide.hits) == 1 and len(guide.hits[0].mismatched) == 1
    ]
    assert len(one) >= 3, "pUC19 and pBR322 differ by single bases through this stretch"
    for guide, hit in one:
        place = hit.mismatched[0]
        assert hit.score == pytest.approx(100 * (1 - guides.MIT_WEIGHTS[place - 1]), abs=1e-6)
        assert guide.specificity == pytest.approx(100 / (100 + hit.score) * 100, abs=0.1)
        # The guide and what is there differ in exactly that one place.
        differ = [
            n
            for n, (a, b) in enumerate(zip(guide.protospacer, hit.protospacer, strict=True), 1)
            if a != b
        ]
        assert differ == [place]


def test_a_guides_own_site_is_not_one_of_its_off_targets() -> None:
    """A guide is not somewhere else it could cut. Searched against the very sequence it
    was found in, a guide with no repeat anywhere has nothing to report."""
    found = guides.designed(
        PUC19,
        True,
        guides.NUCLEASES["spcas9"],
        20,
        (400, 450),
        [("pUC19", PUC19, True)],
        3,
        guides.GC_BAND,
    )
    assert found
    assert all(not guide.hits for guide in found)
    assert all(guide.specificity == pytest.approx(100.0) for guide in found)


def test_spcas9_off_targets_include_nag_sites_which_no_design_places_a_guide_on() -> None:
    """Hsu et al. 2013 reports cleavage at NAG as well as NGG. A guide is never placed on
    an NAG, since nobody designs for the worse site; a search that ignored them would
    under-report where a guide could cut."""
    nuclease = guides.NUCLEASES["spcas9"]
    assert nuclease.pam == "NGG"
    assert nuclease.cleaves == ("NGG", "NAG")
    assert all(
        guides.matches(site.pam, "NGG") for site in guides.sites(PUC19, nuclease, 20, circular=True)
    )
    # A guide beside an NAG rather than an NGG: nothing would place a guide here, and a
    # search that read only NGG would say this guide cuts nowhere else.
    guide = "GTGGTGTCACGCTCGTCGTT"
    elsewhere = "AAAAAAAAAA" + guide + "CAG" + "AAAAAAAAAA"
    hits = guides.search(guide, nuclease, "a sequence", elsewhere, False, 3, skip=None)
    assert [hit.pam for hit in hits] == ["CAG"]
    assert hits[0].mismatched == ()
    assert hits[0].score == pytest.approx(100.0)
    # The PAM is reported beside the score rather than folded into it, so a reader can
    # tell an NAG hit from an NGG one and weigh it themselves (ADR 0205).
    assert guides.matches(hits[0].pam, "NAG")


def test_a_specificity_is_only_computed_where_there_is_something_to_be_particular_against() -> None:
    """The formula adds up what a guide could also cut. With nothing searched there is
    nothing to add up, and a 100 would say the guide is particular rather than that nobody
    looked."""
    found = guides.designed(
        PUC19, True, guides.NUCLEASES["spcas9"], 20, (400, 450), [], 3, guides.GC_BAND
    )
    assert found
    assert all(guide.specificity is None for guide in found)


def test_no_specificity_is_claimed_for_a_nuclease_the_weights_were_not_fitted_for() -> None:
    """The weights were measured on SpCas9's 20-base protospacer. Giving a Cas12a guide a
    number worked from them would be giving it a number about a different enzyme."""
    found = guides.designed(
        PUC19,
        True,
        guides.NUCLEASES["ascas12a"],
        23,
        (1, 2686),
        [("pBR322", PBR322, True)],
        3,
        guides.GC_BAND,
    )
    assert found
    assert all(guide.specificity is None for guide in found)
    assert all(hit.score is None for guide in found for hit in guide.hits)


# --- the design as a whole -----------------------------------------------------------


def test_only_guides_cutting_inside_the_region_are_offered() -> None:
    """A design names the stretch the cut must fall in -- the exon, the codon, the place a
    cassette goes -- and a guide whose protospacer overlaps it but whose cut doesn't fall
    in it is not a guide for that edit."""
    found = guides.designed(
        PUC19, True, guides.NUCLEASES["spcas9"], 20, (400, 450), [], 3, guides.GC_BAND
    )
    assert found
    for guide in found:
        assert any(400 <= cut <= 450 for cut in guide.cuts)


def test_a_region_with_no_site_in_it_is_said_rather_than_returned_empty() -> None:
    with pytest.raises(guides.Nothing, match="it cuts only beside NGG"):
        guides.designed(
            "ATATATATATATATATATATATATATATATATATATATATATAT",
            False,
            guides.NUCLEASES["spcas9"],
            20,
            None,
            [],
            3,
            guides.GC_BAND,
        )


def test_a_spacer_length_the_nuclease_doesnt_take_is_refused() -> None:
    with pytest.raises(guides.Nothing, match="takes a protospacer of 20 bases, not 23"):
        guides.designed(PUC19, True, guides.NUCLEASES["spcas9"], 23, None, [], 3, guides.GC_BAND)


def test_guides_come_back_most_particular_first() -> None:
    """A reader looks at the top of the list, so what is at the top has to be the guide
    with the least to cut elsewhere, then the one with least against it."""
    found = guides.designed(
        PUC19,
        True,
        guides.NUCLEASES["spcas9"],
        20,
        (1901, 2000),
        [("pBR322", PBR322, True)],
        3,
        guides.GC_BAND,
    )
    assert len(found) > 3
    ranked = [(-(guide.specificity or 0.0), len(guide.flags), guide.start) for guide in found]
    assert ranked == sorted(ranked)

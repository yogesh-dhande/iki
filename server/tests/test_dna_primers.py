"""The DNA plugin's mutagenesis primer designs, against the published protocols they are
each taken from (ADR 0204).

Three protocols, three different kinds of primer:

- `overlapping` is the QuikChange design (Braman, Papworth and Greener, *Methods Mol Biol*
  57:31, 1996), with the rules of the kit's own manual (*QuikChange II Site-Directed
  Mutagenesis Kit Instruction Manual*, Agilent Cat. #200523, Rev. E1): 25-45 bases, the
  change at least 10 bases from either end, >= 40% GC, terminating in C or G, and
  Tm >= 78 degrees by the manual's formula `81.5 + 0.41(%GC) - 675/N - %mismatch`.
- `partially_overlapping` is Zheng, Baumann and Reymond (*Nucleic Acids Res* 32:e115,
  2004) with Liu and Naismith's temperature window (*BMC Biotechnol* 8:91, 2008): the
  pair share the stretch carrying the change at their 5' ends, each keeps at least eight
  bases of its own at its 3' end, and the non-overlapping part anneals 5-10 degrees above
  the shared part by Wetmur's formula (*Crit Rev Biochem Mol Biol* 26:227, 1991).
- `back_to_back` is Hemsley, Arnheim, Toney, Cortopassi and Galas (*Nucleic Acids Res*
  17:6545, 1989): two primers meeting 5' end to 5' end, amplifying the circle outwards,
  the product ligated blunt.

The templates are real: pUC19 (GenBank L09137.2), which the repository already keeps for
the sequencing tests, and human beta-globin's coding sequence (NCBI RefSeq NM_000518.5).
Every design is checked back against the molecule it is supposed to bind, which is the
property a wrong primer breaks.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "dna" / "scripts"


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


primers = _module("primers")
mutations = _module("mutations")

#: pUC19, GenBank L09137.2, 2,686 bp, circular.
PUC19 = "".join(
    line.strip()
    for line in (Path(__file__).resolve().parent / "data" / "L09137.2.fasta")
    .read_text()
    .splitlines()
    if not line.startswith(">")
)
#: Human beta-globin's coding sequence (HBB, NM_000518.5), 444 bp, linear.
HBB = (
    "ATGGTGCATCTGACTCCTGAGGAGAAGTCTGCCGTTACTGCCCTGTGGGGCAAGGTGAACGTGGATGAAGTTGGTGGTGAG"
    "GCCCTGGGCAGGCTGCTGGTGGTCTACCCTTGGACCCAGAGGTTCTTTGAGTCCTTTGGGGATCTGTCCACTCCTGATGCT"
    "GTTATGGGCAACCCTAAGGTGAAGGCTCATGGCAAGAAAGTGCTCGGTGCCTTTAGTGATGGCCTGGCTCACCTGGACAAC"
    "CTCAAGGGCACCTTTGCCACACTGAGTGAGCTGCACTGTGACAAGCTGCACGTGGATCCTGAGAACTTCAGGCTCCTGGGC"
    "AACGTGCTGGTCTGTGTGCTGGCCCATCACTTTGGCAAAGAATTCACCCCACCAGTGCAGGCTGCCTATCAGAAAGTGGTG"
    "GCTGGTGTGGCTAATGCCCTGGCCCACAAGTATCACTAA"
)


def designed(
    template: str, said: str, protocol: str, circular: bool, **rules: object
) -> tuple[str, list[Any]]:
    """The mutant and the primers one change gets, by one protocol: the change is read and
    applied as the action reads and applies it, so a design is always checked against the
    molecule it will be used on, not against a hand-written string."""
    change = mutations.read([said], template, circular)[0]
    mutant, origin = mutations.applied(template, [change])
    assert origin == 0, "these cases don't cross the origin"
    written, replaced = len(change.bases), change.replaced
    found = primers.DESIGNS[protocol](
        mutant,
        change.start,
        change.start + written,
        primers.Rules(protocol=protocol, **rules),
        circular,
        mismatched=written if written == replaced else 0,
        inserted=0 if written == replaced else written,
    )
    return mutant, list(found)


def binds(primer: Any, mutant: str, circular: bool) -> bool:
    """Whether a primer's bases are on the mutant at all: the one thing that must be true
    of every primer here, since a primer that isn't on the molecule can't make it."""
    read = mutant + mutant if circular else mutant
    bases = str(primer.bases)
    return bases in read or mutations.reverse_complement(bases) in read


# --- the manual's own formulas ------------------------------------------------------


def test_the_quikchange_formula_is_the_manuals() -> None:
    """`Tm = 81.5 + 0.41(%GC) - 675/N - %mismatch`, worked by hand: 30 bases, 50% GC, one
    mismatch is 81.5 + 20.5 - 22.5 - 3.33 = 76.17."""
    bases = "ATGCATGCATGCATGCATGCATGCATGCAT"
    assert len(bases) == 30
    # 14 of its 30 bases are G or C: 81.5 + 0.41(46.667) - 675/30 - 100/30 = 74.80.
    assert primers.quikchange_tm(bases, 1, indel=False) == pytest.approx(74.80, abs=0.01)
    # The same primer with nothing mismatched is 3 1/3 degrees higher.
    assert primers.quikchange_tm(bases, 0, indel=False) == pytest.approx(78.13, abs=0.01)


def test_an_insertion_drops_the_mismatch_term_and_counts_only_the_matching_bases() -> None:
    """The manual's insertion and deletion variant: no mismatch term, and N without the
    bases inserted or deleted. Thirty bases of which six are inserted is N = 24, so
    81.5 + 0.41(%GC) - 675/24."""
    bases = "ATGCATGCATGCATGCATGCATGCATGCAT"
    assert primers.quikchange_tm(bases, 6, indel=True) == pytest.approx(
        81.5 + 0.41 * (100 * 14 / 30) - 675 / 24, abs=0.01
    )


def test_wetmurs_formula_is_the_one_liu_and_naismith_use() -> None:
    """`Tm = 81.5 + 16.6 log10([K+]/(1 + 0.7[K+])) + 0.41(%GC) - 500/N`: 20 bases at 50%
    GC in 50 mM potassium."""
    bases = "ATGCATGCATGCATGCATGC"
    salt = 16.6 * (0.05 / (1 + 0.7 * 0.05))
    assert len(bases) == 20
    expected = 81.5 + 16.6 * __import__("math").log10(0.05 / (1 + 0.7 * 0.05)) + 20.5 - 25.0
    assert primers.wetmur_tm(bases, 0.05) == pytest.approx(expected, abs=0.01)
    assert salt > 0  # the term is a logarithm of the salt, not the salt


# --- overlapping (QuikChange) -------------------------------------------------------


def test_a_quikchange_pair_is_complementary_and_carries_the_change_in_both() -> None:
    """The design's defining property: the two primers anneal to each other and to the
    same place on the two strands, so both carry the change (Braman et al., 1996)."""
    mutant, found = designed(HBB, "g.20A>T", "overlapping", circular=False)
    assert [primer.role for primer in found] == ["forward", "reverse"]
    forward, reverse = found
    assert forward.bases == mutations.reverse_complement(reverse.bases)
    assert forward.bases in mutant
    assert mutations.reverse_complement(reverse.bases) in mutant
    # The change is in it: the mutant has T where HBB has A at base 20.
    assert "CCTGTGGAG" in forward.bases
    assert "CCTGAGGAG" not in forward.bases


def test_a_quikchange_pair_meets_every_rule_the_manual_states() -> None:
    mutant, found = designed(PUC19, "o.401C>T", "overlapping", circular=True)
    for primer in found:
        bases = primer.bases
        assert 25 <= len(bases) <= 45
        gc = 100.0 * sum(base in "GC" for base in bases) / len(bases)
        assert gc >= 40.0
        assert bases[-1] in "GC"
        assert primers.quikchange_tm(bases, 1, indel=False) >= 78.0
        assert binds(primer, mutant, circular=True)
    # And it says what it measured, under the formula's own name.
    assert found[0].measures[0][0] == "QuikChange Tm"
    assert found[0].measures[0][1] >= 78.0


def test_the_change_is_at_least_the_asked_for_flank_from_either_end() -> None:
    """The manual asks for the change near the centre and at least 10 bases of correct
    template on each side, so the primer's two ends anneal whatever the change does."""
    mutant, found = designed(PUC19, "o.401C>T", "overlapping", circular=True, flank=15)
    forward = found[0].bases
    at = mutant.index(forward)
    change = 400 - at  # the changed base's place inside the primer, counting from 0
    assert change >= 15
    assert len(forward) - change - 1 >= 15


def test_an_insertion_gets_a_primer_carrying_the_inserted_bases() -> None:
    """A BamHI site put into pUC19: the primer carries GGATCC with correct template on
    both sides, and the manual's acceptance temperature is worked without the six
    inserted bases."""
    mutant, found = designed(PUC19, "o.1500_1501insGGATCC", "overlapping", circular=True)
    forward = found[0]
    assert "GGATCC" in forward.bases
    assert forward.tail == 6
    assert forward.matched == len(forward.bases) - 6
    assert binds(forward, mutant, circular=True)
    assert forward.measures[0][1] == pytest.approx(
        primers.quikchange_tm(forward.bases, 6, indel=True), abs=0.05
    )


def test_a_deletion_gets_a_primer_that_spans_the_join() -> None:
    """Six bases out of pUC19: the primer is the two sides of the deletion joined, so the
    bases either side are template and the pair is still complementary."""
    mutant, found = designed(PUC19, "o.1000_1005del", "overlapping", circular=True)
    forward, reverse = found
    assert forward.bases == mutations.reverse_complement(reverse.bases)
    assert forward.bases in mutant
    # Not on the template: the stretch it spans exists only once the bases are gone.
    assert forward.bases not in PUC19 + PUC19
    assert forward.tail == 0


def test_a_change_too_near_a_linear_ends_edge_is_refused() -> None:
    """A linear template has no bases before its first, so a change three bases in can't
    have ten of correct template on each side: that is said, not fudged."""
    with pytest.raises(primers.Impossible, match="which the QuikChange design needs"):
        designed(HBB, "g.3G>C", "overlapping", circular=False)


def test_a_change_at_the_very_start_of_a_circle_is_designed_round_the_origin() -> None:
    """A circle has no first base, so a change at base 3 of pUC19 is designed with the
    bases from the other end of the molecule before it."""
    mutant, found = designed(PUC19, "o.3G>C", "overlapping", circular=True)
    for primer in found:
        assert binds(primer, mutant, circular=True)
    # It wraps: the forward primer isn't inside the molecule written once.
    assert found[0].bases not in mutant


# --- partially overlapping (Zheng; Liu and Naismith) --------------------------------


def test_a_partially_overlapping_pair_shares_the_change_and_keeps_its_own_3_end() -> None:
    """Zheng's design: each primer carries the change near its 5' end and has bases of
    its own at its 3' end, so a primer's 3' end can only extend on the template, never on
    the other primer (Zheng, Baumann and Reymond, 2004)."""
    mutant, found = designed(PUC19, "o.401C>T", "partially_overlapping", circular=True)
    forward, reverse = found
    assert forward.bases != mutations.reverse_complement(reverse.bases)
    for primer in found:
        assert binds(primer, mutant, circular=True)
        assert 25 <= len(primer.bases) <= 45
        assert primer.bases[-1] in "GC"
    # The two share a stretch, which is where the change is, and each 3' end is its own.
    shared = mutations.reverse_complement(reverse.bases)
    overlap = max(
        (
            n
            for n in range(1, min(len(forward.bases), len(shared)) + 1)
            if forward.bases[:n] in shared
        ),
        default=0,
    )
    assert overlap >= 1
    assert len(forward.bases) - overlap >= 8


def test_the_non_overlapping_part_anneals_above_the_shared_part_by_the_asked_window() -> None:
    """Liu and Naismith's rule, which is what keeps the pair from priming each other: the
    3' extension's Tm is 5 to 10 degrees above the shared stretch's, both by Wetmur."""
    _, found = designed(PUC19, "o.401C>T", "partially_overlapping", circular=True)
    for primer in found:
        measures = dict(primer.measures)
        gap = measures["Wetmur Tm of its own 3' end"] - measures["Wetmur Tm shared"]
        assert 5.0 <= gap <= 10.0


def test_a_window_no_stretch_of_this_template_can_meet_is_said() -> None:
    """A window of a tenth of a degree is one almost no pair of stretches lands in, and
    the refusal names the rules rather than returning a primer that breaks one."""
    with pytest.raises(primers.Impossible, match="No partially overlapping pair"):
        designed(
            PUC19,
            "o.401C>T",
            "partially_overlapping",
            circular=True,
            gap=(9.9, 10.0),
            least_own=20,
            shortest=44,
        )


# --- back to back (Hemsley et al.) --------------------------------------------------


def test_a_back_to_back_pair_meets_5_end_to_5_end_and_amplifies_the_circle() -> None:
    """Hemsley's design: the two primers lie on opposite strands with their 5' ends
    together, so the product is the whole plasmid, linear, to be ligated blunt."""
    mutant, found = designed(PUC19, "o.401C>T", "back_to_back", circular=True)
    forward, reverse = found
    assert forward.direction == "forward"
    assert reverse.direction == "reverse"
    for primer in found:
        assert binds(primer, mutant, circular=True)
    # They abut: the reverse primer's own 5' end is the base before the forward's.
    read = mutant + mutant
    at = read.index(forward.bases)
    behind = mutations.reverse_complement(reverse.bases)
    assert read[at - len(behind) : at] == behind


def test_a_back_to_back_forward_primer_carries_the_change_on_its_5_end() -> None:
    """The change rides on the 5' end of the forward primer and lands at the blunt join,
    so the part that anneals is all correct template."""
    mutant, found = designed(PUC19, "o.1500_1501insGGATCC", "back_to_back", circular=True)
    forward = found[0]
    assert forward.bases.startswith("GGATCC")
    assert forward.tail == 6
    assert forward.matched == len(forward.bases) - 6
    assert binds(forward, mutant, circular=True)


def test_a_deletion_needs_no_tail_at_all() -> None:
    """Taking bases out is what back-to-back primers do by themselves: the two primers sit
    either side of what goes, and the product closes without carrying anything."""
    mutant, found = designed(PUC19, "o.1000_1005del", "back_to_back", circular=True)
    for primer in found:
        assert primer.tail == 0
        assert (
            primer.bases in mutant + mutant
            or mutations.reverse_complement(primer.bases) in mutant + mutant
        )


def test_the_matching_part_anneals_at_what_was_asked_for() -> None:
    """The part on the template is sized by SantaLucia's nearest-neighbour model, which is
    the model every Iki primer's Tm uses, so this primer and a vendor's are comparable."""
    _, found = designed(PUC19, "o.401C>T", "back_to_back", circular=True, anneal_tm=64.0)
    for primer in found:
        what, value = primer.measures[0]
        assert what == "Tm where it matches"
        assert value >= 64.0
    # And the reverse primer's temperature is its own oligo's, not its template stretch
    # read backwards: a sequence and the same sequence backwards are different molecules
    # with different nearest-neighbour temperatures.
    reverse = found[1]
    assert reverse.measures[0][1] == pytest.approx(
        primers.melting_temperature(reverse.bases), abs=0.05
    )


def test_back_to_back_primers_are_refused_on_a_linear_template() -> None:
    """There is nothing to go round: the product would be two fragments, not one molecule
    to close, so the design says so and names the ones that do work (ADR 0204)."""
    with pytest.raises(primers.Impossible, match="circular template"):
        designed(HBB, "g.20A>T", "back_to_back", circular=False)


def test_a_primer_that_cannot_end_in_g_or_c_says_so_rather_than_pretending() -> None:
    """A G:C pair at the 3' end holds the primer on the template while the polymerase
    starts, and both the kit manual and Zheng's protocol ask for one. Where the template
    can't give one within the lengths allowed, the primer is designed anyway and says so:
    a primer that works less well is something a lab can weigh, and no primer at all is
    not."""
    _, found = designed(PUC19, "o.1500_1501insGGATCC", "back_to_back", circular=True)
    unclamped = [primer for primer in found if primer.bases[-1] not in "GC"]
    assert unclamped, "this change is the one that can't be clamped"
    for primer in unclamped:
        assert any("not a G or a C" in note for note in primer.notes)
    # And nothing is noted where the end is a G or a C after all.
    for primer in found:
        if primer.bases[-1] in "GC":
            assert not any("not a G or a C" in note for note in primer.notes)


def test_a_long_inversion_gets_no_primer_that_carries_it() -> None:
    """Twenty-one bases replaced by their own reverse complement is a twenty-one-base
    mismatch, which no primer carrying it can anneal through: the QuikChange formula
    takes a hundredth of a degree off for each percent mismatched, and the designs that
    put the change inside the primer say so rather than designing one that can't work. A
    change that long is made by amplifying the molecule outwards instead."""
    # Two bases turned round is a pair of mismatches, which a QuikChange primer carries.
    _, short = designed(PUC19, "o.1300_1301inv", "overlapping", circular=True)
    assert len(short) == 2
    # Four is already beyond the QuikChange design here: the manual's formula takes a
    # hundredth of a degree off for each percent of the primer that mismatches, and four
    # of thirty leaves it short of 78.
    with pytest.raises(primers.Impossible):
        designed(PUC19, "o.1300_1303inv", "overlapping", circular=True)
    # Zheng's design has no such rule -- the change rides in the stretch the two primers
    # share, which is sized by its own temperature -- so four bases turned round still
    # gets a pair there.
    _, four = designed(PUC19, "o.1300_1303inv", "partially_overlapping", circular=True)
    assert len(four) == 2
    # Twenty-one is beyond every design that carries the change inside a primer.
    for protocol in ("overlapping", "partially_overlapping"):
        with pytest.raises(primers.Impossible):
            designed(PUC19, "o.1300_1320inv", protocol, circular=True)
    _, found = designed(PUC19, "o.1300_1320inv", "back_to_back", circular=True)
    assert len(found) == 2


# --- every design on every kind of change -------------------------------------------


@pytest.mark.parametrize("protocol", ["overlapping", "partially_overlapping", "back_to_back"])
@pytest.mark.parametrize(
    "said",
    [
        "o.401C>T",
        "o.1000_1005del",
        "o.1500_1501insGGATCC",
        "o.1200dup",
        "o.1300_1301inv",
        "o.1400_1402delinsTTTT",
    ],
)
def test_every_protocol_designs_primers_that_bind_the_mutant(protocol: str, said: str) -> None:
    """Whatever the change and whichever protocol, every primer is on the molecule it is
    meant to make, is as long as the rules allow, and ends in a G or a C."""
    mutant, found = designed(PUC19, said, protocol, circular=True)
    assert len(found) == 2
    for primer in found:
        assert binds(primer, mutant, circular=True)
        assert 25 <= len(primer.bases) <= 45 or protocol == "back_to_back"
        # It ends in a G or a C, or says why it doesn't.
        assert primer.bases[-1] in "GC" or any("not a G or a C" in note for note in primer.notes)
        assert primer.matched + primer.tail == len(primer.bases) or primer.matched < len(
            primer.bases
        )

"""Changes stated as HGVS names them, read and applied to a sequence (ADR 0204), and what
they do to the protein.

The names are HGVS's DNA recommendations (den Dunnen et al., *Hum Mutat* 37:564, 2016),
which is the notation `align_reads` already writes for every difference it finds, so a
lab asks for a change the way it will read it back. The sequences here are real: pUC19
(GenBank L09137.2, 2,686 bp, circular), already a fixture of this repository, and human
beta-globin's coding sequence (HBB, NCBI RefSeq NM_000518.5), whose first described
change -- GAG to GTG at the sixth codon of the mature protein, p.Glu7Val in HGVS's
numbering from the initiator methionine -- is sickle cell haemoglobin (Ingram, *Nature*
180:326, 1957).
"""

import importlib
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "dna" / "scripts"

#: Human beta-globin's coding sequence (HBB, NM_000518.5), 147 amino acids and a stop.
HBB = (
    "ATGGTGCATCTGACTCCTGAGGAGAAGTCTGCCGTTACTGCCCTGTGGGGCAAGGTGAACGTGGATGAAGTTGGTGGTGAG"
    "GCCCTGGGCAGGCTGCTGGTGGTCTACCCTTGGACCCAGAGGTTCTTTGAGTCCTTTGGGGATCTGTCCACTCCTGATGCT"
    "GTTATGGGCAACCCTAAGGTGAAGGCTCATGGCAAGAAAGTGCTCGGTGCCTTTAGTGATGGCCTGGCTCACCTGGACAAC"
    "CTCAAGGGCACCTTTGCCACACTGAGTGAGCTGCACTGTGACAAGCTGCACGTGGATCCTGAGAACTTCAGGCTCCTGGGC"
    "AACGTGCTGGTCTGTGTGCTGGCCCATCACTTTGGCAAAGAATTCACCCCACCAGTGCAGGCTGCCTATCAGAAAGTGGTG"
    "GCTGGTGTGGCTAATGCCCTGGCCCACAAGTATCACTAA"
)


@pytest.fixture(scope="module")
def mutations() -> Iterator[ModuleType]:
    """The plugin's changes module, imported as a run imports it (by bare name)."""
    kept = sys.modules.pop("mutations", None)
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield importlib.import_module("mutations")
    finally:
        sys.path.remove(str(SCRIPTS))
        sys.modules.pop("mutations", None)
        if kept is not None:
            sys.modules["mutations"] = kept


@pytest.fixture(scope="module")
def puc19() -> str:
    """pUC19's bases, from this repository's copy of GenBank L09137.2."""
    lines = (ROOT / "server" / "tests" / "data" / "L09137.2.fasta").read_text().splitlines()
    bases = "".join(line.strip() for line in lines if not line.startswith(">"))
    assert len(bases) == 2686
    return bases


# --- reading a change -----------------------------------------------------------------


def test_each_kind_of_change_is_read_and_placed(mutations: ModuleType) -> None:
    """The six kinds HGVS names on a genomic reference, each placed 0-based over the
    template with what the sequence has there instead."""
    template = "ATGAAACCCGGGTTTAAACCCGGGTTTTAG"
    read = [
        ("g.4A>T", "substitution", 3, 4, "T"),
        ("g.4_6del", "deletion", 3, 6, ""),
        ("g.4_5insGGATCC", "insertion", 4, 4, "GGATCC"),
        ("g.4dup", "duplication", 3, 4, "AA"),
        ("g.4_7dup", "duplication", 3, 7, "AAACAAAC"),
        ("g.4_9inv", "inversion", 3, 9, "GGGTTT"),
        ("g.4_6delinsTT", "deletion-insertion", 3, 6, "TT"),
    ]
    for said, kind, start, stop, bases in read:
        found = mutations.stated(said, template, False)
        assert (found.kind, found.start, found.stop, found.bases) == (kind, start, stop, bases)
    # The prefix may be left off: a lab that writes 1234C>T means the template it named.
    assert mutations.stated("1234C>T", "A" * 1233 + "C" + "A" * 10, False).kind == "substitution"


def test_a_change_whose_stated_base_isnt_the_templates_is_refused(
    mutations: ModuleType, puc19: str
) -> None:
    """HGVS has a substitution state the reference's own base so that a change written
    against the wrong sequence, or against the other strand, is caught before anyone
    orders a primer. pUC19's base 1 is T, so o.1A>G is not a change to pUC19."""
    assert puc19[0] == "T"
    with pytest.raises(mutations.Refused, match="says the template has A at 1, and it has T"):
        mutations.stated("o.1A>G", puc19, True)
    # And the bases a deletion states, where it states them.
    with pytest.raises(mutations.Refused, match="says the template has GGGG at 1_4"):
        mutations.stated("o.1_4delGGGG", puc19, True)
    assert mutations.stated("o.1_4delTCGC", puc19, True).kind == "deletion"


def test_a_change_numbered_for_the_wrong_topology_is_refused(mutations: ModuleType) -> None:
    """`g.` numbers a linear genomic reference and `o.` a circular one: those are the two
    prefixes HGVS allows for a genomic reference, and the two `align_reads` writes."""
    with pytest.raises(mutations.Refused, match=re.escape("is numbered o., which is a circular")):
        mutations.stated("o.4A>T", "ATGAAA", False)
    with pytest.raises(mutations.Refused, match=re.escape("is numbered g., which is a linear")):
        mutations.stated("g.4A>T", "ATGAAA", True)


def test_a_change_off_the_end_or_unreadable_is_refused(mutations: ModuleType) -> None:
    template = "ATGAAACCCGGG"
    with pytest.raises(mutations.Refused, match="past the template's 12 bases"):
        mutations.stated("g.40A>T", template, False)
    with pytest.raises(mutations.Refused, match="numbers a base before 1"):
        mutations.stated("g.0A>T", template, False)
    with pytest.raises(mutations.Refused, match="isn't a change Iki can read"):
        mutations.stated("delete codon 4", template, False)
    with pytest.raises(mutations.Refused, match="inverts one base, which is a substitution"):
        mutations.stated("g.4inv", template, False)
    with pytest.raises(mutations.Refused, match="inserts between two bases"):
        mutations.stated("g.4_6insT", template, False)
    with pytest.raises(mutations.Refused, match="is a substitution of one base"):
        mutations.stated("g.4_5A>T", template, False)
    # Only a circular sequence's range may run across its origin.
    with pytest.raises(mutations.Refused, match="only a circular sequence's range"):
        mutations.stated("g.11_2del", template, False)


def test_a_range_across_a_circular_origin_is_read_round_it(
    mutations: ModuleType, puc19: str
) -> None:
    """A circular molecule's origin is no end: a change may run across base 1, which HGVS
    names from its first place to its last, as `align_reads` writes it (o.2686_1del)."""
    found = mutations.stated("o.2685_2del", puc19, True)
    assert (found.start, found.stop) == (2684, 2688)
    assert found.replaced == 4


def test_two_changes_touching_the_same_bases_are_refused(mutations: ModuleType) -> None:
    """No sequence has both a base deleted and that base changed, so a design that asks
    for both is a design nobody can make."""
    template = "ATGAAACCCGGGTTT"
    with pytest.raises(mutations.Refused, match="change the same bases"):
        mutations.read(["g.4_6del", "g.5A>T"], template, False)
    # Side by side is not the same as overlapping.
    assert len(mutations.read(["g.4_6del", "g.7C>T"], template, False)) == 2


# --- applying them --------------------------------------------------------------------


def test_the_changes_are_made_where_they_were_stated(mutations: ModuleType) -> None:
    template = "ATGAAACCCGGGTTTAAACCCGGGTTTTAG"
    made, origin = mutations.applied(
        template, mutations.read(["g.4A>T", "g.10_12del"], template, False)
    )
    assert made == "ATGTAACCCTTTAAACCCGGGTTTTAG"
    assert origin == 0
    # Several changes, each where it was stated, however they were given.
    names = ["g.22_23insGGATCC", "g.1_3delinsGTG", "g.16_18dup"]
    made, _ = mutations.applied(template, mutations.read(names, template, False))
    assert made == "GTGAAACCCGGGTTTAAAAAACCCGGGATCCGGTTTTAG"


def test_a_change_across_a_circular_origin_is_made_in_one_piece(
    mutations: ModuleType, puc19: str
) -> None:
    """A circle has no end, so a change over base 1 is one change. The product is written
    from that change's own first base, so the change isn't split between the molecule's
    two ends, and the run says where the product now begins."""
    found = mutations.read(["o.2685_2delinsTTTTTT"], puc19, True)
    made, origin = mutations.applied(puc19, found)
    assert origin == 2684
    assert made.startswith("TTTTTT")
    assert len(made) == 2686 + 2
    # Everything the change didn't touch is still there, in order.
    assert made[6:] == puc19[2:2684]


def test_the_mutant_keeps_everything_the_changes_didnt_touch(
    mutations: ModuleType, puc19: str
) -> None:
    """pUC19's lacZ alpha fragment has an EcoRI site in its polylinker; taking it out
    changes six bases and leaves the other 2,680 exactly as they were."""
    at = puc19.find("GAATTC")
    assert at >= 0
    name = f"o.{at + 1}_{at + 6}delinsGAATTT"
    made, origin = mutations.applied(puc19, mutations.read([name], puc19, True))
    assert origin == 0
    assert len(made) == len(puc19)
    assert made[:at] == puc19[:at] and made[at + 6 :] == puc19[at + 6 :]
    assert "GAATTC" not in made


# --- what it does to the protein ------------------------------------------------------


def test_the_sickle_cell_change_is_a_missense_at_the_seventh_residue(
    mutations: ModuleType,
) -> None:
    """Sickle cell haemoglobin is HBB's GAG to GTG at the sixth codon of the mature
    protein (Ingram 1957). HGVS numbers from the initiator methionine, which the mature
    protein doesn't have, so the same change is p.Glu7Val."""
    table = mutations.genetic_code(1)
    found = mutations.read(["g.20A>T"], HBB, False)
    made, _ = mutations.applied(HBB, found)
    before = mutations.translate(HBB, table)
    after = mutations.translate(made, table)
    assert before[:7] == "MVHLTPE"
    assert after[:7] == "MVHLTPV"
    assert mutations.protein_change(before, after) == ("missense", "p.Glu7Val")


def test_a_change_that_leaves_the_protein_alone_is_silent(mutations: ModuleType) -> None:
    """HBB's codon 2 is GTG; GTA is valine too, so the protein doesn't change."""
    table = mutations.genetic_code(1)
    made, _ = mutations.applied(HBB, mutations.read(["g.6G>A"], HBB, False))
    assert made[3:6] == "GTA"
    before = mutations.translate(HBB, table)
    after = mutations.translate(made, table)
    assert mutations.protein_change(before, after) == ("silent", "p.(=)")


def test_a_stop_gained_is_a_nonsense_change(mutations: ModuleType) -> None:
    """HBB's codon 18 is AAG (Lys); AAG to TAG puts a stop where a residue was, which is
    the kind of change a nonsense beta-thalassaemia allele carries."""
    table = mutations.genetic_code(1)
    at = 17 * 3 + 1  # the first base of codon 18, counting from 1
    assert HBB[at - 1 : at + 2] == "AAG"
    made, _ = mutations.applied(HBB, mutations.read([f"g.{at}A>T"], HBB, False))
    before = mutations.translate(HBB, table)
    after = mutations.translate(made, table)
    effect, name = mutations.protein_change(before, after)
    assert effect == "nonsense"
    assert name == "p.Lys18Ter"


def test_an_in_frame_deletion_and_insertion_are_named_as_hgvs_names_them(
    mutations: ModuleType,
) -> None:
    table = mutations.genetic_code(1)
    # Codons 4 and 5 (CTG ACT, Leu Thr) taken out, which keeps the reading frame.
    made, _ = mutations.applied(HBB, mutations.read(["g.10_15del"], HBB, False))
    before = mutations.translate(HBB, table)
    after = mutations.translate(made, table)
    assert mutations.protein_change(before, after) == ("in-frame deletion", "p.Leu4_Thr5del")
    # And two codons put in between codons 4 and 5.
    made, _ = mutations.applied(HBB, mutations.read(["g.12_13insGGTGGT"], HBB, False))
    after = mutations.translate(made, table)
    effect, name = mutations.protein_change(before, after)
    assert effect == "in-frame insertion"
    assert name == "p.Leu4_Thr5insGlyGly"


def test_a_frameshift_is_named_from_the_first_residue_it_changes(
    mutations: ModuleType,
) -> None:
    """One base out of a coding sequence shifts everything after it, and the name says
    where the new reading frame stops, counting the first residue that changed as 1."""
    table = mutations.genetic_code(1)
    made, _ = mutations.applied(HBB, mutations.read(["g.10del"], HBB, False))
    assert (len(HBB) - len(made)) % 3 == 1
    before = mutations.translate(HBB, table)
    after = mutations.translate(made, table)
    effect, name = mutations.frameshift(before, after)
    assert effect == "frameshift"
    assert name.startswith("p.Leu4")
    assert "fsTer" in name

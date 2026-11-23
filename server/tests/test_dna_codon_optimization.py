"""Codon optimization in the DNA plugin (ADR 0203): the codon usage tables, the measures
computed from them, what a design avoids, and the run that registers one.

The measures are checked against their definitions and against an independent
implementation: relative synonymous codon usage as Sharp, Tuohy and Mosurski define it
(*Nucleic Acids Res* 14:5125, 1986), relative adaptiveness and the codon adaptation index
as Sharp and Li define them (*Nucleic Acids Res* 15:1281, 1987), and the index itself
against Biopython's ``Bio.SeqUtils.CodonAdaptationIndex``, which implements the same
paper, over a reference set of real *E. coli* K-12 coding sequences.

The sequences here are real and public: human beta-globin (HBB, NM_000518.5), human
insulin (INS, NM_000207.3), *Aequorea victoria* green fluorescent protein (M62653.1), and
seven *E. coli* K-12 MG1655 coding sequences from GCF_000005845.2, each named by its
RefSeq protein accession.

The command is run as a worker runs it: an input.json in, an output.json out, checked
against the run-output schema.
"""

import importlib
import json
import math
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from Bio.SeqUtils import CodonAdaptationIndex

from iki_spec import schemas

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "dna" / "scripts"
SCRIPT = SCRIPTS / "optimize.py"
USAGE = ROOT / "plugins" / "dna" / "data" / "codon_usage.json"

#: Human beta-globin's coding sequence (HBB, NM_000518.5), 147 amino acids and a stop.
HBB = (
    "ATGGTGCATCTGACTCCTGAGGAGAAGTCTGCCGTTACTGCCCTGTGGGGCAAGGTGAACGTGGATGAAGTTGGTGGTGAG"
    "GCCCTGGGCAGGCTGCTGGTGGTCTACCCTTGGACCCAGAGGTTCTTTGAGTCCTTTGGGGATCTGTCCACTCCTGATGCT"
    "GTTATGGGCAACCCTAAGGTGAAGGCTCATGGCAAGAAAGTGCTCGGTGCCTTTAGTGATGGCCTGGCTCACCTGGACAAC"
    "CTCAAGGGCACCTTTGCCACACTGAGTGAGCTGCACTGTGACAAGCTGCACGTGGATCCTGAGAACTTCAGGCTCCTGGGC"
    "AACGTGCTGGTCTGTGTGCTGGCCCATCACTTTGGCAAAGAATTCACCCCACCAGTGCAGGCTGCCTATCAGAAAGTGGTG"
    "GCTGGTGTGGCTAATGCCCTGGCCCACAAGTATCACTAA"
)
#: Human preproinsulin (INS, NM_000207.3): 110 amino acids and a stop, and GC-rich.
INS = (
    "ATGGCCCTGTGGATGCGCCTCCTGCCCCTGCTGGCGCTGCTGGCCCTCTGGGGACCTGACCCAGCCGCAGCCTTTGTGAAC"
    "CAACACCTGTGCGGCTCACACCTGGTGGAAGCTCTCTACCTAGTGTGCGGGGAACGAGGCTTCTTCTACACACCCAAGACC"
    "CGCCGGGAGGCAGAGGACCTGCAGGTGGGGCAGGTGGAGCTGGGCGGGGGCCCTGGTGCAGGCAGCCTGCAGCCCTTGGCC"
    "CTGGAGGGGTCCCTGCAGAAGCGTGGCATTGTGGAACAATGCTGTACCAGCATCTGCTCCCTCTACCAGCTGGAGAACTAC"
    "TGCAACTAG"
)
#: Green fluorescent protein's coding sequence (Aequorea victoria, M62653.1).
GFP = (
    "ATGAGTAAAGGAGAAGAACTTTTCACTGGAGTTGTCCCAATTCTTGTTGAATTAGATGGTGATGTTAATGGGCACAAATTT"
    "TCTGTCAGTGGAGAGGGTGAAGGTGATGCAACATACGGAAAACTTACCCTTAAATTTATTTGCACTACTGGAAAACTACCT"
    "GTTCCATGGCCAACACTTGTCACTACTTTCTCTTATGGTGTTCAATGCTTTTCAAGATACCCAGATCATATGAAACAGCAT"
    "GACTTTTTCAAGAGTGCCATGCCCGAAGGTTATGTACAGGAAAGAACTATATTTTTCAAAGATGACGGGAACTACAAGACA"
    "CGTGCTGAAGTCAAGTTTGAAGGTGATACCCTTGTTAATAGAATCGAGTTAAAAGGTATTGATTTTAAAGAAGATGGAAAC"
    "ATTCTTGGACACAAATTGGAATACAACTATAACTCACACAATGTATACATCATGGCAGACAAACAAAAGAATGGAATCAAA"
    "GTTAACTTCAAAATTAGACACAACATTGAAGATGGAAGCGTTCAACTAGCAGACCATTATCAACAAAATACTCCAATTGGC"
    "GATGGCCCTGTCCTTTTACCAGACAACCATTACCTGTCCACACAATCTGCCCTTTCGAAAGATCCCAACGAAAAGAGAGAC"
    "CACATGGTCCTTCTTGAGTTTGTAACAGCTGCTGGGATTACACATGGCATGGATGAACTATACAAATAA"
)
#: Seven E. coli K-12 MG1655 coding sequences (GCF_000005845.2), by RefSeq protein ID:
#: a reference set small enough to read and real enough to count codons from. rpsT and
#: lacI begin TTG and GTG, as bacterial genes may.
E_COLI_REFERENCE = {
    "NP_414542.1": (  # thrL, the thr operon leader peptide
        "ATGAAACGCATTAGCACCACCATTACCACCACCATCACCATTACCACAGGTAACGGTGCGGGCTGA"
    ),
    "NP_417537.1": (  # rpsU, ribosomal protein S21
        "ATGCCGGTAATTAAAGTACGTGAAAACGAGCCGTTCGACGTAGCTCTGCGTCGCTTCAAGCGTTCCTGCGAAAAAGCAG"
        "GTGTTCTGGCGGAAGTTCGTCGTCGTGAGTTCTATGAAAAACCGACTACCGAACGTAAGCGCGCTAAAGCTTCTGCAGT"
        "GAAACGTCACGCGAAGAAACTGGCTCGCGAAAACGCACGCCGCACTCGTCTGTACTAA"
    ),
    "NP_417758.1": (  # rpmJ, ribosomal protein L36
        "ATGAAAGTTCGTGCTTCCGTCAAGAAATTATGCCGTAACTGCAAAATCGTTAAGCGTGATGGTGTCATCCGTGTGATTT"
        "GCAGTGCCGAGCCGAAGCATAAACAGCGCCAAGGCTGA"
    ),
    "NP_414564.1": (  # rpsT, ribosomal protein S20
        "TTGGCTAATATCAAATCAGCTAAGAAGCGCGCCATTCAGTCTGAAAAGGCTCGTAAGCACAACGCAAGCCGTCGCTCTA"
        "TGATGCGTACTTTCATCAAGAAAGTATACGCAGCTATCGAAGCTGGCGACAAAGCTGCTGCACAGAAAGCATTTAACGA"
        "AATGCAACCGATCGTGGACCGTCAGGCTGCTAAAGGTCTGATCCACAAAAACAAAGCTGCACGTCATAAGGCTAACCTG"
        "ACTGCACAGATCAACAAACTGGCTTAA"
    ),
    "NP_415404.1": (  # infA, translation initiation factor IF-1
        "ATGGCCAAAGAAGACAATATTGAAATGCAAGGTACCGTTCTTGAAACGTTGCCTAATACCATGTTCCGCGTAGAGTTAG"
        "AAAACGGTCACGTGGTTACTGCACACATCTCCGGTAAAATGCGCAAAAACTACATCCGCATCCTGACGGGCGACAAAGT"
        "GACTGTTGAACTGACCCCGTACGACCTGAGCAAAGGCCGCATTGTCTTCCGTAGTCGCTGA"
    ),
    "NP_418066.1": (  # secB, protein-export chaperone
        "ATGTCAGAACAAAACAACACTGAAATGACTTTCCAGATCCAACGTATTTATACCAAGGATATCTCTTTCGAAGCGCCGA"
        "ACGCGCCGCACGTTTTCCAGAAAGATTGGCAACCAGAAGTTAAACTTGATCTGGATACGGCATCTTCCCAACTGGCAGA"
        "TGACGTATACGAAGTGGTACTGCGTGTTACCGTAACGGCCTCTTTGGGCGAAGAAACCGCGTTCCTGTGTGAAGTTCAG"
        "CAGGGCGGTATTTTCTCCATCGCGGGTATCGAAGGCACCCAGATGGCGCATTGCCTGGGAGCATACTGCCCGAACATTC"
        "TGTTCCCGTATGCTCGTGAGTGCATCACCAGCATGGTATCCCGCGGTACATTCCCGCAACTGAACCTTGCGCCGGTTAA"
        "CTTCGATGCGCTGTTCATGAACTATTTGCAGCAGCAGGCTGGCGAAGGTACTGAAGAACATCAGGATGCCTGA"
    ),
    "NP_414879.3": (  # lacI, the lac repressor
        "GTGAAACCAGTAACGTTATACGATGTCGCAGAGTATGCCGGTGTCTCTTATCAGACCGTTTCCCGCGTGGTGAACCAGG"
        "CCAGCCACGTTTCTGCGAAAACGCGGGAAAAAGTGGAAGCGGCGATGGCGGAGCTGAATTACATTCCCAACCGCGTGGC"
        "ACAACAACTGGCGGGCAAACAGTCGTTGCTGATTGGCGTTGCCACCTCCAGTCTGGCCCTGCACGCGCCGTCGCAAATT"
        "GTCGCGGCGATTAAATCTCGCGCCGATCAACTGGGTGCCAGCGTGGTGGTGTCGATGGTAGAACGAAGCGGCGTCGAAG"
        "CCTGTAAAGCGGCGGTGCACAATCTTCTCGCGCAACGCGTCAGTGGGCTGATCATTAACTATCCGCTGGATGACCAGGA"
        "TGCCATTGCTGTGGAAGCTGCCTGCACTAATGTTCCGGCGTTATTTCTTGATGTCTCTGACCAGACACCCATCAACAGT"
        "ATTATTTTCTCCCATGAAGACGGTACGCGACTGGGCGTGGAGCATCTGGTCGCATTGGGTCACCAGCAAATCGCGCTGT"
        "TAGCGGGCCCATTAAGTTCTGTCTCGGCGCGTCTGCGTCTGGCTGGCTGGCATAAATATCTCACTCGCAATCAAATTCA"
        "GCCGATAGCGGAACGGGAAGGCGACTGGAGTGCCATGTCCGGTTTTCAACAAACCATGCAAATGCTGAATGAGGGCATC"
        "GTTCCCACTGCGATGCTGGTTGCCAACGATCAGATGGCGCTGGGCGCAATGCGCGCCATTACCGAGTCCGGGCTGCGCG"
        "TTGGTGCGGATATCTCGGTAGTGGGATACGACGATACCGAAGACAGCTCATGTTATATCCCGCCGTTAACCACCATCAA"
        "ACAGGATTTTCGCCTGCTGGGGCAAACCAGCGTGGACCGCTTGCTGCAACTCTCTCAGGGCCAGGCGGTGAAGGGCAAT"
        "CAGCTGTTGCCCGTCTCACTGGTGAAAAGAAAAACCACCCTGGCGCCCAATACGCAAACCGCCTCTCCCCGCGCGTTGG"
        "CCGATTCATTAATGCAGCTGGCACGACAGGTTTCCCGACTGGAAAGCGGGCAGTGA"
    ),
}


@pytest.fixture(scope="module")
def codons() -> Iterator[ModuleType]:
    """The plugin's codon module, imported as a run imports it (by bare name)."""
    names = ("codons", "optimize")
    kept = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield importlib.import_module("codons")
    finally:
        sys.path.remove(str(SCRIPTS))
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(kept)


@pytest.fixture(scope="module")
def tables(codons: ModuleType) -> dict[str, Any]:
    return dict(codons.read_usage(json.loads(USAGE.read_text())))


def reference_usage(codons: ModuleType) -> Any:
    """A usage table counted from E_COLI_REFERENCE, so an independent implementation can
    be given the very same reference set."""
    counts = dict.fromkeys(codons.CODONS, 0)
    for bases in E_COLI_REFERENCE.values():
        for at in range(0, len(bases), 3):
            counts[bases[at : at + 3]] += 1
    return codons.read_usage(
        {
            "organisms": [
                {
                    "key": "reference",
                    "name": "Escherichia coli",
                    "strain": "K-12 substr. MG1655",
                    "common": "E. coli",
                    "assembly": "GCF_000005845.2",
                    "assembly_name": "ASM584v2",
                    "genetic_code": 11,
                    "coding_sequences": len(E_COLI_REFERENCE),
                    "codons": sum(counts.values()),
                    "counts": counts,
                }
            ]
        }
    )["reference"]


# --- the tables -----------------------------------------------------------------------


def test_every_host_table_is_counted_from_a_real_assembly(tables: dict[str, Any]) -> None:
    """Each table names the assembly its counts came from, holds all 64 codons, and its
    counts add up to the number of codons it says it counted."""
    assert len(tables) == 9
    for usage in tables.values():
        assert usage.assembly.startswith("GCF_")
        assert usage.coding_sequences >= 1000
        assert len(usage.counts) == 64
        assert min(usage.counts.values()) > 0
        assert sum(usage.counts.values()) == usage.codons
        assert usage.genetic_code in (1, 11)


def test_the_tables_show_the_codon_biases_the_literature_describes(
    tables: dict[str, Any], codons: ModuleType
) -> None:
    """Two textbook facts, as a check that the counting is of the right thing: the
    arginine codons AGA and AGG are the two rarest in E. coli, which is why expressing a
    eukaryotic gene there stalls on them, while AGA is the most-used arginine codon in
    S. cerevisiae -- the opposite way round."""
    arginine = ["CGT", "CGC", "CGA", "CGG", "AGA", "AGG"]
    coli = tables["e_coli_k12"].counts
    assert sorted(arginine, key=lambda c: coli[c])[:2] == ["AGG", "AGA"]
    yeast = tables["s_cerevisiae_s288c"].counts
    assert max(arginine, key=lambda c: yeast[c]) == "AGA"
    # And the most-used leucine codon in E. coli is CTG, as every expression vector's
    # optimized insert shows.
    leucine = ["TTA", "TTG", "CTT", "CTC", "CTA", "CTG"]
    assert max(leucine, key=lambda c: coli[c]) == "CTG"


def test_relative_synonymous_codon_usage_is_the_count_over_the_familys_mean(
    tables: dict[str, Any], codons: ModuleType
) -> None:
    """RSCU as Sharp, Tuohy and Mosurski (1986) define it: a codon's count divided by the
    mean count of the codons for the same amino acid, so each family's values average 1."""
    usage = tables["e_coli_k12"]
    table = codons.genetic_code(11)
    rscu = usage.rscu(table)
    for residue, family in usage.families(table).items():
        counts = [usage.counts[codon] for codon in family]
        mean = sum(counts) / len(counts)
        for codon in family:
            assert rscu[codon] == pytest.approx(usage.counts[codon] / mean), (residue, codon)
        assert sum(rscu[codon] for codon in family) == pytest.approx(len(family))


def test_relative_adaptiveness_is_the_count_over_the_familys_largest(
    tables: dict[str, Any], codons: ModuleType
) -> None:
    """w as Sharp and Li (1987) define it, so the most-used codon of every family is 1."""
    usage = tables["h_sapiens"]
    table = codons.genetic_code(1)
    weights = usage.weights(table)
    for family in usage.families(table).values():
        most = max(usage.counts[codon] for codon in family)
        assert max(weights[codon] for codon in family) == 1.0
        for codon in family:
            assert weights[codon] == pytest.approx(usage.counts[codon] / most)


def test_a_codon_the_table_never_shows_counts_as_half_an_occurrence(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """Sharp and Li give a codon absent from the reference set a value of 0.5 counts,
    rather than 0, so that a gene using it still has a codon adaptation index at all."""
    usage = reference_usage(codons)
    table = codons.genetic_code(11)
    weights = usage.weights(table)
    absent = [codon for codon in codons.CODONS if not usage.counts[codon]]
    assert absent, "the small reference set should not use every codon"
    for codon in absent:
        residue = codons.residue_of(codon, table)
        family = [c for c in codons.CODONS if codons.residue_of(c, table) == residue]
        most = max(usage.counts[c] or codons.ABSENT for c in family)
        assert weights[codon] == pytest.approx(codons.ABSENT / most)
        assert weights[codon] > 0
    # So every gene has an index, and no log of zero is ever taken.
    assert codons.cai(GFP[:-3], weights, table) is not None


# --- the codon adaptation index -------------------------------------------------------


@pytest.mark.parametrize("gene", ["HBB", "INS", "GFP"])
def test_the_codon_adaptation_index_is_biopythons(codons: ModuleType, gene: str) -> None:
    """The index, over the same reference set, is the one Biopython's
    CodonAdaptationIndex computes: an independent implementation of Sharp and Li's paper.

    Both leave out the codons whose amino acid has only one codon (ATG and TGG in the
    standard code) -- their w is 1 whatever the gene is -- so the comparison is of the
    geometric mean itself. The stop codon is left off the sequence, since Biopython's
    index counts stops as a synonymous family of their own and takes them into the mean,
    which Iki doesn't.
    """
    bases = {"HBB": HBB, "INS": INS, "GFP": GFP}[gene][:-3]
    usage = reference_usage(codons)
    table = codons.genetic_code(1)
    theirs = CodonAdaptationIndex(list(E_COLI_REFERENCE.values()))  # type: ignore[no-untyped-call]
    mine = usage.weights(table)
    # The weights themselves agree, family by family.
    for codon in codons.CODONS:
        if codons.residue_of(codon, table) == "*":
            continue
        assert mine[codon] == pytest.approx(theirs[codon]), codon
    independent = theirs.calculate(bases)  # type: ignore[no-untyped-call]
    assert codons.cai(bases, mine, table) == pytest.approx(independent)


def test_a_gene_of_nothing_but_the_hosts_best_codons_has_an_index_of_one(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """CAI is the geometric mean of w, and w is 1 for each family's most-used codon, so a
    gene written entirely in them is exactly 1."""
    usage = tables["e_coli_k12"]
    table = codons.genetic_code(11)
    weights = usage.weights(table)
    best = {
        residue: max(family, key=lambda c: (weights[c], c))
        for residue, family in usage.families(table).items()
    }
    bases = "".join(best[residue] for residue in "MAKLRSTVFGHWEDNQIPCY")
    assert codons.cai(bases, weights, table) == pytest.approx(1.0)


def test_the_index_is_the_geometric_mean_of_the_weights_it_counts(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """Recomputed from the definition: exp of the mean log of w over the codons counted."""
    usage = tables["c_griseus"]
    table = codons.genetic_code(1)
    weights = usage.weights(table)
    single = {
        residue
        for residue, family in usage.families(table).items()
        if len(family) == 1 and residue != "*"
    }
    logs = [
        math.log(weights[HBB[at : at + 3]])
        for at in range(0, len(HBB), 3)
        if codons.residue_of(HBB[at : at + 3], table) not in {"*", *single}
    ]
    assert codons.cai(HBB, weights, table) == pytest.approx(math.exp(sum(logs) / len(logs)))


# --- what a design avoids -------------------------------------------------------------


def test_a_motif_is_found_on_both_strands_and_where_it_overlaps_itself(
    codons: ModuleType,
) -> None:
    """A restriction site is a site whichever strand it reads on, and two sites that
    share bases are two sites."""
    motifs = [("GGTCTC", "the BsaI site", "restriction_site")]
    # BsaI's site on the given strand at 3, and its reverse complement (GAGACC) at 11.
    found = codons.motif_hits("TTGGTCTCTTGAGACCTT", motifs)
    assert [(v.start, v.end, "other" in v.detail) for v in found] == [
        (3, 8, False),
        (11, 16, True),
    ]
    # AAAA inside AAAAAA occurs three times, not once.
    overlapping = codons.motif_hits("TTAAAAAATT", [("AAAA", "four A's", "motif")])
    assert [(v.start, v.end) for v in overlapping] == [(3, 6), (4, 7), (5, 8)]


def test_a_palindromic_site_is_reported_once(codons: ModuleType) -> None:
    """EcoRI's GAATTC is its own reverse complement, so the site is one site, not two."""
    found = codons.motif_hits("TTGAATTCTT", [("GAATTC", "the EcoRI site", "restriction_site")])
    assert [(v.start, v.end) for v in found] == [(3, 8)]


def test_a_restriction_site_comes_from_biopythons_rebase_set(codons: ModuleType) -> None:
    assert codons.enzyme_site("EcoRI") == "GAATTC"
    assert codons.enzyme_site("BsaI") == "GGTCTC"
    assert codons.enzyme_site("BsmBI") == "CGTCTC"
    with pytest.raises(codons.Refused, match="No restriction enzyme is called NotAnEnzyme"):
        codons.enzyme_site("NotAnEnzyme")


def test_runs_repeats_and_hairpins_are_found_where_they_are(codons: ModuleType) -> None:
    runs = codons.homopolymers("CCAAAAAAAGG", 6)
    assert [(v.start, v.end, v.detail) for v in runs] == [(3, 9, "7 A's in a row, more than 6")]
    assert codons.homopolymers("CCAAAAAAGG", 6) == []
    # The second occurrence of a repeated stretch is the one reported, so repairing it
    # leaves the first where it is.
    word = "ACGTTGCAACGTAGC"
    repeated = codons.repeats(word + "TT" + word, len(word))
    assert [(v.start, v.end) for v in repeated] == [(18, 32)]
    # An inverted repeat of eight bases closing a loop of four.
    stem = "ACGTTGCA"
    arm = codons.reverse_complement(stem)
    pins = codons.hairpins(stem + "TTTT" + arm, 8, 30)
    assert [(v.start, v.end) for v in pins] == [(1, 20)]
    assert codons.hairpins(stem + "TTTT" + arm, 9, 30) == []


def test_the_gc_share_is_checked_overall_and_in_a_window(codons: ModuleType) -> None:
    """A sequence inside the overall band can still have a stretch far outside it, which
    is what the window finds; out-of-band windows next to one another are one stretch."""
    constraints = codons.Constraints(
        gc_min=30, gc_max=70, gc_window=10, gc_window_min=25, gc_window_max=75
    )
    even = "ACGT" * 10
    assert codons.gc_breaks(even, constraints) == []
    lopsided = "GCGCGCGCGCGC" + "ATATATATATAT" + "ACGTACGTACGTACGT"
    found = codons.gc_breaks(lopsided, constraints)
    assert [v.kind for v in found] == ["gc_window", "gc_window"]
    assert found[0].needs == -1  # too much GC at the start
    assert found[1].needs == 1  # too little in the middle
    assert all(v.severity > 0 for v in found)
    # And the overall share is its own constraint.
    assert [v.kind for v in codons.gc_breaks("AT" * 30, constraints)] == ["gc_content", "gc_window"]


def test_a_bacterial_host_avoids_internal_shine_dalgarno_and_a_eukaryotic_one_splice_sites(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """The motif set a design gets follows from the host's own translation table: a host
    read by NCBI's table 11 is a bacterium."""
    constraints = codons.Constraints()
    bacterial = [motif for motif, _, _ in codons.motifs_of(constraints, "bacterial")]
    assert bacterial == ["AGGAGG"]
    eukaryotic = [motif for motif, _, _ in codons.motifs_of(constraints, "eukaryotic")]
    assert eukaryotic == ["MAGGTRAGT", "AATAAA"]
    assert tables["e_coli_k12"].genetic_code == codons.BACTERIAL_CODE
    assert tables["c_griseus"].genetic_code == 1
    # Turned off, the host's own motifs aren't looked for, and the design's still are.
    allowed = codons.Constraints(avoid_host_motifs=False, avoid_motifs=("GGTCTC",))
    assert [m for m, _, _ in codons.motifs_of(allowed, "bacterial")] == ["GGTCTC"]


# --- optimizing -----------------------------------------------------------------------


@pytest.mark.parametrize("gene", ["HBB", "INS", "GFP"])
@pytest.mark.parametrize("host", ["e_coli_k12", "k_phaffii_gs115", "c_griseus", "s_frugiperda"])
@pytest.mark.parametrize("strategy", ["highest_usage", "usage_matched"])
def test_the_protein_is_never_changed(
    codons: ModuleType, tables: dict[str, Any], gene: str, host: str, strategy: str
) -> None:
    """Whatever the host, the strategy or the gene, the optimized bases code for the
    protein the template coded for, residue for residue -- and the length is the same, so
    nothing outside the region moves."""
    bases = {"HBB": HBB, "INS": INS, "GFP": GFP}[gene]
    table = codons.genetic_code(1)
    found = codons.optimize(bases, tables[host], table_number=1, strategy=strategy)
    assert codons.translate(found.bases, table) == codons.translate(bases, table)
    assert len(found.bases) == len(bases)
    # And its first codon and its stop codon are the template's, not the host's choice.
    assert found.bases[:3] == bases[:3]
    assert found.bases[-3:] == bases[-3:]


def test_optimizing_for_a_host_raises_the_index_against_that_host(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """The point of the exercise: GFP, a jellyfish gene, is poorly adapted to E. coli and
    well adapted to it afterwards."""
    found = codons.optimize(GFP, tables["e_coli_k12"], table_number=1, strategy="highest_usage")
    assert found.source_cai is not None and found.optimized_cai is not None
    assert found.source_cai < 0.6
    assert found.optimized_cai > 0.99
    assert found.codons_changed > 100
    assert found.unmet == []


def test_a_rare_codon_is_never_written(codons: ModuleType, tables: dict[str, Any]) -> None:
    """No codon below the threshold is used: E. coli's AGA and AGG, which its rare tRNAs
    decode, are gone from a gene full of them."""
    usage = tables["e_coli_k12"]
    table = codons.genetic_code(11)
    weights = usage.weights(table)
    assert weights["AGA"] < 0.1 and weights["AGG"] < 0.1
    found = codons.optimize(
        "ATG" + "AGA" * 30 + "TAA", usage, table_number=11, strategy="highest_usage"
    )
    written = {found.bases[at : at + 3] for at in range(0, len(found.bases), 3)}
    assert not written & {"AGA", "AGG"}
    # With the threshold off, the rare codons are allowed again, and usage-matched
    # design then uses them at the share the host does.
    loose = codons.Constraints(rare_codon_threshold=0.0, longest_repeat=0)
    relaxed = codons.optimize(
        "ATG" + "AGA" * 30 + "TAA",
        usage,
        table_number=11,
        strategy="usage_matched",
        constraints=loose,
    )
    assert "AGA" in {relaxed.bases[at : at + 3] for at in range(0, len(relaxed.bases), 3)}


def test_a_named_restriction_site_is_designed_out_of_the_gene(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """Six enzymes a cloning plan might use, none of whose sites may be in the gene: the
    sites are gone, and the protein is the same."""
    enzymes = ("EcoRI", "BamHI", "HindIII", "XhoI", "NdeI", "BsaI")
    constraints = codons.Constraints(avoid_enzymes=enzymes)
    for host in ("e_coli_k12", "c_griseus"):
        found = codons.optimize(
            INS, tables[host], table_number=1, strategy="highest_usage", constraints=constraints
        )
        sites = [(codons.enzyme_site(name), name, "restriction_site") for name in enzymes]
        assert codons.motif_hits(found.bases, sites) == []
        assert [v for v in found.unmet if v.kind == "restriction_site"] == []


def test_a_motif_the_protein_forces_is_reported_rather_than_hidden(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """Methionine has one codon, so ATGATG in a run of methionines cannot be designed
    away. The design is returned with the motif placed, and the protein untouched."""
    constraints = codons.Constraints(avoid_motifs=("ATGATG",), gc_min=0, gc_window=0)
    found = codons.optimize(
        "ATGATGATGATGATGTAA",
        tables["e_coli_k12"],
        table_number=1,
        strategy="highest_usage",
        constraints=constraints,
    )
    assert found.bases == "ATGATGATGATGATGTAA"
    assert [v.kind for v in found.unmet] == ["motif"] * 4
    assert found.codons_changed == 0


def test_the_same_design_always_gives_the_same_bases(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """The action is declared deterministic, so nothing here may depend on a random
    number or on the order a dictionary happens to have."""
    for strategy in ("highest_usage", "usage_matched"):
        again = [
            codons.optimize(HBB, tables["h_sapiens"], table_number=1, strategy=strategy).bases
            for _ in range(3)
        ]
        assert len(set(again)) == 1


def test_a_harmonized_design_matches_the_source_organisms_codon_shares(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    """Harmonization (Angov et al., PLoS ONE 3:e2189, 2008) keeps each codon's share
    within its family near the share the template's codon has where the gene came from.
    Every codon it writes is the allowed host codon whose share is nearest the source's.
    """
    host, source = tables["e_coli_k12"], tables["h_sapiens"]
    table = codons.genetic_code(1)
    host_shares, source_shares = host.fractions(table), source.fractions(table)
    weights = host.weights(table)
    protein = codons.translate(HBB, table)
    allowed = {
        i: codons.allowed_codons(residue, table, weights, 0.1) for i, residue in enumerate(protein)
    }
    source_codons = [HBB[at : at + 3] for at in range(0, len(HBB), 3)]
    chosen = codons.initial_codons(
        protein,
        table,
        host_shares,
        allowed,
        "harmonized",
        source_shares,
        source_codons,
    )
    for i, (was, now) in enumerate(zip(source_codons, chosen, strict=True)):
        wanted = source_shares[was]
        nearest = min(abs(host_shares[codon] - wanted) for codon in allowed[i])
        assert abs(host_shares[now] - wanted) == pytest.approx(nearest), (i, was, now)
    # Harmonizing is not the same as taking the host's favourite codon: it keeps the
    # template's pattern, so its index is lower than a highest-usage design's.
    harmonized = codons.optimize(
        HBB, host, table_number=1, strategy="harmonized", source_usage=source
    )
    best = codons.optimize(HBB, host, table_number=1, strategy="highest_usage")
    assert harmonized.optimized_cai is not None and best.optimized_cai is not None
    assert harmonized.optimized_cai < best.optimized_cai
    assert codons.translate(harmonized.bases, table) == protein


def test_a_region_that_cannot_be_read_as_a_gene_is_refused(
    codons: ModuleType, tables: dict[str, Any]
) -> None:
    usage = tables["e_coli_k12"]
    with pytest.raises(codons.Refused, match="isn't a whole number of codons"):
        codons.optimize("ATGAAAGG", usage)
    with pytest.raises(codons.Refused, match="has a stop codon at codon 3 of 6"):
        codons.optimize("ATGAAATAACGCGGCTAA", usage)
    with pytest.raises(codons.Refused, match="isn't a codon of A, C, G and T"):
        codons.optimize("ATGNNNAAATAA", usage)
    with pytest.raises(codons.Refused, match="at least two codons"):
        codons.optimize("ATG", usage)
    with pytest.raises(codons.Refused, match="read as sense codons in some places"):
        codons.optimize("ATGAAATAA", usage, table_number=27)
    with pytest.raises(codons.Refused, match="no 27001 strategy"):
        codons.optimize("ATGAAATAA", usage, strategy="27001")


# --- the run --------------------------------------------------------------------------


def command(tmp_path: Path, given: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Runs the plugin's command as a worker would, and gives its output and its log."""
    (tmp_path / "input.json").write_text(json.dumps(given))
    # Without -I, so the script imports its siblings by bare name as a run does.
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT)],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    log = result.stdout + result.stderr
    if result.returncode != 0:
        return {}, log
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output)), log
    return output, log


def design(values: dict[str, Any], template: dict[str, Any], **params: Any) -> dict[str, Any]:
    return {
        "params": {
            "rare_codon_threshold": 0.1,
            "gc_min": 30.0,
            "gc_max": 70.0,
            "gc_window": 50,
            "gc_window_min": 25.0,
            "gc_window_max": 75.0,
            "longest_homopolymer": 6,
            "longest_repeat": 15,
            "longest_hairpin_stem": 10,
            "hairpin_loop": 30,
            "avoid_host_motifs": True,
            **params,
        },
        "inputs": {
            "optimization": [
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "hrid": "OPT-000001",
                    "values": values,
                    "included": {
                        "template": [
                            {
                                "id": "00000000-0000-4000-8000-000000000002",
                                "hrid": "DNA-000002",
                                "values": template,
                            }
                        ]
                    },
                }
            ]
        },
    }


def test_the_run_registers_the_whole_template_with_the_region_rewritten(
    tmp_path: Path,
) -> None:
    """The product is the template with the coding region replaced, so a gene inside a
    vector comes back as the vector with that gene optimized: the flanks are the
    template's, the topology is the template's, and the features still point where they
    did, since a synonymous rewrite changes no length."""
    gene = "ATGAAATTTGAATTCGGCCATAAAGGCTAA"
    features = [{"type": "CDS", "name": "gene", "start": 11, "end": 40, "strand": "forward"}]
    output, log = command(
        tmp_path,
        design(
            {
                "host": "e_coli_k12",
                "coding_start": 11,
                "coding_end": 40,
                "avoid_enzymes": ["EcoRI"],
            },
            {
                "bases": "GGAATTCCGG" + gene + "CCGAATTCGG",
                "topology": "circular",
                "strandedness": "double",
                "name": "pGene",
                "features": features,
            },
        ),
    )
    assert output, log
    created = output["creates"][0]
    assert created["type"] == "iki/dna.DNASequence"
    assert created["derived_from"] == ["00000000-0000-4000-8000-000000000001"]
    product = created["values"]["bases"]
    assert product.startswith("GGAATTCCGG") and product.endswith("CCGAATTCGG")
    assert len(product) == 50
    assert product[10:40] == "ATGAAATTTGAATTTGGCCATAAAGGCTAA"
    assert created["values"]["name"] == "pGene, E. coli codons"
    assert created["values"]["topology"] == "circular"
    assert created["values"]["features"] == features
    values = output["updates"]["optimization"][0]["values"]
    assert values["codons"] == 10
    assert values["codons_changed"] == 1
    assert values["constraints_met"] is True
    assert "Escherichia coli K-12 substr. MG1655 (GCF_000005845.2" in values["cai_reference"]
    assert "highest usage codons" in values["optimized_with"]
    assert "no codon below w = 0.1" in values["optimized_with"]
    assert "without the EcoRI sites" in values["optimized_with"]


def test_the_run_reads_a_gene_on_the_other_strand_and_writes_it_back_there(
    tmp_path: Path,
) -> None:
    gene = "ATGAAATTTGAATTCGGCCATAAAGGCTAA"
    other = gene.translate(str.maketrans("ACGT", "TGCA"))[::-1]
    output, log = command(
        tmp_path,
        design(
            {
                "host": "e_coli_k12",
                "coding_start": 5,
                "coding_end": 34,
                "coding_strand": "reverse",
            },
            {"bases": "ACGT" + other + "TTTT", "topology": "linear", "strandedness": "double"},
        ),
    )
    assert output, log
    product = output["creates"][0]["values"]["bases"]
    assert product.startswith("ACGT") and product.endswith("TTTT")
    region = product[4:34].translate(str.maketrans("ACGT", "TGCA"))[::-1]
    assert region == "ATGAAATTTGAATTTGGCCATAAAGGCTAA"
    assert "on the other strand" in output["creates"][0]["values"]["description"]


def test_a_design_that_cannot_meet_a_constraint_warns_and_still_registers(
    tmp_path: Path,
) -> None:
    output, log = command(
        tmp_path,
        design(
            {"host": "e_coli_k12", "avoid_motifs": ["ATGATG"]},
            {"bases": "ATGATGATGATGATGTAA", "topology": "linear", "strandedness": "double"},
            gc_min=0.0,
            gc_window=0,
        ),
    )
    assert output, log
    assert output["creates"][0]["values"]["bases"] == "ATGATGATGATGATGTAA"
    values = output["updates"]["optimization"][0]["values"]
    assert values["constraints_met"] is False
    assert len(values["unmet_constraints"]) == 4
    assert output["messages"][0]["level"] == "warning"
    assert "no synonymous codon removes them" in output["messages"][0]["text"]
    assert output["metrics"]["unmet"] == 4
    assert "still broken: motif 1..6" in log


def test_a_region_outside_the_template_is_refused_by_the_run(tmp_path: Path) -> None:
    output, log = command(
        tmp_path,
        design(
            {"host": "e_coli_k12", "coding_start": 1, "coding_end": 99},
            {"bases": "ATGAAATAA", "topology": "linear", "strandedness": "double"},
        ),
    )
    assert not output
    assert "::error" in log
    assert "past the template's 9 bases" in log


def test_a_host_with_no_table_is_refused_by_the_run(tmp_path: Path) -> None:
    output, log = command(
        tmp_path,
        design(
            {"host": "nonesuch"},
            {"bases": "ATGAAATAA", "topology": "linear", "strandedness": "double"},
        ),
    )
    assert not output
    assert "which has no codon usage table here" in log

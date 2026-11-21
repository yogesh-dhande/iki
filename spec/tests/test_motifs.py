"""Motifs (ADR 0175): bases or residues in IUPAC codes, read as the sets of residues each
code stands for, and the regular expressions a search compiles them to.

Biopython's tables of the IUPAC-IUB codes (Cornish-Bowden, Nucleic Acids Res. 1985,
13:3021) and its reverse complement are the reference: an implementation of the codes
that isn't Iki's own."""

import re
from pathlib import Path

import pytest
from Bio import SeqUtils
from Bio.Data import IUPACData
from Bio.Seq import reverse_complement, reverse_complement_rna

from iki_spec import motifs

SEQUENCES = Path(__file__).parent / "sequences"


def puc19() -> str:
    """pUC19, GenBank L09137.2: 2,686 bp, with its one EcoRI site at 396 (NEB's map)."""
    lines = (SEQUENCES / "L09137.2.fasta").read_text().splitlines()
    return "".join(line.strip() for line in lines if not line.startswith(">"))


@pytest.mark.parametrize(
    ("format", "table"),
    [
        ("dna_sequence", IUPACData.ambiguous_dna_values),
        ("rna_sequence", IUPACData.ambiguous_rna_values),
    ],
)
def test_each_nucleotide_code_stands_for_the_bases_iupac_gives_it(
    format: str, table: dict[str, str]
) -> None:
    # Biopython also reads X as any base, which IUPAC-IUB doesn't: N is.
    expected = {code: frozenset(bases) for code, bases in table.items() if code != "X"}
    assert motifs.MEANINGS[format] == expected


def test_each_amino_acid_code_stands_for_the_residues_iupac_gives_it() -> None:
    table = IUPACData.extended_protein_values
    meanings = motifs.MEANINGS["protein_sequence"]
    for code in "BZJ":
        assert meanings[code] == frozenset(table[code])
    # X is any residue: the 20 standard ones, and selenocysteine and pyrrolysine too.
    assert meanings["X"] == frozenset(table["X"]) | {"U", "O"}
    assert set(meanings) == set(table)


@pytest.mark.parametrize(
    ("format", "reference"),
    [("dna_sequence", reverse_complement), ("rna_sequence", reverse_complement_rna)],
)
def test_a_motifs_reverse_complement_is_biopythons(format: str, reference: object) -> None:
    letters = "".join(sorted(motifs.MEANINGS[format]))
    assert motifs.reverse_complement(format, letters) == reference(letters)  # type: ignore[operator]
    site = "GAATTY" if format == "dna_sequence" else "GAAUUY"
    assert motifs.reverse_complement(format, site) == reference(site)  # type: ignore[operator]


def test_a_protein_has_no_reverse_complement() -> None:
    with pytest.raises(ValueError, match="second strand"):
        motifs.reverse_complement("protein_sequence", "MKV")


def test_a_motif_is_read_in_either_case_with_spaces_and_line_breaks_left_out() -> None:
    assert motifs.read("dna_sequence", " gaa ttc\n") == "GAATTC"
    assert motifs.read("rna_sequence", "GAAUUY") == "GAAUUY"
    # An N-glycosylation sequon: N, any residue, S.
    assert motifs.read("protein_sequence", "nxs") == "NXS"


@pytest.mark.parametrize(
    ("format", "text", "message"),
    [
        ("dna_sequence", "", "some bases"),
        ("dna_sequence", "  ", "some bases"),
        ("dna_sequence", "GAAUUC", "U isn't a DNA base: DNA has T"),
        ("rna_sequence", "GAATTC", "T isn't an RNA base: RNA has U"),
        ("dna_sequence", "GAA-TTC", "`-` isn't an IUPAC code"),
        ("dna_sequence", "GAATT[CT]", "`[` isn't an IUPAC code"),
        ("protein_sequence", "MK*", "`*` isn't an IUPAC code"),
        ("protein_sequence", "", "some residues"),
    ],
)
def test_a_motif_is_only_its_alphabets_codes(format: str, text: str, message: str) -> None:
    with pytest.raises(motifs.MotifError, match=re.escape(message)):
        motifs.read(format, text)


def test_a_code_matches_a_residue_it_certainly_is() -> None:
    """Y (C or T) matches C, T and Y in a record; N, which might be anything, matches only
    N: a record's unknown base isn't certainly the base asked for."""
    pattern = motifs.pattern("dna_sequence", "GAATTY")
    assert pattern == "GAATT[CTY]"
    assert motifs.pattern("dna_sequence", "N") == "[ABCDGHKMNRSTVWY]"
    assert motifs.pattern("dna_sequence", "W") == "[ATW]"
    assert motifs.pattern("protein_sequence", "NBS") == "N[BDN]S"
    for given, found in [("GAATTC", True), ("GAATTT", True), ("GAATTY", True)]:
        assert bool(re.search(pattern, given)) is found
    for given in ("GAATTG", "GAATTN", "GAATTB"):
        assert re.search(pattern, given) is None


def test_a_motif_without_ambiguity_is_plain_text() -> None:
    assert motifs.plain("dna_sequence", "GAATTC")
    assert not motifs.plain("dna_sequence", "GAATTY")
    # Every code but N and X stands for itself alone among a protein's.
    assert motifs.plain("protein_sequence", "MKV")
    assert not motifs.plain("protein_sequence", "NXS")


@pytest.mark.parametrize("motif", ["GAATTC", "GAATTY", "AAGCTT", "GGATCC", "RGATCY", "GTMKAC"])
def test_on_puc19_a_motif_finds_what_biopythons_search_finds_on_both_strands(motif: str) -> None:
    """On real bases (no ambiguity in the record), the compiled pattern finds exactly the
    sites Biopython's ``nt_search`` finds, on the strand given and on its reverse
    complement: EcoRI's at 396, HindIII's at 447, BamHI's at 417 (NEB's pUC19 map)."""
    bases = puc19()
    assert len(bases) == 2686
    for strand in (motif, motifs.reverse_complement("dna_sequence", motif)):
        pattern = motifs.pattern("dna_sequence", strand)
        ours = [m.start() for m in re.finditer(f"(?={pattern})", bases)]
        assert ours == biopythons(bases, strand)
    assert biopythons(bases, "GAATTC") == [395]
    assert biopythons(bases, "AAGCTT") == [446]
    assert biopythons(bases, "GGATCC") == [416]


def biopythons(bases: str, motif: str) -> list[int]:
    """Where Biopython's ``nt_search`` finds the motif, counting from 0."""
    found: list[int] = SeqUtils.nt_search(bases, motif)[1:]  # type: ignore[no-untyped-call]
    return found

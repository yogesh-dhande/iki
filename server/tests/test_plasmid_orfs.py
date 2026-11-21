"""The open reading frames the plasmid plugin finds, checked against the INSDC records in
``data/plasmids/``: every ORF it reports on pUC19, pBR322 and pGKL1 is matched with the
coding sequences those records' own feature tables give, the proteins are translated as
Biopython translates them, and the difference a topology makes is the one ADR 0168 asks
for -- a circle's ORF reads across the origin and a linear molecule's never does.

The command is run as a worker runs it: an input.json in, an output.json out, checked
against the run-output schema.
"""

import importlib
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from Bio.Seq import Seq
from test_plasmid_annotation import RECORDS, read

from iki_spec import schemas

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "plasmid" / "scripts"
SCRIPT = SCRIPTS / "plasmids.py"


@pytest.fixture(scope="module")
def orfs() -> Iterator[ModuleType]:
    """The plugin's ORF module, imported as a run imports it (by bare name)."""
    names = ("orfs", "priming", "cloning", "library")
    kept = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield importlib.import_module("orfs")
    finally:
        sys.path.remove(str(SCRIPTS))
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(kept)


def command(tmp_path: Path, which: str, run: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Runs the plugin's command as a worker would, and gives its output and its log."""
    (tmp_path / "input.json").write_text(json.dumps(run))
    # Without -I, so the script imports its siblings by bare name as a run does.
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT), which],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        return {}, result.stdout + result.stderr
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output, result.stdout


def plasmid(bases: str, *, topology: str = "circular") -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "hrid": "PLS-000001",
        "values": {"name": "a plasmid", "length": len(bases), "topology": topology},
        "included": {
            "sequence": [
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "hrid": "DNA-000001",
                    "values": {"bases": bases, "topology": topology, "strandedness": "double"},
                }
            ]
        },
    }


def coding(accession: str) -> dict[tuple[int, int, str], str]:
    """Each CDS of a record's own feature table: its first and last base and its strand,
    with the gene or product it names. INSDC writes a reverse-strand one as
    ``complement(3293..4153)``."""
    _, _, features = read(accession)
    found: dict[tuple[int, int, str], str] = {}
    for key, location, qualifiers in features:
        if key != "CDS":
            continue
        where = re.fullmatch(r"(complement\()?(\d+)\.\.(\d+)\)?", location)
        if where is None:  # a join or a partial location: not one stretch of this molecule
            continue
        strand = "reverse" if where.group(1) else "forward"
        named = re.search(r'/(?:gene|product)="([^"]+)"', qualifiers)
        found[(int(where.group(2)), int(where.group(3)), strand)] = named.group(1) if named else key
    return found


# --- the ORFs of real vectors --------------------------------------------------------


def test_puc19s_beta_lactamase_and_lacz_alpha_are_its_two_long_orfs(orfs: ModuleType) -> None:
    """pUC19 (L09137.2) has no CDS in its own feature table -- the record is from 1986 and
    annotates its parts in its COMMENT -- so the two ORFs that matter are checked against
    what that comment and the publication give: beta-lactamase (Ap-R) over 1629-2417 by
    the comment's own numbering of the gene within it, and the lacZ-alpha fragment on the
    complementary strand, which the record's DEFINITION line says is where the
    beta-galactosidase mRNA lies.

    The plugin's own annotation finds AmpR at 1626..2486 (test_plasmid_annotation), the
    whole gene including its signal peptide, and that is the reading frame found here: 286
    codons, which is TEM-1 beta-lactamase's length, starting MSIQHFRVALIPFFAAFCLPVFA --
    its signal peptide -- and the lacZ-alpha ORF is 107 codons from pUC19's own ATG.
    """
    bases, length, _ = read("L09137.2")
    found = orfs.find(bases, True, table_number=11, least=75)
    long = {(orf.start, orf.end, orf.strand): orf for orf in found if orf.codons >= 100}
    bla = long[(1626, 2486, "reverse")]
    assert bla.codons == 286
    assert bla.frame == -3
    assert bla.start_codon == "ATG"
    assert bla.translation.startswith("MSIQHFRVALIPFFAAFCLPVFA")
    assert bla.bases == 861  # 286 codons and its stop
    lacz = long[(146, 469, "reverse")]
    assert lacz.codons == 107
    assert lacz.translation.startswith("MTMITPSLHACRST")
    # Each is translated as Biopython translates that stretch of the record.
    for orf in (bla, lacz):
        stretch = Seq(bases[orf.start - 1 : orf.end]).reverse_complement()
        assert str(stretch.translate(table=11))[:-1] == orf.translation
        assert str(stretch.translate(table=11)).endswith("*")
    assert length == 2686


@pytest.mark.parametrize(
    ("accession", "gene", "where"),
    [
        # pBR322's own feature table: tetracycline resistance and beta-lactamase.
        ("J01749.1", "tet", (86, 1276, "forward")),
        ("J01749.1", "bla", (3293, 4153, "reverse")),
    ],
)
def test_an_annotated_cds_of_a_record_comes_out_as_an_orf(
    orfs: ModuleType, accession: str, gene: str, where: tuple[int, int, str]
) -> None:
    """A CDS that an INSDC record's own feature table gives is an ORF of the molecule, at
    those very bases: pBR322 (J01749.1) annotates both of its resistance genes, and both
    come out with the record's coordinates and the protein the record's /translation
    qualifier gives."""
    bases, _, _ = read(accession)
    assert coding(accession)[where] in (gene, "tetracycline resistance protein", "beta-lactamase")
    found = {(orf.start, orf.end, orf.strand): orf for orf in orfs.find(bases, True, least=75)}
    orf = found[where]
    stretch = Seq(bases[where[0] - 1 : where[1]])
    if where[2] == "reverse":
        stretch = stretch.reverse_complement()
    assert orf.translation == str(stretch.translate(table=11))[:-1]
    assert orf.codons == (where[1] - where[0] + 1) // 3 - 1


def test_every_annotated_cds_of_a_linear_plasmid_is_found(orfs: ModuleType) -> None:
    """pGKL1 (X01095.1), the 8,874 bp linear killer plasmid of Kluyveromyces lactis, has
    four CDSs in its feature table, one of them on the reverse strand. All four are ORFs
    of the molecule read as the line it is, at the record's own coordinates."""
    bases, length, _ = read("X01095.1")
    assert length == 8874
    annotated = set(coding("X01095.1"))
    assert len(annotated) == 4
    found = {(orf.start, orf.end, orf.strand) for orf in orfs.find(bases, False, least=200)}
    assert annotated <= found


# --- a circle's origin, and a line's ends (ADR 0168) ---------------------------------


def test_an_orf_across_the_origin_is_one_orf_on_a_circle_and_none_on_a_line(
    orfs: ModuleType,
) -> None:
    """The same bases, read as a circle and as a linear molecule, give different ORFs, and
    that is what topology means: pUC19 written from base 2001 puts its beta-lactamase gene
    across the join. As a circle it is one ORF of 286 codons, the same protein as before,
    reported from its highest base round to its lowest; as a line, with two ends, it is no
    ORF at all, since nothing says where that protein would end."""
    bases, _, _ = read("L09137.2")
    rotated = bases[2000:] + bases[:2000]
    circle = orfs.find(rotated, True, table_number=11, least=200)
    assert [(orf.start, orf.end, orf.strand, orf.codons) for orf in circle] == [
        (2312, 486, "reverse", 286)
    ]
    straight = orfs.find(bases, True, table_number=11, least=200)
    assert circle[0].translation == straight[0].translation
    assert orfs.find(rotated, False, table_number=11, least=200) == []


def test_nothing_on_a_linear_molecule_reads_across_its_ends(orfs: ModuleType) -> None:
    """Wild-type lambda (J02459.1), a 48,502 bp linear genome: no ORF of it is reported
    from a higher base to a lower one, however long the molecule, because that would be an
    ORF across its two ends."""
    text = (RECORDS / "J02459.1.fasta").read_text(encoding="utf-8")
    bases = "".join(line.strip() for line in text.splitlines() if not line.startswith(">")).upper()
    found = orfs.find(bases, False, table_number=11, least=100)
    assert found
    assert all(orf.end > orf.start for orf in found)


# --- the genetic code and the start codons -------------------------------------------


def test_a_tables_alternative_starts_find_a_frame_that_doesnt_begin_at_atg(
    orfs: ModuleType,
) -> None:
    """Table 11 starts a gene at TTG, CTG, ATT, ATC, ATA and GTG as well as ATG, and
    bacterial genes do: pBR322's Rop protein, which its own feature table gives at
    1915..2106, begins at a GTG. With ATG alone that frame is reported only from an ATG
    inside the gene, well after its real start; with the table's own start codons it is
    reported from the first of them after the stop before it, the GTG at 1747, and the
    protein the record annotates is the end of what that frame codes for. A start codon is
    read as methionine either way, as an initiator tRNA reads it.
    """
    bases, _, _ = read("J01749.1")
    assert coding("J01749.1")[(1915, 2106, "forward")] == "ROP protein"
    assert bases[1914:1917] == "GTG"
    atg_only = {o.end: o for o in orfs.find(bases, True, table_number=11, least=50)}
    # ATG alone starts that frame at an ATG inside the gene, after Rop's own start.
    assert (atg_only[2106].start, atg_only[2106].start_codon) == (1882, "ATG")
    found = {
        o.end: o for o in orfs.find(bases, True, table_number=11, alternative_starts=True, least=50)
    }
    frame = found[2106]
    assert (frame.start, frame.codons, frame.start_codon) == (1747, 119, "GTG")
    assert frame.translation.startswith("M")
    # The record's own Rop, 63 amino acids from its GTG, is the end of this reading frame.
    rop = str(Seq(bases[1914:2106]).translate(table=11))[:-1]
    assert len(rop) == 63
    assert frame.translation.endswith(rop[1:])


def test_the_translation_table_decides_what_stops_and_what_codes(orfs: ModuleType) -> None:
    """A table is not a label: the vertebrate mitochondrial code (table 2) reads AGA and
    AGG as stops and TGA as tryptophan, so the same bases give different ORFs under it
    than under the standard code. Here a stretch whose only in-frame stop is TGA is an ORF
    under table 1 and under table 11, and no ORF under table 2, where TGA codes for
    tryptophan and nothing ends it."""
    coding_bases = "ATG" + "AAA" * 80 + "TGA"
    linear = coding_bases + "C" * 2  # nothing in frame after the stop
    assert [o.codons for o in orfs.find(linear, False, table_number=1, least=75)] == [81]
    assert [o.codons for o in orfs.find(linear, False, table_number=11, least=75)] == [81]
    assert orfs.find(linear, False, table_number=2, least=75) == []
    # And a table whose stops depend on where they are isn't offered at all.
    with pytest.raises(orfs.Refused, match="read as sense codons"):
        orfs.find(linear, False, table_number=27)
    with pytest.raises(orfs.Refused, match="no NCBI translation table"):
        orfs.find(linear, False, table_number=99)


def test_the_longest_orf_before_a_stop_is_the_one_reported(orfs: ModuleType) -> None:
    """One ORF for each stop codon, from the first start after the stop before it: a later
    start in the same frame is part of the same protein's coding sequence, not another
    ORF, which is how ORFfinder and getorf report them."""
    bases = "ATG" + "AAA" * 10 + "ATG" + "AAA" * 10 + "TAA"
    found = orfs.find(bases, False, least=5)
    assert [(o.start, o.codons) for o in found] == [(1, 22)]


# --- the run -------------------------------------------------------------------------


def test_the_run_writes_the_orfs_and_what_it_found_them_with(tmp_path: Path) -> None:
    bases, _, _ = read("L09137.2")
    output, log = command(
        tmp_path,
        "orfs",
        {
            "inputs": {"plasmid": [plasmid(bases)]},
            "params": {
                "least_codons": 75,
                "genetic_code": "bacterial_archaeal_plant_plastid",
                "alternative_starts": False,
            },
        },
    )
    values = output["updates"]["plasmid"][0]["values"]
    assert values["orfs_found_with"] == (
        "ORFs of at least 75 codons, translation table 11 (Bacterial, Archaeal, Plant "
        "Plastid), starting at ATG only; read across its origin"
    )
    bla = next(orf for orf in values["orfs"] if (orf["start"], orf["end"]) == (1626, 2486))
    assert bla == {
        "start": 1626,
        "end": 2486,
        "strand": "reverse",
        "frame": -3,
        "length": 861,
        "amino_acids": 286,
        "start_codon": "ATG",
        "translation": bla["translation"],
    }
    assert output["metrics"] == {"orfs": len(values["orfs"]), "longest": 286}
    assert "frame -3 1626..2486 (reverse), 286 codons from ATG" in log


def test_a_linear_plasmids_run_says_nothing_is_read_across_its_ends(tmp_path: Path) -> None:
    bases, _, _ = read("X01095.1")
    output, log = command(
        tmp_path,
        "orfs",
        {
            "inputs": {"plasmid": [plasmid(bases, topology="linear")]},
            "params": {"least_codons": 200, "genetic_code": "standard"},
        },
    )
    values = output["updates"]["plasmid"][0]["values"]
    assert values["orfs_found_with"].endswith("not read across its two ends")
    assert all(orf["end"] > orf["start"] for orf in values["orfs"])
    assert "ORFs of at least 200 codons in six frames, table 1" in log

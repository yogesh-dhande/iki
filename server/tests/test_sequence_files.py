"""GenBank and FASTA as Iki writes a sequence record (ADR 0171), checked two ways: read by
Biopython, an independent reader, against NCBI's own record; and read back by the dna
plugin's import into the same values it first read from NCBI's.

The records are INSDC's, as NCBI's E-utilities give them, which NCBI places no
restrictions on: pUC19 (L09137.2, circular), phage lambda (J02459.1, linear), and the
human mitochondrial genome (NC_012920.1, circular, whose D-loop is written across the
origin: ``complement(join(16024..16569,1..576))``).
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import warnings
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from Bio import BiopythonParserWarning, SeqIO
from Bio.SeqRecord import SeqRecord

from iki_server.entities.sequence_files import Organism, SequenceFile, fasta, genbank

ROOT = Path(__file__).resolve().parents[2]
IMPORTS = ROOT / "plugins" / "dna" / "scripts" / "imports.py"
DATA = Path(__file__).parent / "data"
RECORDS = {
    "L09137.2": DATA / "plasmids" / "L09137.2.gb",
    "J02459.1": DATA / "genbank" / "J02459.1.gb",
    "NC_012920.1": DATA / "genbank" / "NC_012920.1.gb",
}
FILE_ID = "0190e9a4-0000-7000-8000-000000000001"
DAY = date(2026, 11, 20)


def imported(tmp_path: Path, content: bytes, name: str, topology: str = "linear") -> dict[str, Any]:
    """What the dna plugin's import makes of a file: its one record's values, and its
    organism's."""
    path = tmp_path / name
    path.write_bytes(content)
    run = {
        "params": {"file": FILE_ID, "topology": topology},
        "files": {FILE_ID: {"name": name, "path": str(path), "size": path.stat().st_size}},
    }
    (tmp_path / "input.json").write_text(json.dumps(run))
    (tmp_path / "out").mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(IMPORTS)],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(tmp_path / "out.json"),
            "IKI_OUT_DIR": str(tmp_path / "out"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads((tmp_path / "out.json").read_text())
    organisms = {
        c["key"]: c["values"] for c in output["creates"] if c["type"] == "iki/core.Organism"
    }
    (record,) = [c for c in output["creates"] if c["type"] == "iki/dna.DNASequence"]
    values = dict(record["values"])
    organism = values.pop("organism", None)
    if organism is not None:
        values["organism"] = organisms[organism["$create"]]
    return values


def as_file(values: dict[str, Any], **kw: Any) -> SequenceFile:
    organism = values.get("organism")
    return SequenceFile(
        hrid="DNA-000042",
        name=values.get("name") or "DNA-000042",
        bases=values["bases"],
        topology=values["topology"],
        strandedness=values.get("strandedness", "double"),
        revision=3,
        date=DAY,
        description=values.get("description", ""),
        accession=values.get("accession", ""),
        organism=Organism(organism.get("scientific_name", ""), organism["taxon_id"])
        if organism
        else None,
        features=tuple(values.get("features") or ()),
        **kw,
    )


def biopython(content: bytes) -> SeqRecord:
    with warnings.catch_warnings():
        warnings.simplefilter("error", BiopythonParserWarning)
        found: SeqRecord = SeqIO.read(io.StringIO(content.decode()), "genbank")  # type: ignore[no-untyped-call]
        return found


def parts(feature: Any) -> list[tuple[int, int, int | None]]:
    return [(int(p.start), int(p.end), p.strand) for p in feature.location.parts]


def qualifiers(feature: Any) -> dict[str, list[str]]:
    """Its qualifiers, a repeated one's values joined as the import joins them, but those
    written one a value again."""
    out = {}
    for name, values in feature.qualifiers.items():
        if name in ("db_xref", "EC_number", "old_locus_tag", "transl_except"):
            out[name] = list(values)
        else:
            out[name] = ["; ".join(values)]
    return out


@pytest.mark.parametrize("accession", sorted(RECORDS))
def test_an_ncbi_record_written_as_genbank_is_ncbis_record(tmp_path: Path, accession: str) -> None:
    """Its bases, topology and features, each with its location and qualifiers, are what
    NCBI's record gives, as Biopython reads both."""
    original = RECORDS[accession].read_bytes()
    values = imported(tmp_path, original, f"{accession}.gb")
    written = genbank(as_file(values))
    ncbi, ours = biopython(original), biopython(written.content)
    assert str(ours.seq) == str(ncbi.seq)
    assert ours.annotations["topology"] == ncbi.annotations["topology"]
    assert ours.annotations["molecule_type"] == ncbi.annotations["molecule_type"]
    # Biopython types its annotations as text or numbers; accessions are a list.
    annotations: dict[str, Any] = dict(ours.annotations)
    assert annotations["accessions"] == [accession.split(".")[0]]
    assert ours.id == accession
    assert ours.description == ncbi.description
    assert written.left_out == []
    # Its organism is its source feature's: the one feature Iki keeps as a field.
    assert ours.annotations["organism"] == ncbi.annotations["organism"]
    (source,) = [f for f in ours.features if f.type == "source"]
    taxon = next(x for x in ncbi.features[0].qualifiers["db_xref"] if x.startswith("taxon:"))
    assert source.qualifiers["db_xref"] == [taxon]
    theirs = [f for f in ncbi.features if f.type != "source"]
    mine = [f for f in ours.features if f.type != "source"]
    assert len(mine) == len(theirs)
    for a, b in zip(mine, theirs, strict=True):
        assert (a.type, parts(a)) == (b.type, parts(b))
        expected = qualifiers(b)
        if "label" not in expected and a.qualifiers.get("label"):
            # A feature named by no qualifier the import reads a name from.
            expected["label"] = a.qualifiers["label"]
        assert qualifiers(a) == expected, (a.type, a.location)


@pytest.mark.parametrize("accession", sorted(RECORDS))
def test_an_exported_record_imports_as_the_record_it_was(tmp_path: Path, accession: str) -> None:
    """NCBI's record into Iki, out as GenBank and into Iki again: the same values."""
    first = imported(tmp_path, RECORDS[accession].read_bytes(), f"{accession}.gb")
    again = imported(tmp_path, genbank(as_file(first)).content, "exported.gb")
    assert without_texts(again) == without_texts(first)


def without_texts(values: dict[str, Any]) -> dict[str, Any]:
    """Its values, but each feature's ``location`` qualifier: the text the import read,
    which INSDC lets a record write more than one way (NCBI writes one partial base as
    ``<23231`` in one feature and ``21737..>21737`` in another). The parts, with their
    partial ends, are what it says."""
    out = dict(values)
    out["features"] = [
        {
            **f,
            "qualifiers": {k: v for k, v in (f.get("qualifiers") or {}).items() if k != "location"},
        }
        for f in values.get("features") or ()
    ]
    return out


def test_locus_keeps_insdcs_columns(tmp_path: Path) -> None:
    values = imported(tmp_path, RECORDS["L09137.2"].read_bytes(), "L09137.2.gb")
    written = genbank(as_file(values)).content.decode()
    locus = written.splitlines()[0]
    ncbi = RECORDS["L09137.2"].read_text().splitlines()[0]
    # NCBI's, but its division (an INSDC database's own classification) and its date.
    assert locus[:64] == ncbi[:64]
    assert (
        locus == "LOCUS       SYNPUC19CV              2686 bp    DNA     circular     20-NOV-2026"
    )
    assert len(locus) == 79


def test_a_single_stranded_molecule_says_so_on_its_locus_line() -> None:
    record = SequenceFile(
        "DNA-000001", "M13", "ACGT" * 5, "circular", 1, DAY, strandedness="single"
    )
    locus = genbank(record).content.decode().splitlines()[0]
    assert locus[44:53] == "ss-DNA   "
    assert biopython(genbank(record).content).annotations["molecule_type"] == "ss-DNA"


def test_a_feature_across_a_circles_origin_is_joined_round_it() -> None:
    record = SequenceFile(
        "DNA-000001",
        "pDemo",
        "ACGT" * 25,
        "circular",
        1,
        DAY,
        features=(
            {"type": "CDS", "start": 90, "end": 10, "strand": "forward"},
            {"type": "promoter", "start": 95, "end": 5, "strand": "reverse"},
            {
                "type": "misc_feature",
                "start": 100,
                "end": 1,
                "strand": "forward",
                "parts": [{"start": 100, "end": 1, "strand": "forward", "between": True}],
            },
        ),
    )
    text = genbank(record).content.decode()
    assert "     CDS             join(90..100,1..10)\n" in text
    assert "     promoter        complement(join(95..100,1..5))\n" in text
    assert "     misc_feature    100^1\n" in text
    read = biopython(genbank(record).content)
    assert parts(read.features[0]) == [(89, 100, 1), (0, 10, 1)]


def test_nothing_reads_across_a_linear_molecules_ends() -> None:
    """A feature across the ends of a linear molecule, or beyond them, is left out, and the
    file's COMMENT says which and why: GenBank can't say it, and nothing is dropped
    unsaid."""
    record = SequenceFile(
        "DNA-000001",
        "fragment",
        "ACGT" * 25,
        "linear",
        1,
        DAY,
        features=(
            {"type": "CDS", "name": "bla", "start": 90, "end": 10, "strand": "forward"},
            {"type": "gene", "start": 90, "end": 120, "strand": "forward"},
            {"type": "promoter", "start": 1, "end": 10, "strand": "forward"},
        ),
    )
    written = genbank(record)
    assert written.left_out == [
        "Left out a CDS (bla): it reads across the ends of a linear molecule (90..10), which "
        "only a circle's origin allows.",
        "Left out a gene: it lies beyond the sequence's 100 bases (90..120).",
    ]
    text = written.content.decode()
    assert "     promoter        1..10\n" in text
    assert "     CDS " not in text
    comment = " ".join(str(biopython(written.content).annotations["comment"]).split())
    assert "Left out a CDS (bla): it reads across the ends of a linear molecule" in comment


def test_parts_partial_ends_and_orders_are_written_as_insdc_writes_them() -> None:
    feature = {
        "type": "mRNA",
        "start": 2,
        "end": 40,
        "strand": "reverse",
        "parts": [
            {"start": 30, "end": 40, "strand": "reverse", "partial_end": True},
            {"start": 2, "end": 9, "strand": "reverse", "partial_start": True},
        ],
    }
    ordered = {
        "type": "misc_feature",
        "start": 5,
        "end": 20,
        "strand": "mixed",
        "operator": "order",
        "parts": [
            {"start": 5, "end": 8, "strand": "forward"},
            {"start": 15, "end": 20, "strand": "reverse"},
            {"start": 12, "end": 12, "strand": "forward"},
        ],
    }
    record = SequenceFile("DNA-1", "x", "ACGT" * 25, "linear", 1, DAY, features=(feature, ordered))
    text = genbank(record).content.decode()
    assert "     mRNA            complement(join(<2..9,30..>40))\n" in text
    assert "     misc_feature    order(5..8,complement(15..20),12)\n" in text


def test_a_name_locus_cant_hold_is_kept_in_the_comment() -> None:
    long = SequenceFile("PLS-000007", "pLKO.1 puro shRNA GFP", "ACGT" * 5, "circular", 4, DAY)
    text = genbank(long).content.decode()
    assert text.startswith("LOCUS       PLS-000007 ")
    assert "Name: pLKO.1 puro shRNA GFP." in text
    short = SequenceFile("PLS-000007", "pUC19 AmpR", "ACGT" * 5, "circular", 4, DAY)
    assert genbank(short).content.decode().startswith("LOCUS       pUC19_AmpR ")


def test_the_comment_says_which_record_and_revision_it_is() -> None:
    record = SequenceFile(
        "PLS-000007",
        "pUC19",
        "ACGT" * 5,
        "circular",
        4,
        DAY,
        sequence_of="DNA-000003",
        latest=False,
    )
    comment = str(biopython(genbank(record).content).annotations["comment"])
    assert " ".join(comment.split()) == (
        "Exported from Iki: PLS-000007 (revision 4, not its latest), whose sequence is DNA-000003."
    )


def test_a_record_without_an_accession_names_none() -> None:
    text = genbank(SequenceFile("DNA-1", "x", "ACGT", "linear", 1, DAY)).content.decode()
    assert "ACCESSION" not in text and "VERSION" not in text
    assert "DEFINITION  .\n" in text


def test_qualifiers_are_quoted_as_insdc_writes_them() -> None:
    feature = {
        "type": "CDS",
        "name": "bla",
        "start": 1,
        "end": 99,
        "strand": "forward",
        "qualifiers": {
            "gene": "bla",
            "codon_start": "1",
            "note": 'the "beta" lactamase',
            "pseudo": "",
            "translation": "M" * 100,
            "db_xref": "GeneID:1; UniProtKB/Swiss-Prot:P62593",
            "location": "1..99",
            "not a name": "x",
        },
    }
    written = genbank(SequenceFile("DNA-1", "x", "A" * 99, "linear", 1, DAY, features=(feature,)))
    text = written.content.decode()
    assert "/codon_start=1\n" in text
    assert '/note="the ""beta"" lactamase"\n' in text
    assert "                     /pseudo\n" in text
    assert (
        '/db_xref="GeneID:1"\n                     /db_xref="UniProtKB/Swiss-Prot:P62593"' in text
    )
    assert "/location" not in text and "/label" not in text  # its name is its /gene
    assert written.left_out == [
        "Left out a CDS (bla)'s qualifier 'not a name': GenBank can't name one so."
    ]
    read = biopython(written.content).features[0]
    assert read.qualifiers["translation"] == ["M" * 100]
    assert read.qualifiers["note"] == ['the "beta" lactamase']
    assert max(len(line) for line in text.splitlines()) <= 79


def test_a_feature_with_no_strand_is_said_to_read_forward() -> None:
    feature = {"type": "misc_feature", "name": "spacer", "start": 1, "end": 4, "strand": "none"}
    text = genbank(SequenceFile("DNA-1", "x", "ACGT" * 2, "linear", 1, DAY, features=(feature,)))
    comment = " ".join(str(biopython(text.content).annotations["comment"]).split())
    assert "1 feature has no strand in Iki, which GenBank can't say" in comment
    assert '/label="spacer"' in text.content.decode()


def test_a_type_genbank_cant_hold_is_written_as_a_misc_feature() -> None:
    feature = {"type": "engineered region", "start": 1, "end": 4, "strand": "forward"}
    written = genbank(SequenceFile("DNA-1", "x", "ACGT" * 2, "linear", 1, DAY, features=(feature,)))
    read = biopython(written.content).features[0]
    assert read.type == "misc_feature"
    assert read.qualifiers["note"] == ["Iki feature type: engineered region"]
    assert len(written.left_out) == 1


def test_the_same_revision_is_the_same_file(tmp_path: Path) -> None:
    values = imported(tmp_path, RECORDS["J02459.1"].read_bytes(), "J02459.1.gb")
    assert genbank(as_file(values)).content == genbank(as_file(values)).content


def test_fasta_names_the_record_and_its_topology(tmp_path: Path) -> None:
    record = SequenceFile(
        "DNA-000042",
        "pUC19",
        "ACGT" * 40,
        "circular",
        2,
        DAY,
        description="Cloning vector pUC19c",
        features=({"type": "CDS", "start": 1, "end": 9},),
    )
    written = fasta(record)
    lines = written.content.decode().splitlines()
    assert lines[0] == ">DNA-000042 pUC19 Cloning vector pUC19c [topology=circular]"
    assert [len(line) for line in lines[1:]] == [70, 70, 20]
    assert written.left_out == ["FASTA holds no features: its feature isn't in it."]
    read = SeqIO.read(io.StringIO(written.content.decode()), "fasta")  # type: ignore[no-untyped-call]
    assert read.id == "DNA-000042" and str(read.seq) == record.bases
    # Iki's import reads its topology back from NCBI's source modifier.
    again = imported(tmp_path, written.content, "DNA-000042.fasta", topology="linear")
    assert again["topology"] == "circular"
    assert again["bases"] == record.bases
    assert again["description"] == "pUC19 Cloning vector pUC19c"


# --- SnapGene's files, through Iki, as GenBank -------------------------------------------------

SNAPGENE = sorted((DATA / "snapgene").glob("*.dna"))


@pytest.mark.parametrize("path", SNAPGENE, ids=[p.stem for p in SNAPGENE])
def test_a_snapgene_file_exports_as_genbank_with_what_snapgene_says(
    path: Path, tmp_path: Path
) -> None:
    """SnapGene's own files (made by SnapGene, as Biopython's tests have them), imported and
    written as GenBank, say what Biopython reads in the SnapGene file: its bases, topology,
    and each feature's type, place, strand and qualifiers. Iki doesn't write SnapGene's
    format (ADR 0171); SnapGene opens GenBank.

    What differs is said here: a qualifier SnapGene gives several values is one text in
    Iki, its values joined by "; "; a feature's name is its ``/label``, as SnapGene writes
    GenBank, where Biopython makes it a ``/name`` (not an INSDC qualifier); and its
    segments' names, which the import says it doesn't keep, aren't written."""
    with path.open("rb") as handle:
        snapgene: SeqRecord = SeqIO.read(handle, "snapgene")  # type: ignore[no-untyped-call]
    written = genbank(as_file(imported(tmp_path, path.read_bytes(), path.name)))
    ours = biopython(written.content)
    assert written.left_out == []
    assert str(ours.seq) == str(snapgene.seq).upper()
    assert ours.annotations["topology"] == snapgene.annotations["topology"]

    def place(f: Any) -> tuple[str, str]:
        return f.type, str(f.location)

    assert sorted(map(place, ours.features)) == sorted(map(place, snapgene.features))
    for theirs in snapgene.features:
        (mine,) = [f for f in ours.features if place(f) == place(theirs)]
        expected = {
            k: ["; ".join(str(v) for v in values)]
            for k, values in theirs.qualifiers.items()
            if k not in ("name", "parts")
        }
        name = theirs.qualifiers.get("name")
        if name and name[0] not in expected.get("label", [""])[0].split("; "):
            expected["label"] = [name[0], *expected.get("label", [])]
        got = {k: ["; ".join(v)] if k != "label" else v for k, v in mine.qualifiers.items()}
        assert got == expected, theirs

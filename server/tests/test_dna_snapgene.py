"""The DNA plugin's import of SnapGene files (`.dna`), run as a worker runs it: an
input.json in, an output.json out.

The files in data/snapgene were made by SnapGene itself and come from Biopython's tests
(see their README). Each is read here by the plugin's own reader and checked against
Biopython's (Bio.SeqIO's "snapgene"), an independent reading of the same format, where the
two should agree. FASTA's topology modifier and the line each record's run output gives
about where its topology came from are checked here too.
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from Bio import SeqIO

from iki_spec import schemas

IMPORTS = Path(__file__).resolve().parents[2] / "plugins" / "dna" / "scripts" / "imports.py"
DATA = Path(__file__).parent / "data" / "snapgene"
FILE_ID = "0190e9a4-0000-7000-8000-000000000002"
FILES = [
    "sample-d",
    "sample-e",
    "sample-f",
    "sample-hybridization-params",
    "looped_feature",
    "linebreak_in_qualifier_text",
]


def imported(
    tmp_path: Path, data: bytes, *, name: str = "construct.dna", topology: str = "linear"
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    path = tmp_path / name
    path.write_bytes(data)
    run_input = {
        "params": {"file": FILE_ID, "topology": topology},
        "files": {FILE_ID: {"name": name, "path": str(path), "size": len(data)}},
    }
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-E", "-s", str(IMPORTS)],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if not out.exists():
        return result, None
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


def ok(tmp_path: Path, data: bytes, **given: Any) -> tuple[str, dict[str, Any]]:
    result, output = imported(tmp_path, data, **given)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    return result.stdout, output


def sequence(output: dict[str, Any]) -> dict[str, Any]:
    (found,) = [c for c in output["creates"] if c["type"] == "iki/dna.DNASequence"]
    return dict(found)


def primers(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in output["creates"] if c["type"] == "iki/dna.Primer"]


def messages(output: dict[str, Any]) -> list[str]:
    return [m["text"] for m in output["messages"]]


def vendored(name: str) -> bytes:
    return (DATA / f"{name}.dna").read_bytes()


def biopython_read(name: str) -> Any:
    """Biopython's reading of a file, from a handle closed here: its reader leaves a file it
    opened itself open."""
    with (DATA / f"{name}.dna").open("rb") as handle:
        return SeqIO.read(handle, "snapgene")  # type: ignore[no-untyped-call]


# --- Files built here, for what SnapGene's own don't show ----------------------------------------


def packet(kind: int, data: bytes) -> bytes:
    return bytes([kind]) + struct.pack(">I", len(data)) + data


def cookie(kind: int = 1) -> bytes:
    return packet(0x09, b"SnapGene" + struct.pack(">HHH", kind, 15, 20))


def xml(root: str, body: str = "", **attributes: str) -> bytes:
    attrs = "".join(f' {k}="{v}"' for k, v in attributes.items())
    return f'<?xml version="1.0"?><{root}{attrs}>{body}</{root}>'.encode()


def dna(
    bases: str = "ACGTACGTAC" * 10,
    *,
    flags: int = 0x03,
    features: str = "",
    primers: str = "",
    notes: str = "",
    extra: bytes = b"",
) -> bytes:
    """A SnapGene file: circular and double-stranded unless ``flags`` says otherwise."""
    out = cookie() + packet(0x00, bytes([flags]) + bases.encode())
    if features:
        out += packet(0x0A, xml("Features", features))
    if primers:
        out += packet(0x05, xml("Primers", primers))
    if notes:
        out += packet(0x06, f"<Notes>{notes}</Notes>".encode())
    return out + extra


def feature(segments: str, *, name: str = "F", kind: str = "misc_feature", **attrs: str) -> str:
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f'<Feature name="{name}" type="{kind}"{extra}>{segments}</Feature>'


def segment(at: str, kind: str = "standard", **attrs: str) -> str:
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f'<Segment range="{at}" color="#a6acb3" type="{kind}"{extra}/>'


# --- SnapGene's files ----------------------------------------------------------------------------


def test_a_snapgene_file_keeps_its_bases_topology_strands_notes_and_features(
    tmp_path: Path,
) -> None:
    data = vendored("sample-d")
    stdout, output = ok(tmp_path, data, name="sample-d.dna", topology="circular")
    found = sequence(output)
    values = found["values"]
    assert values["bases"] == str(biopython_read("sample-d").seq).upper()
    # A SnapGene file names no sequence: SnapGene shows its file's name.
    assert values["name"] == "sample-d"
    # Its Comments are its Description, so the description is said once.
    assert values["description"] == "Sample Sequence D"
    # Linear and double-stranded, as its flags say, whatever the FASTA param says.
    assert (values["topology"], values["strandedness"]) == ("linear", "double")
    assert values["features"] == [
        {
            # No directionality: SnapGene draws it without an arrow.
            "type": "misc_binding",
            "start": 500,
            "end": 700,
            "strand": "none",
            "name": "FeatureB",
            "qualifiers": {
                "bound_moiety": "Random DNA-Binding Protein 1",
                "note": "Sample feature B",
            },
        },
        {
            "type": "promoter",
            "start": 50,
            "end": 150,
            "strand": "forward",
            "name": "FeatureA",
            "qualifiers": {"note": "Sample feature A"},
        },
        {
            # Two values of one qualifier, joined as GenBank's repeated ones are.
            "type": "misc_feature",
            "start": 701,
            "end": 720,
            "strand": "none",
            "name": "FeatureC",
            "qualifiers": {
                "label": "FeatureC",
                "note": "Sample feature C, with explicit label; Another note for sample feature C",
            },
        },
        {
            # Its name and its label differ, and both are kept.
            "type": "misc_feature",
            "start": 721,
            "end": 740,
            "strand": "none",
            "name": "FeatureD",
            "qualifiers": {
                "label": "SampleFeatureD",
                "note": "Sample feature D, with a label different from the feature name",
            },
        },
    ]
    assert found["origin"] == {
        "source": "SnapGene file",
        "id": "sample-d",
        "file": FILE_ID,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert primers(output) == []
    assert output["metrics"] == {"records": 1}
    # What of its notes has no field, said once.
    assert messages(output) == [
        "sample-d: these of its SnapGene notes aren't kept, as a DNA sequence has no field "
        "for them (the file is kept, as the record's origin): its type (Natural)."
    ]
    assert "sample-d: 1,000 bp, linear, as its SnapGene file says" in stdout.splitlines()


def test_a_circular_snapgene_file_is_circular_and_says_what_methylates_it(
    tmp_path: Path,
) -> None:
    _, output = ok(tmp_path, vendored("sample-e"), name="sample-e.dna")
    values = sequence(output)["values"]
    assert (values["topology"], values["strandedness"]) == ("circular", "double")
    reverse, forward = values["features"]
    assert (reverse["name"], reverse["strand"], reverse["start"], reverse["end"]) == (
        "FeatureB",
        "reverse",
        400,
        750,
    )
    assert forward["qualifiers"] == {"note": "Random origin of replication", "direction": "RIGHT"}
    assert messages(output) == [
        "sample-e: SnapGene says the strain it's grown in methylates it (Dam, Dcm and "
        "EcoKI), which isn't kept: it's the host's doing, not the sequence's.",
        "sample-e: these of its SnapGene notes aren't kept, as a DNA sequence has no field "
        "for them (the file is kept, as the record's origin): its type (Synthetic).",
    ]


def test_a_split_feature_keeps_its_parts_in_the_order_they_read(tmp_path: Path) -> None:
    """sample-f's features have gaps between their segments, as a gene's introns are, and
    names for some segments, which a feature's parts can't hold."""
    _, output = ok(tmp_path, vendored("sample-f"), name="sample-f.dna")
    reverse, forward, bind = sequence(output)["values"]["features"]
    # A reverse-strand feature's parts are read from its last to its first, as INSDC's
    # complement(join(...)) reads them; the gaps between segments aren't parts.
    assert reverse == {
        "type": "terminator",
        "start": 400,
        "end": 724,
        "strand": "reverse",
        "parts": [
            {"start": 635, "end": 724, "strand": "reverse"},
            {"start": 517, "end": 634, "strand": "reverse"},
            {"start": 400, "end": 499, "strand": "reverse"},
        ],
        "name": "FeatureB",
        "qualifiers": {"note": "An example of a reverse-strand split feature"},
    }
    assert forward["parts"] == [
        {"start": 161, "end": 180, "strand": "forward"},
        {"start": 188, "end": 207, "strand": "forward"},
        {"start": 215, "end": 241, "strand": "forward"},
    ]
    assert (forward["start"], forward["end"]) == (161, 241)
    # Its one primer, at the bases SnapGene numbers from 0: 751-775 is 752..776.
    assert bind == {
        "type": "primer_bind",
        "start": 752,
        "end": 776,
        "strand": "forward",
        "name": "Primer 1",
    }
    said = messages(output)
    assert (
        "sample-f: a terminator (FeatureB)'s segment names (subfeature3, subfeature1) "
        "aren't kept, as a feature's parts have none."
    ) in said
    assert (
        "sample-f: a rep_origin (FeatureA)'s segment names (subfeature2) aren't kept, as a "
        "feature's parts have none."
    ) in said


def test_primers_are_registered_and_bound_where_snapgene_shows_them(tmp_path: Path) -> None:
    """sample-hybridization-params has two primers with 5' tails. Its forward primer also
    binds weakly elsewhere, below the file's hybridization settings, where SnapGene doesn't
    show it."""
    _, output = ok(tmp_path, vendored("sample-hybridization-params"), name="hht2.dna")
    found = sequence(output)
    binds = [f for f in found["values"]["features"] if f["type"] == "primer_bind"]
    assert binds == [
        {
            "type": "primer_bind",
            "start": 498,
            "end": 516,
            "strand": "forward",
            "name": "XhoI-hht2(US)-Fwd",
        },
        {
            "type": "primer_bind",
            "start": 1397,
            "end": 1411,
            "strand": "reverse",
            "name": "XmaI-hht2(Cter)-Rev",
        },
    ]
    bases = found["values"]["bases"]
    # The bases a forward primer's 3' end anneals to are the top strand's there.
    assert bases[497:516] == "CTCGAGGAAAAGCTTCAAC"
    # Each primer is its own record, of all its bases, tail and all, bound to the
    # sequence the file is of.
    key = found["key"]
    assert [p["values"] for p in primers(output)] == [
        {
            "name": "XhoI-hht2(US)-Fwd",
            "bases": "AAAAAACTCGAGGAAAAGCTTCAAC",
            "direction": "forward",
            "target": {"$create": key},
        },
        {
            "name": "XmaI-hht2(Cter)-Rev",
            "bases": "AAAAAACCCGGGAGAACGCTCACCACG",
            "direction": "reverse",
            "target": {"$create": key},
        },
    ]
    origin = primers(output)[0]["origin"]
    assert (origin["source"], origin["id"], origin["file"]) == (
        "SnapGene file",
        "XhoI-hht2(US)-Fwd",
        FILE_ID,
    )
    assert (
        "hht2: primer XhoI-hht2(US)-Fwd binds at 1292..1301 (reverse) below the file's "
        "hybridization settings (at least 10 bases annealed, at a Tm of at least 40 °C). "
        "SnapGene doesn't show such sites, so they were left out."
    ) in messages(output)


def test_a_primers_description_is_kept_and_html_and_line_breaks_go(tmp_path: Path) -> None:
    _, output = ok(tmp_path, vendored("linebreak_in_qualifier_text"), name="oriT.dna")
    (orit,) = [f for f in sequence(output)["values"]["features"] if f["type"] == "oriT"]
    assert orit["qualifiers"] == {"note": "Origin of transference region of RP4 plasmid"}
    described = {p["values"]["name"]: p["values"].get("description") for p in primers(output)}
    assert described == {
        "AC4_Fragment.REV": None,
        "P.SEVA.AbR.R": "05/09/2017,Alex Primers 1,37",
        "P.SEVA.ORI.F": "03/08/2017,Alex Primers 1,33",
    }


def test_a_feature_across_the_origin_is_only_on_a_circular_molecule(tmp_path: Path) -> None:
    """looped_feature is 10 bases round a circle, with a feature from base 3 round to
    base 2: all of it, read from 3."""
    data = vendored("looped_feature")
    _, output = ok(tmp_path, data, name="looped.dna")
    (looped,) = sequence(output)["values"]["features"]
    assert looped["parts"] == [
        {"start": 3, "end": 10, "strand": "none"},
        {"start": 1, "end": 2, "strand": "none"},
    ]
    assert (
        "looped: SnapGene's history of how it was made isn't kept; the file is, as the "
        "record's origin." in messages(output)
    )
    # The same file, its flag saying linear: a linear molecule has no origin to cross.
    at = data.index(b"\x00\x00\x00\x00\x0b") + 5
    linear = data[:at] + bytes([data[at] & ~0x01]) + data[at + 1 :]
    _, output = ok(tmp_path, linear, name="looped.dna")
    values = sequence(output)["values"]
    assert values["topology"] == "linear"
    assert "features" not in values
    assert (
        "looped: a misc_feature (Feature 1) at 3-2 reads across the origin, which only a "
        "circular molecule has, and the SnapGene file says it's linear. It was left out: "
        "say the molecule is circular, or give the feature's place on the linear one."
    ) in messages(output)


@pytest.mark.parametrize("name", FILES)
def test_biopythons_reader_reads_the_same_bases_topology_and_features(
    tmp_path: Path, name: str
) -> None:
    """Biopython reads SnapGene files independently of this import. Where the two can
    differ: Biopython takes a feature with no directionality as forward, and doesn't
    read strandedness, so those aren't compared."""
    _, output = ok(tmp_path, vendored(name), name=f"{name}.dna")
    ours = sequence(output)["values"]
    theirs = biopython_read(name)
    assert ours["bases"] == str(theirs.seq).upper()
    assert ours["topology"] == theirs.annotations["topology"]

    def ranges_of(f: dict[str, Any]) -> tuple[str, tuple[tuple[int, int], ...]]:
        parts = f.get("parts") or [f]
        return f["type"], tuple(sorted((p["start"], p["end"]) for p in parts))

    def biopython(f: Any) -> tuple[str, tuple[tuple[int, int], ...]]:
        return f.type, tuple(sorted((int(p.start) + 1, int(p.end)) for p in f.location.parts))

    assert sorted(ranges_of(f) for f in ours.get("features", [])) == sorted(
        biopython(f) for f in theirs.features
    )
    strands = {
        ranges_of(f): f["strand"]
        for f in ours.get("features", [])
        if f["strand"] in ("forward", "reverse")
    }
    for f in theirs.features:
        if biopython(f) in strands:
            assert strands[biopython(f)] == ("forward" if f.location.strand == 1 else "reverse")


# --- What a file holds that a sequence doesn't ---------------------------------------------------


def test_what_isnt_kept_is_said(tmp_path: Path) -> None:
    notes = (
        "<Type>Synthetic</Type><ConfirmedExperimentally>1</ConfirmedExperimentally>"
        "<Description>&lt;html&gt;&lt;body&gt;A &lt;i&gt;test&lt;/i&gt; vector&lt;/body"
        "&gt;&lt;/html&gt;</Description><Comments>Made for a test.</Comments>"
        "<AccessionNumber>not one</AccessionNumber><Organism>Escherichia coli</Organism>"
        "<TransformedInto>DH5\u03b1\u2122</TransformedInto><CustomMapLabel>pTest</CustomMapLabel>"
        "<UseCustomMapLabel>1</UseCustomMapLabel>"
        '<References><Reference pubMedID="7747518" title="A"/><Reference title="B"/>'
        "</References><Created>2020.1.1</Created><CreatedBy>Someone</CreatedBy>"
    )
    features = (
        feature(segment("1-10"), name="both", kind="promoter", directionality="3")
        + feature(segment("11-40"), name="sig", kind="CDS", directionality="1", cleavageArrows="30")
        + feature(segment("200-210"), name="outside")
        + feature("", name="empty")
    )
    extra = (
        packet(0x07, b"\xfd7zXZ history")
        + packet(0x0B, b"history node")
        + packet(
            0x11,
            b'<AlignableSequences><Sequence name="read1"/>'
            b'<Sequence name="read2"/></AlignableSequences>',
        )
        + packet(0x12, b"trace")
        + packet(0x13, b"uracil")
        + packet(0x2A, b"new")
        + packet(0x03, b"enzymes")
        + packet(0x0E, b"<CustomEnzymeSets/>")
    )
    _, output = ok(
        tmp_path,
        dna("ACGTacgtAC" * 10, flags=0x1F, features=features, notes=notes, extra=extra),
        name="whatever.dna",
    )
    values = sequence(output)["values"]
    assert values["name"] == "pTest"
    assert values["description"] == "A test vector\n\nComments: Made for a test."
    assert "accession" not in values and "organism" not in values
    assert [(f["name"], f["strand"]) for f in values["features"]] == [
        ("both", "none"),
        ("sig", "forward"),
    ]
    assert messages(output) == [
        "pTest: SnapGene says the strain it's grown in methylates it (Dam, Dcm and EcoKI), "
        "which isn't kept: it's the host's doing, not the sequence's.",
        "pTest: its bases are in both upper and lower case, which SnapGene shows but reads "
        "as the same bases. They're kept in upper case.",
        "pTest: a promoter (both) is marked as running both ways, which a feature's strand "
        "can't say, so it's kept with no strand.",
        "pTest: a CDS (sig)'s cleavage sites (30) aren't kept.",
        "pTest: a misc_feature (outside) at 200-210 isn't on its 100 bases, so it was left out.",
        "pTest: a misc_feature (empty) has no segments, so it was left out.",
        "pTest: its accession number, not one, isn't one an accession can be (letters, "
        "digits, _ and .), so it's left out.",
        "pTest: its organism, Escherichia coli, has no NCBI Taxonomy ID (a SnapGene file "
        "gives only its name), so it's left out.",
        "pTest: these of its SnapGene notes aren't kept, as a DNA sequence has no field for "
        "them (the file is kept, as the record's origin): its type (Synthetic), that it was "
        "confirmed experimentally, what it was transformed into (DH5\u03b1\u2122), its 2 "
        "references (PubMed 7747518).",
        "pTest: SnapGene's history of how it was made isn't kept; the file is, as the "
        "record's origin.",
        "pTest: the 2 sequences aligned to it in SnapGene, such as sequencing reads, aren't "
        "kept: align them to it with align_reads.",
        "pTest: parts of the SnapGene file this import doesn't read were left out: "
        "sequencing traces (packet 0x12), uracil positions (packet 0x13), packet 0x2a.",
    ]


def test_a_linear_molecules_overhangs_and_modified_ends_are_said(tmp_path: Path) -> None:
    ends = (
        "<AdditionalSequenceProperties><UpstreamStickiness>4</UpstreamStickiness>"
        "<DownstreamStickiness>0</DownstreamStickiness>"
        "<UpstreamModification>FivePrimePhosphorylated</UpstreamModification>"
        "<DownstreamModification>Unmodified</DownstreamModification>"
        "</AdditionalSequenceProperties>"
    )
    data = dna(flags=0x02, extra=packet(0x08, ends.encode()))
    _, output = ok(tmp_path, data, name="cut.dna")
    assert messages(output) == [
        "cut: SnapGene gives its ends an upstream stickiness of 4 and an upstream "
        "modification (FivePrimePhosphorylated), which isn't kept: Iki reads a linear "
        "molecule as fully paired, with plain ends (ADR 0168)."
    ]
    # A circle has no ends, whatever the packet says of them.
    _, output = ok(tmp_path, dna(flags=0x03, extra=packet(0x08, ends.encode())), name="c.dna")
    assert messages(output) == []


def test_a_single_stranded_file_is_single_stranded(tmp_path: Path) -> None:
    _, output = ok(tmp_path, dna(flags=0x01), name="m13.dna")
    values = sequence(output)["values"]
    assert (values["topology"], values["strandedness"]) == ("circular", "single")


def test_a_primer_that_cant_be_a_primer_is_left_out_but_still_binds(tmp_path: Path) -> None:
    site = '<BindingSite location="0-9" boundStrand="0" annealedBases="ACGTACGTAC"/>'
    long = "A" * 190 + "ACGTACGTAC" + "A"
    given = (
        f'<Primer name="odd" sequence="ACGTXACGTAC">{site}</Primer>'
        f'<Primer name="long" sequence="{long}">{site}</Primer>'
    )
    _, output = ok(tmp_path, dna(primers=given), name="p.dna")
    assert primers(output) == []
    assert [f["name"] for f in sequence(output)["values"]["features"]] == ["odd", "long"]
    assert messages(output) == [
        "p: primer odd isn't kept as a primer: its bases have 'X' at base 5, which isn't an "
        "IUPAC base. Its binding sites are kept as features.",
        "p: primer long isn't kept as a primer: it's 201 bases, and a primer is at most "
        "200. Its binding sites are kept as features.",
    ]


# --- Files that can't be read --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (
            cookie() + packet(0x00, b"\x03ACGT")[:7],
            "It's cut short: its DNA packet says it has 5 bytes, and the file ends after 2.",
        ),
        (
            cookie()[:9],
            "It's cut short: its first packet says it has 14 bytes, and the file ends after 4.",
        ),
        (
            cookie(2) + packet(0x00, b"\x03ACGT"),
            "It's a SnapGene file, but not of DNA (its sequence type is 2; a .dna file's "
            "is 1), such as a protein (.prot) or RNA (.rna) file.",
        ),
        (cookie() + packet(0x06, b"<Notes/>"), "It has no DNA packet, so it has no bases."),
        (
            cookie() + packet(0x01, b"compressed"),
            "It keeps its bases compressed (packet 0x01), which this import can't read: "
            "save it from SnapGene as GenBank and import that.",
        ),
        (
            cookie() + packet(0x00, b"\x03ACGT") + packet(0x00, b"\x03ACGT"),
            "It has more than one DNA packet.",
        ),
        (
            dna(features='</Features><!DOCTYPE x [<!ENTITY a "b">]><Features>'),
            "Its features packet declares a DOCTYPE or entities, which a SnapGene file "
            "doesn't: it isn't read.",
        ),
        (
            cookie() + packet(0x00, b"\x03ACGT") + packet(0x0A, b"<Features><Feature"),
            "Its features packet isn't XML that can be read",
        ),
        (
            cookie() + packet(0x00, b"\x03ACUT"),
            "construct has U at base 3: it's RNA, not DNA.",
        ),
    ],
)
def test_a_file_that_cant_be_read_fails_the_import_saying_why(
    tmp_path: Path, data: bytes, why: str
) -> None:
    result, output = imported(tmp_path, data)
    assert result.returncode == 1
    assert output is None
    assert result.stdout.startswith(f"::error construct.dna: {why}"), result.stdout


# --- Where each record's topology came from ------------------------------------------------------


def test_a_fasta_header_can_say_its_topology_as_ncbi_writes_it(tmp_path: Path) -> None:
    """NCBI's FASTA source modifiers (`[topology=circular]`), which Iki's FASTA export
    writes: the header's own word wins over the param, as GenBank's LOCUS line does."""
    text = (
        ">DNA-000001 pUC19 cloning vector [topology=circular]\nACGT\n"
        ">DNA-000002 [Topology=Linear] a fragment\nGGCC\n"
        ">seq3 no word on it\nTTAA\n"
    )
    path_name = "vectors.fa"
    stdout, output = ok(tmp_path, text.encode(), name=path_name, topology="circular")
    first, second, third = (c["values"] for c in output["creates"])
    assert (first["description"], first["topology"]) == ("pUC19 cloning vector", "circular")
    assert (second["description"], second["topology"]) == ("a fragment", "linear")
    assert third["topology"] == "circular"
    lines = stdout.splitlines()
    assert "Record 1 (DNA-000001): 4 bp, circular, as its header says" in lines
    assert "Record 2 (DNA-000002): 4 bp, linear, as its header says" in lines
    assert "Record 3 (seq3): 4 bp, circular, as the topology param for FASTA records says" in (
        lines
    )


def test_a_genbank_records_topology_is_said_to_be_its_locus_lines(tmp_path: Path) -> None:
    record = (Path(__file__).parent / "data" / "plasmids" / "L09137.2.gb").read_bytes()
    stdout, _ = ok(tmp_path, record, name="pUC19.gb", topology="linear")
    assert "Record 1 (SYNPUC19CV): 2,686 bp, circular, as its LOCUS line says" in (
        stdout.splitlines()
    )

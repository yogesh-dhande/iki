"""Annotating real vectors from the plasmid plugin's library, run as a worker runs it (an
input.json in, an output.json out), and checked against the INSDC records in
``data/plasmids/``: each plasmid's length is the one its LOCUS line gives, each part lands
where the record's own feature table says, every resistance and selection gene the
annotation finds is a whole intact reading frame in the plasmid's own bases, and the
published coordinates of pUC19's and pBR322's parts come out.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "plugins" / "plasmid" / "scripts" / "plasmids.py"
RECORDS = Path(__file__).resolve().parent / "data" / "plasmids"

COMPLEMENT = str.maketrans("ACGT", "TGCA")
STOPS = frozenset({"TAA", "TAG", "TGA"})

#: Each vector, by the accession of its record, with the length its publication gives.
VECTORS = {
    "pUC19": ("L09137.2", 2686),
    "pBR322": ("J01749.1", 4361),
    "pRS416": ("U03450.1", 4898),
    "pcDNA3.1(+)": ("MN996867.1", 5428),
    "pLenti6-V5-Puro": ("LT009457.1", 8835),
    "pET-28a-ZaTdT12": ("OL989372.1", 6835),
}


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def read(accession: str) -> tuple[str, int, list[tuple[str, str, str]]]:
    """A record's bases, the length its LOCUS line gives, and its own features: each a
    key, a location and its qualifiers."""
    text = (RECORDS / f"{accession}.gb").read_text(encoding="utf-8")
    locus = re.match(r"LOCUS\s+\S+\s+(\d+) bp", text)
    assert locus is not None, f"{accession} has no LOCUS line"
    bases, reading = [], False
    for line in text.splitlines():
        if line.startswith("ORIGIN"):
            reading = True
        elif line.startswith("//"):
            break
        elif reading:
            bases.append(re.sub(r"[^A-Za-z]", "", line))
    features: list[tuple[str, str, str]] = []
    key: str | None = None
    location = ""
    qualifiers: list[str] = []
    in_features = False
    for line in text.splitlines():
        if line.startswith("FEATURES"):
            in_features = True
            continue
        if line.startswith("ORIGIN"):
            break
        if not in_features:
            continue
        start = re.match(r"^ {5}(\S+)\s+(\S.*)$", line)
        if start:
            if key:
                features.append((key, location, " ".join(qualifiers)))
            key, location, qualifiers = start.group(1), start.group(2).strip(), []
        elif line.startswith(" " * 21) and key:
            qualifiers.append(line.strip())
    if key:
        features.append((key, location, " ".join(qualifiers)))
    return "".join(bases).upper(), int(locus.group(1)), features


def annotate(tmp_path: Path, bases: str, *, circular: bool = True) -> dict[str, Any]:
    """What ``annotate_plasmid`` writes for a plasmid of these bases."""
    plasmid = {
        "id": "00000000-0000-0000-0000-000000000001",
        "hrid": "PLS-000001",
        "values": {},
        "included": {
            "sequence": [
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "hrid": "DNA-000001",
                    "values": {"bases": bases, "topology": "circular" if circular else "linear"},
                }
            ]
        },
    }
    (tmp_path / "input.json").write_text(
        json.dumps({"inputs": {"plasmid": [plasmid]}, "params": {"min_identity": 0.9}})
    )
    result = subprocess.run(  # noqa: S603 - our own script
        # Without -I, so the script imports its siblings by bare name as a run does.
        [sys.executable, str(SCRIPT), "annotate"],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    values: dict[str, Any] = output["updates"]["plasmid"][0]["values"]
    return values


def found(values: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Its features by the part's name, since a part can be found more than once."""
    out: dict[str, list[dict[str, Any]]] = {}
    for feature in values["annotated_features"]:
        out.setdefault(feature["name"], []).append(feature)
    return out


def at(bases: str, feature: dict[str, Any]) -> str:
    """The bases a feature covers, 5′ to 3′ along its own strand, round the origin where
    its end comes before its start."""
    doubled = bases + bases
    span = (
        feature["end"] - feature["start"] + 1
        if feature["end"] >= feature["start"]
        else len(bases) - feature["start"] + 1 + feature["end"]
    )
    got = doubled[feature["start"] - 1 : feature["start"] - 1 + span]
    return reverse_complement(got) if feature["strand"] == "reverse" else got


def located(location: str) -> tuple[int, int, str]:
    """An INSDC location of one span, as a feature's start, end and strand."""
    reverse = location.startswith("complement(")
    inner = location.removeprefix("complement(").removesuffix(")") if reverse else location
    start, _, end = inner.partition("..")
    return int(start), int(end or start), "reverse" if reverse else "forward"


@pytest.fixture(scope="module")
def annotations(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Each vector annotated once, since every test here reads the same annotations."""
    out = {}
    for name, (accession, _) in VECTORS.items():
        bases, _, _ = read(accession)
        out[name] = annotate(tmp_path_factory.mktemp(accession), bases)
    return out


@pytest.mark.parametrize("vector", VECTORS)
def test_each_record_is_the_length_its_publication_gives(vector: str) -> None:
    accession, published = VECTORS[vector]
    bases, locus, _ = read(accession)
    assert len(bases) == locus == published


#: Parts the annotation finds whose place that plasmid's own record also gives, so the
#: two can be compared. The ones from another record are the real check: pBR322's
#: beta-lactamase and Rop are found from parts taken out of pLenti6 and pET-28a.
AGAINST_THE_RECORD = [
    ("pBR322", "AmpR", "complement(3293..4153)", "LT009457.1"),
    ("pBR322", "rop", "1915..2106", "OL989372.1"),
    ("pBR322", "TetR", "86..1276", "J01749.1"),
    ("pcDNA3.1(+)", "NeoR/KanR", "2136..2930", "MN996867.1"),
    ("pLenti6-V5-Puro", "3' LTR", "17..251", "LT009457.1"),
    ("pLenti6-V5-Puro", "5' LTR", "3579..3907", "LT009457.1"),
    ("pLenti6-V5-Puro", "CMV promoter", "5304..5820", "LT009457.1"),
    ("pLenti6-V5-Puro", "PuroR", "8159..8761", "LT009457.1"),
    ("pLenti6-V5-Puro", "CmR", "6170..6829", "LT009457.1"),
    ("pLenti6-V5-Puro", "ccdB", "7171..7476", "LT009457.1"),
    ("pET-28a-ZaTdT12", "KanR", "complement(560..1375)", "OL989372.1"),
    ("pET-28a-ZaTdT12", "ori", "1497..2085", "OL989372.1"),
    ("pET-28a-ZaTdT12", "f1 ori", "12..467", "OL989372.1"),
    ("pET-28a-ZaTdT12", "T7 promoter", "4984..5002", "OL989372.1"),
    ("pET-28a-ZaTdT12", "T7 terminator", "6763..6810", "OL989372.1"),
    ("pET-28a-ZaTdT12", "lacI", "complement(3515..4597)", "OL989372.1"),
]


@pytest.mark.parametrize(("vector", "part", "location", "from_record"), AGAINST_THE_RECORD)
def test_a_part_lands_where_the_records_own_feature_table_says(
    annotations: dict[str, dict[str, Any]], vector: str, part: str, location: str, from_record: str
) -> None:
    hits = found(annotations[vector])[part]
    start, end, strand = located(location)
    assert (start, end, strand) in [(h["start"], h["end"], h["strand"]) for h in hits], (
        f"{part} (from {from_record}) in {vector}: {hits}"
    )


def test_the_two_sv40_origins_of_a_lentiviral_vector_are_both_found(
    annotations: dict[str, dict[str, Any]],
) -> None:
    # Its own record annotates rep_origin twice, at 533..608 and 8007..8082: the second
    # lies inside the SV40 early promoter, so parts that merely overlap both stay.
    hits = found(annotations["pLenti6-V5-Puro"])["SV40 ori"]
    assert sorted((h["start"], h["end"]) for h in hits) == [(533, 608), (8007, 8082)]
    promoter = found(annotations["pLenti6-V5-Puro"])["SV40 early promoter"][0]
    assert (promoter["start"], promoter["end"]) == (7768, 8050)


def test_only_one_of_gateways_two_att_sites_is_annotated_at_each_place(
    annotations: dict[str, dict[str, Any]],
) -> None:
    # attR1 and attR2 are 95% identical, so each finds the other's place; only the better
    # match is kept, and each site is annotated once.
    hits = found(annotations["pLenti6-V5-Puro"])
    assert [(h["start"], h["end"], h["strand"]) for h in hits["attR1"]] == [(5937, 6061, "reverse")]
    assert [(h["start"], h["end"], h["strand"]) for h in hits["attR2"]] == [(7517, 7641, "forward")]


#: pUC19's and pBR322's parts at the coordinates their published maps give. pUC19's own
#: record has no feature table, so these stand in for it.
PUBLISHED = [
    ("pUC19", "ori", 867, 1455, "reverse"),
    ("pUC19", "AmpR", 1626, 2486, "reverse"),
    ("pUC19", "lac promoter", 508, 591, "reverse"),
    ("pBR322", "ori", 2534, 3122, "reverse"),
    ("pRS416", "URA3", 417, 1220, "forward"),
    ("pRS416", "CEN6", 4325, 4443, "forward"),
    ("pRS416", "ARSH4", 4456, 4829, "forward"),
]


@pytest.mark.parametrize(("vector", "part", "start", "end", "strand"), PUBLISHED)
def test_published_coordinates_come_out(
    annotations: dict[str, dict[str, Any]],
    vector: str,
    part: str,
    start: int,
    end: int,
    strand: str,
) -> None:
    hits = found(annotations[vector])[part]
    assert [(h["start"], h["end"], h["strand"]) for h in hits] == [(start, end, strand)]


@pytest.mark.parametrize("vector", VECTORS)
def test_every_marker_found_is_a_whole_intact_reading_frame(
    annotations: dict[str, dict[str, Any]], vector: str
) -> None:
    """Each resistance or selection gene the annotation finds must read as a gene in the
    plasmid's own bases: a whole number of codons, from ATG to a stop, with no stop
    inside it. A hit a base or a codon out of place would fail this, whatever its
    identity, and no record's feature table is consulted for it."""
    bases, _, _ = read(VECTORS[vector][0])
    genes = [
        feature
        for feature in annotations[vector]["annotated_features"]
        if feature["qualifiers"]["role"] in ("resistance", "selection")
    ]
    assert genes, f"{vector} has no resistance or selection gene"
    for gene in genes:
        got = at(bases, gene)
        where = f"{gene['name']} at {gene['start']}..{gene['end']} in {vector}"
        assert len(got) % 3 == 0, f"{where}: {len(got)} bases isn't whole codons"
        assert got[:3] == "ATG", f"{where}: starts {got[:3]}"
        assert got[-3:] in STOPS, f"{where}: ends {got[-3:]}"
        inside = [got[i : i + 3] for i in range(0, len(got) - 3, 3)]
        assert not [c for c in inside if c in STOPS], f"{where}: a stop codon inside it"


#: What each vector's parts say about it: how it's selected, where it replicates, and its
#: promoters, as the parts found imply them.
SAYS = {
    "pUC19": {
        "bacterial_resistance": ["ampicillin"],
        "selection_markers": [],
        "replication_origins": ["pMB1/pUC"],
        "promoters": ["lac"],
    },
    "pBR322": {
        "bacterial_resistance": ["tetracycline", "ampicillin"],
        "selection_markers": [],
        "replication_origins": ["pMB1/pUC"],
        "promoters": [],
    },
    "pRS416": {
        "bacterial_resistance": ["ampicillin"],
        "selection_markers": ["ura3"],
        "replication_origins": ["f1", "pMB1/pUC", "CEN6", "ARSH4"],
        "promoters": ["T7", "lac"],
    },
    "pcDNA3.1(+)": {
        "bacterial_resistance": ["kanamycin", "ampicillin"],
        "selection_markers": ["neomycin_g418"],
        "replication_origins": ["f1", "SV40", "pMB1/pUC"],
        "promoters": ["CMV", "T7", "SV40 early", "lac"],
    },
    "pLenti6-V5-Puro": {
        "bacterial_resistance": ["ampicillin", "chloramphenicol"],
        "selection_markers": ["puromycin"],
        "replication_origins": ["SV40", "f1", "pMB1/pUC"],
        "promoters": ["T7", "lac", "CMV", "SV40 early"],
    },
    "pET-28a-ZaTdT12": {
        "bacterial_resistance": ["kanamycin"],
        "selection_markers": [],
        "replication_origins": ["f1", "pMB1/pUC"],
        "promoters": ["lacI", "T7"],
    },
}


@pytest.mark.parametrize("vector", VECTORS)
def test_what_the_parts_found_say_about_each_vector(
    annotations: dict[str, dict[str, Any]], vector: str
) -> None:
    values = annotations[vector]
    assert {field: values[field] for field in SAYS[vector]} == SAYS[vector]
    assert values["length"] == VECTORS[vector][1]
    assert values["annotated_with"] == (
        "iki/plasmid features 1.0 (34 parts), at least 90% identity"
    )


def test_a_part_that_drifted_keeps_the_identity_it_matched_at(
    annotations: dict[str, dict[str, Any]],
) -> None:
    """Vector parts drift between vectors: pUC19's pMB1/pUC origin differs from the
    library's (pET-28a's) over its 589 bases, and pcDNA3.1(+)'s f1 origin differs from it
    over 456, while their beta-lactamases are the library's exactly. Each hit says how
    much of the part matched and where the part's bases are from, so a scientist can see
    which parts aren't the library's."""
    puc = found(annotations["pUC19"])
    assert puc["ori"][0]["qualifiers"]["identity"] == "99.8%"
    assert puc["AmpR"][0]["qualifiers"]["identity"] == "100.0%"
    assert puc["lac promoter"][0]["qualifiers"]["identity"] == "100.0%"
    assert puc["AmpR"][0]["qualifiers"]["source"] == (
        "INSDC LT009457.1 1412..2272 (Lentiviral vector pLenti6-V5-Puro)"
    )
    pcdna = found(annotations["pcDNA3.1(+)"])
    assert pcdna["f1 ori"][0]["qualifiers"]["identity"] == "94.1%"
    assert pcdna["ori"][0]["qualifiers"]["identity"] == "99.3%"


def test_a_linear_sequence_is_annotated_without_wrapping_its_origin(tmp_path: Path) -> None:
    """pUC19 cut open at its origin: the parts that don't cross the cut are still found,
    and none is annotated across the ends, since a linear molecule has none."""
    bases, _, _ = read("L09137.2")
    values = annotate(tmp_path, bases, circular=False)
    hits = found(values)
    assert sorted(hits) == ["AmpR", "lac promoter", "ori"]
    assert all(f["end"] > f["start"] for f in values["annotated_features"])

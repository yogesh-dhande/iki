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


def annotated(tmp_path: Path, bases: str, *, circular: bool = True) -> dict[str, Any]:
    """``annotate`` in a directory of its own, so one test can annotate several times."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return annotate(tmp_path, bases, circular=circular)


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
    # The two shortest parts of the library, each at the place LT009457.1's own feature
    # table gives it in the very record the part was taken from: `misc_feature 1..16
    # /note="HIV-1 cPPT"` and `regulatory 3454..3470 /note="T3 promoter"`. Both are
    # shorter than a seed, so neither can be placed by the seed index.
    ("pLenti6-V5-Puro", "cPPT", 1, 16, "forward"),
    ("pLenti6-V5-Puro", "T3 promoter", 3454, 3470, "forward"),
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
        "replication_origins": ["f1", "pMB1/pUC", "ARSH4"],
        "promoters": ["T7", "T3", "lac"],
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
        "promoters": ["T7", "lac", "T3", "CMV", "SV40 early"],
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


def _every(bases: str, start: int, end: int, step: int) -> str:
    """``bases`` with every ``step``-th base from ``start`` to ``end`` (from 1) changed to
    the next of A, C, G and T, so no exact stretch longer than ``step - 1`` is left."""
    change = {"A": "C", "C": "G", "G": "T", "T": "A"}
    out = list(bases)
    for i in range(start - 1 + step // 2, end, step):
        out[i] = change[out[i]]
    return "".join(out)


def test_a_part_without_an_exact_seed_is_found_where_it_aligns(tmp_path: Path) -> None:
    """A seed is an exact 18 bases, so a part with a mismatch in every 18 has none: pUC19's
    origin with every 15th base changed is 93% identical to the library's, above the 0.9
    the run asks, though its longest exact stretch is 14 bases. It's found, at that
    identity, as is a second copy of it; with every 8th base changed (87%) it isn't."""
    bases, _, _ = read("L09137.2")
    (ori,) = found(annotated(tmp_path / "pUC19", bases))["ori"]
    start, end = ori["start"], ori["end"]
    assert ori["strand"] == "reverse" and end > start
    every15 = _every(bases, start, end, 15)
    hits = found(annotated(tmp_path / "every-15th", every15))
    assert [(h["start"], h["end"]) for h in hits["ori"]] == [(start, end)]
    identity = float(hits["ori"][0]["qualifiers"]["identity"].rstrip("%"))
    assert 92.5 < identity < 94.0
    assert sorted(hits) == ["AmpR", "lac promoter", "ori"]
    # Twice, the second copy after the first: each is a hit.
    twice = every15 + every15[start - 1 : end]
    copies = found(annotated(tmp_path / "twice", twice))["ori"]
    assert sorted((h["start"], h["end"]) for h in copies) == [
        (start, end),
        (len(bases) + 1, len(bases) + end - start + 1),
    ]
    every8 = _every(bases, start, end, 8)
    assert "ori" not in found(annotated(tmp_path / "every-8th", every8))


def test_a_linear_sequence_is_annotated_without_wrapping_its_origin(tmp_path: Path) -> None:
    """pUC19 cut open at its origin: the parts that don't cross the cut are still found,
    and none is annotated across the ends, since a linear molecule has none."""
    bases, _, _ = read("L09137.2")
    values = annotate(tmp_path, bases, circular=False)
    hits = found(values)
    assert sorted(hits) == ["AmpR", "lac promoter", "ori"]
    assert all(f["end"] > f["start"] for f in values["annotated_features"])


def test_a_part_across_the_cut_is_read_on_the_circle_and_not_on_the_linear_molecule(
    tmp_path: Path,
) -> None:
    """pUC19 linearised with ScaI, which has one site in it, 2,179 bases in, inside the
    beta-lactamase gene -- the cut a lab makes to open pUC19 away from its polylinker.

    Read as a circle from that cut, the AmpR gene is annotated across the origin, from
    2,133 round to 307. Read as the linear molecule ScaI leaves, it is in two pieces and
    neither is the gene, so it isn't annotated at all: a linear molecule's two ends are
    ends, and nothing is read across them. The two parts that don't meet the cut -- the
    origin and the lac promoter -- are found at the same bases either way.
    """
    puc, _, _ = read("L09137.2")
    # ScaI's site AGT^ACT, cut blunt after base 2179, which lies in AmpR (1626..2486).
    assert puc[2176:2182] == "AGTACT"
    assert 1626 < 2179 < 2486
    opened = puc[2179:] + puc[:2179]
    assert len(opened) == 2686

    circle = found(annotated(tmp_path / "circle", opened, circular=True))
    assert sorted(circle) == ["AmpR", "lac promoter", "ori"]
    # Its end comes before its start, which is how a part across the origin is written.
    (amp,) = circle["AmpR"]
    assert (amp["start"], amp["end"]) == (2133, 307)

    line = found(annotated(tmp_path / "line", opened, circular=False))
    assert sorted(line) == ["lac promoter", "ori"]
    for part in ("lac promoter", "ori"):
        assert (line[part][0]["start"], line[part][0]["end"]) == (
            circle[part][0]["start"],
            circle[part][0]["end"],
        )


def test_a_linear_plasmids_record_is_annotated_as_the_linear_molecule_it_is(
    tmp_path: Path,
) -> None:
    """pGKL1 (INSDC X01095.1), the 8,874 bp linear killer plasmid of Kluyveromyces
    lactis, whose LOCUS line says `linear` and whose own feature table gives the terminal
    inverted repeats at each end (1..202 and 8673..8874) that make it a linear molecule
    rather than a circle (Hishinuma et al., Nucleic Acids Res. 1984).

    None of the library's parts is in it -- it is a yeast killer plasmid, not a cloning
    vector -- and the run says so, with its length and its topology, which is the entry's
    and which the run fills where it is empty.
    """
    bases, locus, features = read("X01095.1")
    assert len(bases) == locus == 8874
    repeats = [location for key, location, _ in features if key == "repeat_region"]
    assert repeats == ["1..202", "8673..8874"]
    values = annotate(tmp_path, bases, circular=False)
    assert values["annotated_features"] == []
    assert values["length"] == 8874
    assert values["topology"] == "linear"
    assert values["replication_origins"] == []


def test_a_run_leaves_the_topology_its_entry_has(tmp_path: Path) -> None:
    """A plasmid's topology is its registry entry's, the same for every project, so a run
    fills it where it is empty and never writes over it -- as its length is handled."""
    bases, _, _ = read("L09137.2")
    plasmid = {
        "id": "00000000-0000-0000-0000-000000000001",
        "hrid": "PLS-000001",
        "values": {"length": 2686, "topology": "circular"},
        "included": {
            "sequence": [
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "hrid": "DNA-000001",
                    "values": {"bases": bases, "topology": "circular"},
                }
            ]
        },
    }
    (tmp_path / "input.json").write_text(
        json.dumps({"inputs": {"plasmid": [plasmid]}, "params": {}})
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT), "annotate"],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    written = json.loads((tmp_path / "out.json").read_text())["updates"]["plasmid"][0]["values"]
    assert "topology" not in written
    assert "length" not in written


# --- what a part found says, and what it doesn't -------------------------------------


def test_a_centromere_is_not_an_origin_of_replication(
    annotations: dict[str, dict[str, Any]],
) -> None:
    """A pRS41x shuttle vector carries both CEN6 and ARSH4, and they do different things:
    CEN6 is the centromere of Saccharomyces cerevisiae chromosome VI, which segregates
    the plasmid at mitosis, and ARSH4 is the autonomously replicating sequence it
    replicates from. Sikorski and Hieter 1989 (Genetics 122:19-27) built the pRS vectors
    from the two, and INSDC's feature table keeps `centromere` and `rep_origin` apart. So
    CEN6 is annotated, as a centromere, and the plasmid's origins are ARSH4's, not its."""
    hits = found(annotations["pRS416"])
    (cen6,) = hits["CEN6"]
    (arsh4,) = hits["ARSH4"]
    assert (cen6["type"], cen6["qualifiers"]["role"]) == ("centromere", "segregation")
    assert (arsh4["type"], arsh4["qualifiers"]["role"]) == ("rep_origin", "origin")
    assert "CEN6" not in annotations["pRS416"]["replication_origins"]
    assert "ARSH4" in annotations["pRS416"]["replication_origins"]


def test_a_marker_whose_reading_frame_is_broken_selects_nothing(tmp_path: Path) -> None:
    """A ura3 nonsense allele is the classic yeast auxotroph: it is all but identical to
    URA3 and selects nothing, because its protein is never made. pRS416's URA3 is 804
    bases, 267 codons and a stop (SGD YEL021W, orotidine-5'-phosphate decarboxylase, 267
    aa). Its second codon is TCG, and one base makes that TAG: 803 of 804 bases still
    match, and the open frame is one residue long. The homology is still annotated, with
    its frame recorded as interrupted, and the plasmid is no longer said to be selected
    with ura3."""
    bases, _, _ = read("U03450.1")
    start, end, strand = located("417..1220")
    assert strand == "forward"
    gene = bases[start - 1 : end]
    assert len(gene) == 804 == 3 * (267 + 1)
    assert gene[:3] == "ATG" and gene[-3:] in STOPS

    whole = annotated(tmp_path / "whole", bases)
    assert "ura3" in whole["selection_markers"]
    assert found(whole)["URA3"][0]["qualifiers"]["reading_frame"] == "whole"

    assert gene[3:6] == "TCG"
    nonsense = gene[:4] + "A" + gene[5:]
    assert nonsense[3:6] == "TAG" in STOPS
    broken = bases[: start - 1] + nonsense + bases[end:]
    assert len(broken) == len(bases)
    assert sum(a != b for a, b in zip(broken, bases, strict=True)) == 1
    values = annotated(tmp_path / "broken", broken)
    (hit,) = found(values)["URA3"]
    # 801 of the part's 804 bases: the local alignment opens after the new stop codon
    # rather than paying for a mismatch three bases in, so the first codon is left out.
    assert hit["qualifiers"]["identity"] == "99.6%"
    assert hit["qualifiers"]["reading_frame"] == "interrupted"
    assert values["selection_markers"] == []
    # Everything else the vector says is unchanged.
    assert values["replication_origins"] == whole["replication_origins"]
    assert values["bacterial_resistance"] == whole["bacterial_resistance"]


def test_a_part_nested_in_a_part_of_another_kind_stays_wherever_the_origin_is(
    tmp_path: Path,
) -> None:
    """Only alternative readings of the same thing compete: a 19-base T7 promoter inside
    a 25-base lac operator means something of its own, and both stay. Which survived used
    to depend on where the record happened to begin, because a hit crossing the origin was
    ranked by a negative length. pET-28a's own T7 promoter and lac operator lie next to
    each other, so the record is rotated until each in turn crosses the origin and the
    same parts come out."""
    bases, _, _ = read("OL989372.1")
    wanted = {"T7 promoter", "lac operator", "lacI promoter"}
    first = {
        name: len(hits)
        for name, hits in found(annotated(tmp_path / "plain", bases)).items()
        if name in wanted
    }
    assert first == {"T7 promoter": 1, "lac operator": 1, "lacI promoter": 1}
    for n, at_base in enumerate((4990, 5010, 5025, 2500)):
        rotated = bases[at_base:] + bases[:at_base]
        values = annotated(tmp_path / f"rotated{n}", rotated)
        again = {name: len(hits) for name, hits in found(values).items() if name in wanted}
        assert again == first, f"rotated by {at_base}"

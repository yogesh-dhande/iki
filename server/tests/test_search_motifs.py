"""Finding sequences by a motif (ADR 0175): IUPAC codes, a duplex's other strand, and a
circle's origin.

The record is pUC19 itself (GenBank L09137.2, 2,686 bp), whose one EcoRI site, GAATTC, is
at 396 (NEB's pUC19 map). The M13/pUC reverse sequencing primer, 5'-CAGGAAACAGCTATGAC-3'
(NEB #S1233S), binds the strand L09137.2 doesn't give: its bases are found only as their
reverse complement, from 465.
"""

from pathlib import Path
from typing import Any

import pytest
from api_client import Api, problem
from django.db import connection
from org_world import World, build_world
from test_api_entities import api_as, create

from iki_server.entities import search

pytestmark = pytest.mark.django_db

DATA = Path(__file__).parent / "data"
#: The M13/pUC reverse sequencing primer (NEB #S1233S).
M13_REVERSE = "CAGGAAACAGCTATGAC"
#: Human preproinsulin (UniProt P01308): its signal peptide, then the B chain, C peptide
#: and A chain.
INSULIN = (
    "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCGERGFFYTPKTRREAEDLQVGQVELGGGPGAGSLQPLALEGSLQ"
    "KRGIVEQCCTSICSLYQLENYCN"
)


def puc19() -> str:
    lines = (DATA / "L09137.2.fasta").read_text().splitlines()
    return "".join(line.strip() for line in lines if not line.startswith(">"))


def rotated(bases: str, at: int) -> str:
    """The circle read from ``at`` (counting from 0) on."""
    return bases[at:] + bases[:at]


@pytest.fixture
def world() -> World:
    return build_world()


def made(world: World, type_name: str, values: dict[str, Any]) -> dict[str, Any]:
    response = create(api_as(world), world.project, values, type_name)
    assert response.status_code == 201, response.content
    return response.json()  # type: ignore[no-any-return]


def find(world: World, type_name: str, field: str, motif: str) -> list[str]:
    """The names of what a search for the motif finds, in the order they were made."""
    body = {"type": type_name, "where": {"field": field, "op": "motif", "value": motif}}
    response = api_as(world, "viewer").post("/entities/search", body)
    assert response.status_code == 200, response.content
    return [e["values"]["name"] for e in response.json()["items"]]


PLASMID = "acme/dna.Plasmid"


def test_a_motif_finds_a_site_by_its_iupac_codes(world: World) -> None:
    bases = puc19()
    assert len(bases) == 2686 and bases.find("GAATTC") == 395
    made(world, PLASMID, {"bases": bases, "name": "pUC19"})
    made(world, PLASMID, {"bases": bases.replace("GAATTC", "GAATTG"), "name": "no EcoRI"})
    assert find(world, PLASMID, "bases", "GAATTC") == ["pUC19"]
    # Y is C or T; the motif is read in either case, as pasted.
    assert find(world, PLASMID, "bases", "gaatt y") == ["pUC19"]
    assert find(world, PLASMID, "bases", "GAATTS") == ["pUC19", "no EcoRI"]
    # pUC19 has no GAATTA or GAATTT, but the strand it doesn't give has TAATTC at 2,127:
    # its other strand, read 5' to 3', has GAATTA there.
    assert find(world, PLASMID, "bases", "GAATTW") == ["pUC19", "no EcoRI"]


def test_a_base_the_record_doesnt_know_isnt_the_base_asked_for(world: World) -> None:
    """N in a record might be any base, so it's found by N (or a code allowing every base),
    not by GAATTC: a site that might not be there isn't reported as one."""
    made(world, PLASMID, {"bases": puc19().replace("GAATTC", "GAANTC"), "name": "read with an N"})
    assert find(world, PLASMID, "bases", "GAATTC") == []
    assert find(world, PLASMID, "bases", "GAANTC") == ["read with an N"]


def test_a_duplex_is_found_by_either_strand_and_a_single_strand_only_by_its_own(
    world: World,
) -> None:
    bases = puc19()
    assert M13_REVERSE not in bases
    made(world, PLASMID, {"bases": bases, "name": "pUC19"})
    made(world, PLASMID, {"bases": bases, "strandedness": "single", "name": "one strand"})
    assert find(world, PLASMID, "bases", M13_REVERSE) == ["pUC19"]
    # EcoRI's site is its own reverse complement: both have it.
    assert find(world, PLASMID, "bases", "GAATTC") == ["pUC19", "one strand"]


def test_a_circle_is_found_across_its_origin_and_a_line_only_between_its_ends(
    world: World,
) -> None:
    """pUC19 read from inside its EcoRI site, so GAA ends the record and TTC starts it."""
    bases = rotated(puc19(), 398)
    assert bases.startswith("TTC") and bases.endswith("GAA") and "GAATTC" not in bases
    made(world, PLASMID, {"bases": bases, "topology": "circular", "name": "circle"})
    made(world, PLASMID, {"bases": bases, "topology": "linear", "name": "line"})
    assert find(world, PLASMID, "bases", "GAATTC") == ["circle"]
    assert find(world, PLASMID, "bases", "GAATTY") == ["circle"]


def test_a_duplex_is_found_by_its_other_strand_across_its_origin(world: World) -> None:
    """pUC19 read from the middle of the primer's site, which is on the other strand."""
    across = rotated(puc19(), 470)
    assert M13_REVERSE not in across
    made(world, PLASMID, {"bases": across, "name": "circle"})
    made(world, PLASMID, {"bases": across, "topology": "linear", "name": "line"})
    assert find(world, PLASMID, "bases", M13_REVERSE) == ["circle"]


def test_a_motif_longer_than_a_circle_isnt_found_going_round_it_twice(world: World) -> None:
    made(world, PLASMID, {"bases": "GAATTCAG", "topology": "circular", "name": "tiny"})
    assert find(world, PLASMID, "bases", "TTCAGGAA") == ["tiny"]
    assert find(world, PLASMID, "bases", "GAATTCAGGAA") == []


def test_a_proteins_motif_reads_its_ambiguity_codes(world: World) -> None:
    """J is I or L (IUPAC-IUBMB): LVEALYLV, in insulin's B chain, is JVEAJYJV."""
    made(world, "acme/protein.Protein", {"sequence": INSULIN, "name": "insulin"})
    made(world, "acme/protein.Protein", {"sequence": "MKVL", "name": "short"})
    assert find(world, "acme/protein.Protein", "sequence", "LVEALYLV") == ["insulin"]
    assert find(world, "acme/protein.Protein", "sequence", "JVEAJYJV") == ["insulin"]
    # Z is E or Q; X is any residue.
    assert find(world, "acme/protein.Protein", "sequence", "GZRGXXYT") == ["insulin"]
    assert find(world, "acme/protein.Protein", "sequence", "MKV") == ["short"]


@pytest.mark.parametrize(
    ("type_name", "field", "value", "pointer", "detail"),
    [
        (
            "acme/protein.Protein",
            "host",
            "GAATTC",
            "#/where/op",
            "`host` takes `is_set`, `eq`, `ne`, `in`, `contains` or `starts_with`, not `motif`.",
        ),
        (
            PLASMID,
            "bases",
            "GAAUUC",
            "#/where/value",
            "has U, and U isn't a DNA base: DNA has T.",
        ),
        (
            PLASMID,
            "bases",
            "GAATT[CT]",
            "#/where/value",
            "has `[`, and `[` isn't an IUPAC code for DNA: give A, B, C, D, G, H, K, M, N, R, "
            "S, T, V, W, Y.",
        ),
        (
            PLASMID,
            "bases",
            7,
            "#/where/value",
            "must be bases or residues in IUPAC codes, such as `GAATTC`.",
        ),
        (PLASMID, "bases", "A" * 201, "#/where/value", "must be at most 200 codes long."),
    ],
)
def test_a_motif_is_a_sequences_and_its_alphabets_codes(
    world: World, type_name: str, field: str, value: Any, pointer: str, detail: str
) -> None:
    body = {"type": type_name, "where": {"field": field, "op": "motif", "value": value}}
    response = api_as(world, "viewer").post("/entities/search", body)
    assert problem(response, 422, "validation_failed")["errors"] == [
        {"pointer": pointer, "detail": detail}
    ]


@pytest.mark.parametrize("motif", ["MKWVTF", "MKWVTJ"])
def test_a_motif_on_an_indexed_sequence_uses_its_trigram_index(world: World, motif: str) -> None:
    """As text without ambiguity, and as a regular expression with it (ADR 0037)."""
    wanted = search.Search(
        type="acme/protein.Protein",
        where={"field": "signal_peptide", "op": "motif", "value": motif},
    )
    found = search.queryset(world.users["viewer"], wanted)
    with connection.cursor() as cursor:
        for scan in ("seqscan", "indexscan", "indexonlyscan"):
            cursor.execute(f"SET LOCAL enable_{scan} = off")
        sql, params = found.query.sql_with_params()
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    # Index names are cut to fit: the field's, then `trgm`.
    assert "Bitmap Index Scan on signal_pep_trgm_" in plan, plan


def test_a_type_says_which_of_its_fields_are_sequences(world: World) -> None:
    api: Api = api_as(world, "viewer")
    types = {t["name"]: t for t in api.get("/entity-types").json()}
    fields = {f["name"]: f for f in types[PLASMID]["fields"]}
    assert fields["bases"]["format"] == "dna_sequence"
    assert fields["topology"]["format"] is None

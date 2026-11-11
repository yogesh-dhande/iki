"""Searching entities: typed conditions, subtypes, sorting and keyset pages, and limits."""

from typing import Any

import pytest
from api_client import Api, problem
from django.db import connection
from org_world import World, build_world
from test_api_entities import api_as, create

from iki_server.entities import search
from iki_server.org.models import Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def made(api: Api, project: Project, values: dict[str, Any], type_name: str) -> dict[str, Any]:
    response = create(api, project, values, type_name)
    assert response.status_code == 201, response.content
    return response.json()  # type: ignore[no-any-return]


@pytest.fixture
def lab(world: World) -> dict[str, dict[str, Any]]:
    """A few DNA sequences and proteins, by name."""
    api = api_as(world)
    out = {
        "puc19": made(
            api, world.project, {"bases": "ATGC", "name": "pUC19 kinase"}, "dna.DNASequence"
        ),
        "lambda": made(
            api, world.project, {"bases": "GGCC", "topology": "circular"}, "dna.DNASequence"
        ),
    }
    for name, values in {
        "abl1": {
            "sequence": "MKVL",
            "organism": "Homo sapiens",
            "temperature_optimum": 37,
            "uniprot_ids": ["P00519"],
            "summary": "A tyrosine kinase; the active site binds ATP.",
        },
        "src": {
            "sequence": "MSTL",
            "organism": "Homo sapiens",
            "temperature_optimum": 30,
            "encoded_by": out["puc19"]["id"],
        },
        "lacz": {
            "sequence": "MTMI",
            "organism": "Escherichia coli",
            "temperature_optimum": 25,
            "summary": "A galactosidase fragment.",
        },
        "mystery": {"sequence": "MAAA"},
    }.items():
        out[name] = made(api, world.project, {"name": name, **values}, "protein.Protein")
    return out


def find(world: World, body: dict[str, Any], principal: str = "viewer") -> list[str]:
    """The names (or HRIDs) of what the search finds, in order, across every page."""
    api = api_as(world, principal)
    names: list[str] = []
    cursor = None
    while True:
        response = api.post("/entities/search", {**body, "limit": 2, "cursor": cursor})
        assert response.status_code == 200, response.content
        page = response.json()
        names += [e["values"].get("name") or e["hrid"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            return names


def errors(world: World, body: dict[str, Any]) -> list[dict[str, str]]:
    response = api_as(world, "viewer").post("/entities/search", body)
    return problem(response, 422, "validation_failed")["errors"]  # type: ignore[no-any-return]


# --- Conditions ---------------------------------------------------------------------------------


def test_a_parents_fields_find_its_subtypes(world: World, lab: dict[str, Any]) -> None:
    where: dict[str, Any] = {"field": "name", "op": "contains", "value": "KINASE"}
    # dna.Sequence is abstract: its name is shared by DNA sequences and proteins.
    assert find(world, {"type": "dna.Sequence", "where": where}) == ["pUC19 kinase"]
    where = {"field": "name", "op": "in", "value": ["abl1", "pUC19 kinase"]}
    assert find(world, {"type": "dna.Sequence", "where": where}) == ["pUC19 kinase", "abl1"]
    assert find(world, {"type": "dna.Sequence", "exact_type": True, "where": where}) == []
    assert find(world, {"type": "protein.Protein", "where": where}) == ["abl1"]


def test_conditions_combine(world: World, lab: dict[str, Any]) -> None:
    human = {"field": "organism", "op": "eq", "value": "Homo sapiens"}
    warm = {"field": "temperature_optimum", "op": "gt", "value": 35}
    body = {"type": "protein.Protein"}
    assert find(world, {**body, "where": {"all": [human, warm]}}) == ["abl1"]
    assert find(
        world, {**body, "where": {"any": [warm, {"field": "name", "op": "eq", "value": "lacz"}]}}
    ) == ["abl1", "lacz"]
    # `ne` and `not` include entities without a value.
    assert find(world, {**body, "where": {"not": human}}) == ["lacz", "mystery"]
    assert find(
        world, {**body, "where": {"field": "organism", "op": "ne", "value": "Homo sapiens"}}
    ) == ["lacz", "mystery"]
    assert find(
        world, {**body, "where": {"field": "organism", "op": "is_set", "value": False}}
    ) == ["mystery"]


def test_quantities_compare_in_any_unit_of_their_kind(world: World, lab: dict[str, Any]) -> None:
    body = {"type": "protein.Protein", "sort": {"field": "temperature_optimum"}}
    # 300 K is 26.85 °C; 86 °F is 30 °C.
    over = {"field": "temperature_optimum", "op": "gte", "value": {"value": 300, "unit": "K"}}
    assert find(world, {**body, "where": over}) == ["src", "abl1"]
    exactly = {"field": "temperature_optimum", "op": "eq", "value": {"value": 86, "unit": "[degF]"}}
    assert find(world, {**body, "where": exactly}) == ["src"]


def test_text_and_strings(world: World, lab: dict[str, Any]) -> None:
    body = {"type": "protein.Protein"}
    assert find(
        world, {**body, "where": {"field": "organism", "op": "starts_with", "value": "homo"}}
    ) == ["abl1", "src"]
    # Words, as people type them into a search engine.
    assert find(
        world, {**body, "where": {"field": "summary", "op": "matches", "value": "kinase ATP"}}
    ) == ["abl1"]
    assert find(
        world, {**body, "where": {"field": "summary", "op": "matches", "value": '"active site"'}}
    ) == ["abl1"]
    assert find(
        world, {**body, "where": {"field": "summary", "op": "matches", "value": "-kinase"}}
    ) == ["lacz"]
    # Wildcards are just characters.
    assert find(world, {**body, "where": {"field": "name", "op": "contains", "value": "%"}}) == []
    assert find(world, {**body, "q": "_"}) == []


def test_references_lists_and_enums(world: World, lab: dict[str, Any]) -> None:
    body = {"type": "protein.Protein"}
    by_hrid = {"field": "encoded_by", "op": "eq", "value": lab["puc19"]["hrid"]}
    assert find(world, {**body, "where": by_hrid}) == ["src"]
    by_id = {"field": "encoded_by", "op": "in", "value": [lab["puc19"]["id"]]}
    assert find(world, {**body, "where": by_id}) == ["src"]
    assert find(
        world, {**body, "where": {"field": "uniprot_ids", "op": "has", "value": "P00519"}}
    ) == ["abl1"]
    assert find(
        world, {**body, "where": {"field": "uniprot_ids", "op": "is_set", "value": True}}
    ) == ["abl1"]
    circular = {"field": "topology", "op": "eq", "value": "circular"}
    assert find(world, {"type": "dna.DNASequence", "where": circular}) == [lab["lambda"]["hrid"]]


def test_lists_of_references_and_extension_fields(world: World, lab: dict[str, Any]) -> None:
    from iki_server.files.models import Blob, File

    blob = Blob.objects.create(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    pdb = File.objects.create(
        blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=world.project
    )
    api = api_as(world)
    values = {"structure": str(pdb.pk), "chains": [lab["abl1"]["id"]], "ranking": {"rank": 1}}
    one = made(api, world.project, {**values, "method": "xray"}, "protein.ProteinStructure")
    two = made(
        api, world.project, {"structure": str(pdb.pk), "method": "nmr"}, "protein.ProteinStructure"
    )
    body = {"type": "protein.ProteinStructure"}
    has = {"field": "chains", "op": "has", "value": lab["abl1"]["hrid"]}
    assert find(world, {**body, "where": has}) == [one["hrid"]]
    assert find(world, {**body, "where": {"field": "chains", "op": "is_set", "value": False}}) == [
        two["hrid"]
    ]
    # ranking is the structure_prediction plugin's: it's in another table.
    assert find(world, {**body, "where": {"field": "ranking", "op": "is_set", "value": True}}) == [
        one["hrid"]
    ]
    assert find(
        world, {**body, "where": {"field": "structure", "op": "eq", "value": str(pdb.pk)}}
    ) == [one["hrid"], two["hrid"]]
    assert find(
        world, {**body, "where": {"field": "method", "op": "in", "value": ["nmr", "cryo_em"]}}
    ) == [two["hrid"]]


def test_every_entity_has_its_own_columns(world: World, lab: dict[str, Any]) -> None:
    hrids = {"field": "hrid", "op": "in", "value": [lab["abl1"]["hrid"], lab["puc19"]["hrid"]]}
    assert find(world, {"where": hrids}) == ["pUC19 kinase", "abl1"]
    proteins = {"field": "type", "op": "eq", "value": "protein.Protein"}
    assert len(find(world, {"where": proteins})) == 4
    mine = {"field": "created_by", "op": "eq", "value": str(world.users["editor"].pk)}
    assert len(find(world, {"where": mine})) == 6
    since = {"field": "created_at", "op": "gte", "value": "2026-01-01T00:00:00+01:00"}
    assert len(find(world, {"where": since})) == 6
    before = {"field": "updated_at", "op": "lt", "value": "2026-01-01T00:00:00Z"}
    assert find(world, {"where": before}) == []
    assert find(world, {"where": {"field": "archived_at", "op": "is_set", "value": True}}) == []


def test_only_entities_you_can_read(world: World, lab: dict[str, Any]) -> None:
    assert find(world, {"type": "protein.Protein"}, "outsider") == []
    # Naming one you can't read reveals nothing about it.
    body = {
        "type": "protein.Protein",
        "where": {"field": "encoded_by", "op": "eq", "value": lab["puc19"]["hrid"]},
    }
    response = api_as(world, "outsider").post("/entities/search", body)
    assert problem(response, 422, "validation_failed")["errors"] == [
        {"pointer": "#/where/value", "detail": "refers to no entity you can read."}
    ]


# --- Mistakes -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"type": "protein.Protein", "where": {"field": "colour", "op": "eq", "value": "red"}},
            [{"pointer": "#/where/field", "detail": "protein.Protein has no field `colour`."}],
        ),
        (
            {"where": {"field": "name", "op": "eq", "value": "x"}},
            [
                {
                    "pointer": "#/where/field",
                    "detail": "every entity has no field `name`. Name a type to search its fields.",
                }
            ],
        ),
        (
            {
                "type": "protein.Protein",
                "where": {
                    "all": [
                        {"field": "organism", "op": "gt", "value": "x"},
                        {"field": "temperature_optimum", "op": "lt", "value": "warm"},
                        {
                            "field": "temperature_optimum",
                            "op": "lt",
                            "value": {"value": 1, "unit": "kg"},
                        },
                    ]
                },
            },
            [
                {
                    "pointer": "#/where/all/0/op",
                    "detail": "`organism` takes `is_set`, `eq`, `ne`, `in`, `contains` or "
                    "`starts_with`, not `gt`.",
                },
                {
                    "pointer": "#/where/all/1/value",
                    "detail": 'must be a number in Cel, or {"value": …, "unit": …}.',
                },
                {
                    "pointer": "#/where/all/2/value",
                    "detail": "has a unit Iki can't use here: `kg` and `Cel` measure different "
                    "things, so values can't convert.",
                },
            ],
        ),
        (
            {
                "type": "dna.DNASequence",
                "where": {"field": "topology", "op": "in", "value": ["round"]},
            },
            [{"pointer": "#/where/value/0", "detail": "must be one of `linear` or `circular`."}],
        ),
        (
            {
                "type": "protein.Protein",
                "where": {"field": "summary", "op": "contains", "value": "  "},
            },
            [{"pointer": "#/where/value", "detail": "must be some text, up to 200 characters."}],
        ),
        (
            {"type": "protein.Protein", "where": {"field": "name", "op": "eq"}},
            [{"pointer": "#/where/value", "detail": "is missing."}],
        ),
        (
            {"type": "protein.Protein", "where": {"all": []}},
            [{"pointer": "#/where/all", "detail": "must be a list of conditions."}],
        ),
        (
            {
                "type": "protein.Protein",
                "where": {"field": "name", "op": "eq", "value": "x", "all": []},
            },
            [{"pointer": "#/where", "detail": "has `all`, so it can't have anything else."}],
        ),
        (
            {"type": "protein.Protein", "sort": {"field": "uniprot_ids"}},
            [{"pointer": "#/sort", "detail": "`uniprot_ids` can't be sorted by."}],
        ),
        (
            {"type": "protein.Protein", "sort": {"field": "colour"}},
            [{"pointer": "#/sort", "detail": "There’s no field `colour` to sort by."}],
        ),
        (
            {"type": "protein.Prion"},
            [{"pointer": "#/type", "detail": "There’s no entity type protein.Prion."}],
        ),
    ],
)
def test_mistakes_point_at_the_condition(world: World, body: dict[str, Any], expected: Any) -> None:
    assert errors(world, body) == expected


def test_searches_have_bounds(world: World) -> None:
    leaf = {"field": "hrid", "op": "eq", "value": "PRT-000001"}
    many = {"any": [leaf] * (search.MAX_CONDITIONS + 5)}
    assert errors(world, {"where": many}) == [
        {
            "pointer": f"#/where/any/{search.MAX_CONDITIONS}",
            "detail": "is one condition too many: at most 50.",
        }
    ]
    deep: dict[str, Any] = leaf
    for _ in range(search.MAX_DEPTH):
        deep = {"not": deep}
    pointer = "#/where" + "/not" * search.MAX_DEPTH
    assert errors(world, {"where": deep}) == [
        {"pointer": pointer, "detail": "nests too deeply: at most 6 levels."}
    ]
    values = {"field": "hrid", "op": "in", "value": ["PRT-000001"] * 101}
    assert errors(world, {"where": values}) == [
        {"pointer": "#/where/value", "detail": "must be a list of 1 to 100 values."}
    ]


def test_searches_that_run_too_long_are_stopped(world: World) -> None:
    with pytest.raises(search.TooSlow), search.time_limit(10), connection.cursor() as cursor:
        cursor.execute("SELECT pg_sleep(1)")


def test_the_api_says_when_a_search_ran_too_long(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(*args: Any) -> Any:
        raise search.TooSlow

    monkeypatch.setattr(search, "page", slow)
    body = problem(api_as(world, "viewer").post("/entities/search", {}), 422, "search_too_slow")
    assert body["detail"].startswith("This search took too long.")


# --- Sorting and pages --------------------------------------------------------------------------


def test_sorted_by_a_field_with_unset_values_last(world: World, lab: dict[str, Any]) -> None:
    body: dict[str, Any] = {"type": "protein.Protein", "sort": {"field": "organism"}}
    # Ties (the two human proteins) keep the order they were made in, across pages.
    assert find(world, body) == ["lacz", "abl1", "src", "mystery"]
    body["sort"]["descending"] = True
    assert find(world, body) == ["abl1", "src", "lacz", "mystery"]
    by_age = {"type": "dna.Sequence", "sort": {"field": "created_at", "descending": True}}
    assert find(world, by_age) == [
        "mystery",
        "lacz",
        "src",
        "abl1",
        lab["lambda"]["hrid"],
        "pUC19 kinase",
    ]


def test_cursors_belong_to_their_search(world: World, lab: dict[str, Any]) -> None:
    api = api_as(world, "viewer")
    body = {"type": "protein.Protein", "sort": {"field": "organism"}, "limit": 1}
    cursor = api.post("/entities/search", body).json()["next_cursor"]
    other = {**body, "sort": {"field": "name"}, "cursor": cursor}
    assert problem(api.post("/entities/search", other), 422, "validation_failed")["errors"] == [
        {"pointer": "#/cursor", "detail": "This cursor is from a search sorted another way."}
    ]
    forged = {**body, "cursor": cursor[:-2] + "xx"}
    assert problem(api.post("/entities/search", forged), 422, "validation_failed")["errors"] == [
        {"pointer": "#/cursor", "detail": "Not a cursor from this API."}
    ]


# --- Indexes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("where", "index"),
    [
        (search.Search(type="protein.Protein", q="kin"), "iki_entity_hrid_trgm"),
        (
            search.Search(
                type="protein.Protein",
                where={"field": "organism", "op": "contains", "value": "sapi"},
            ),
            "organism_trgm_",
        ),
        (
            search.Search(
                type="protein.Protein",
                where={"field": "summary", "op": "matches", "value": "kinase"},
            ),
            "summary_fts_",
        ),
    ],
)
def test_text_searches_use_their_indexes(world: World, where: search.Search, index: str) -> None:
    found = search.queryset(world.users["viewer"], where)
    with connection.cursor() as cursor:
        # Tables this small would be scanned: leave the planner only index-driven bitmaps.
        for scan in ("seqscan", "indexscan", "indexonlyscan"):
            cursor.execute(f"SET LOCAL enable_{scan} = off")
        sql, params = found.query.sql_with_params()
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert index in plan, plan

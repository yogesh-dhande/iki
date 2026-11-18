"""Lists of rows (ADR 0137): each row's fields checked as a type's are, each role held to its
count and what it refers to, stored a column per row field, and every surface showing them."""

from typing import Any

import pytest
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as, create, patch
from test_graph import walk
from test_runs import protein, world

from iki_server.entities import states
from iki_server.graph.models import Edge

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # the fixture, used here too

BISPECIFIC = "acme/protein.Bispecific"


def dna(world: World, bases: str = "ACGT") -> Any:
    made = create(api_as(world), world.project, {"bases": bases})
    assert made.status_code == 201, made.content
    return made.json()


def chains(world: World) -> tuple[Any, Any, Any]:
    api = api_as(world)
    return protein(api, world.project, "hc1"), protein(api, world.project, "lc1"), dna(world)


def components(heavy: Any, light: Any, linker: Any | None = None) -> list[dict[str, Any]]:
    rows = [
        {"part": heavy["id"], "role": "heavy", "chain": "H"},
        {"part": heavy["id"], "role": "heavy", "chain": "H2", "copies": 1},
        {"part": light["id"], "role": "light"},
    ]
    if linker is not None:
        rows.append({"part": linker["id"], "role": "linker"})
    return rows


def errors(response: Any) -> dict[str, str]:
    assert response.status_code == 422, response.content
    return {e["pointer"]: e["detail"] for e in response.json()["errors"]}


def test_rows_are_saved_and_read_with_what_they_refer_to(world: World) -> None:
    heavy, light, linker = chains(world)
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"name": "BS-1", "components": components(heavy, light, linker)},
        BISPECIFIC,
    )
    assert made.status_code == 201, made.content
    body = made.json()
    rows = body["values"]["components"]
    # A reference to an identity holds its registry entry (ADR 0115), titled by the record.
    assert rows[0] == {"part": rows[0]["part"], "copies": 1, "chain": "H", "role": "heavy"}
    assert rows[2] == {"part": rows[2]["part"], "copies": 1, "chain": None, "role": "light"}
    named = [body["refs"][r["part"]]["hrid"] for r in rows]
    assert named == [heavy["hrid"], heavy["hrid"], light["hrid"], linker["hrid"]]
    found = api.get(f"/entities/{body['hrid']}").json()
    assert found["values"]["components"] == rows
    # The type says what each row has and what each role takes.
    field = next(
        f
        for f in api.get(f"/entity-types/{BISPECIFIC}").json()["fields"]
        if f["name"] == "components"
    )
    assert field["items"] == "row"
    assert [f["name"] for f in field["row"]["fields"]] == ["part", "copies", "chain", "role"]
    assert field["row"]["by"] == "role"
    heavy_role = field["row"]["roles"][0]
    assert heavy_role == {
        "name": "heavy",
        "label": "Heavy",
        "min": 2,
        "max": 2,
        "to": {"part": ["acme/protein.Protein"]},
    }


def test_each_row_field_is_checked_where_it_is(world: World) -> None:
    heavy, light, _ = chains(world)
    rows = components(heavy, light)
    rows[0] = {"role": "heavy", "copies": 0, "colour": "red"}
    found = errors(
        create(api_as(world), world.project, {"name": "BS-1", "components": rows}, BISPECIFIC)
    )
    assert found == {
        "#/values/components/0/part": "is required.",
        "#/values/components/0/copies": "must be at least 1.",
        "#/values/components/0/colour": "isn't one of the row's fields: part, copies, chain, role.",
    }


def test_each_role_takes_its_count_and_its_types(world: World) -> None:
    heavy, light, linker = chains(world)
    api = api_as(world)
    one_heavy = [
        {"part": heavy["id"], "role": "heavy"},
        {"part": light["id"], "role": "light"},
        {"part": linker["id"], "role": "linker"},
    ]
    found = errors(create(api, world.project, {"name": "B", "components": one_heavy}, BISPECIFIC))
    assert found == {"#/values/components": "needs at least 2 heavy rows, and has 1."}
    wrong = components(heavy, light)
    wrong[0]["part"] = linker["id"]
    found = errors(create(api, world.project, {"name": "B", "components": wrong}, BISPECIFIC))
    assert found == {
        "#/values/components/0/part": "must refer to a acme/protein.Protein in a heavy row, "
        "not a acme/dna.DNASequence."
    }
    two_linkers = [*components(heavy, light, linker), {"part": linker["id"], "role": "linker"}]
    found = errors(create(api, world.project, {"name": "B", "components": two_linkers}, BISPECIFIC))
    assert found == {"#/values/components": "can have at most 1 linker row, and has 2."}


def test_rows_change_as_a_whole_and_each_revision_keeps_them(world: World) -> None:
    heavy, _, linker = chains(world)
    api = api_as(world)
    added = [{"material": heavy["id"], "amount": {"value": 5, "unit": "ug"}, "note": "first"}]
    body = create(
        api, world.project, {"label": "S-1", "additions": added}, "acme/protein.Sample"
    ).json()
    more = [*added, {"material": linker["id"], "added_on": "2026-11-18"}]
    changed = patch(
        api,
        body["hrid"],
        {"values": {"additions": more}, "reason": "Added the linker"},
        api.get(f"/entities/{body['hrid']}").headers["ETag"],
    )
    assert changed.status_code == 200, changed.content
    assert len(changed.json()["values"]["additions"]) == 2
    revisions = api.get(f"/entities/{body['hrid']}/revisions").json()["items"]
    edited = next(r for r in revisions if r["number"] == 2)
    assert edited["changed_fields"] == ["additions"]
    first = api.get(f"/entities/{body['hrid']}/revisions/1").json()
    assert first["values"]["additions"] == body["values"]["additions"]


def test_a_set_of_parts_is_one_identity_whatever_their_order(world: World) -> None:
    """ADR 0138: a bispecific is its components, by part, copies and role, as a multiset:
    registered again in another order, or with other chain labels, it's found."""
    heavy, light, linker = chains(world)
    api = api_as(world)
    rows = components(heavy, light, linker)
    made = create(api, world.project, {"name": "BS-1", "components": rows}, BISPECIFIC)
    assert made.status_code == 201, made.content
    again = [{**rows[3]}, {**rows[2]}, {**rows[1], "chain": "X"}, {**rows[0]}]
    found = create(api, world.project, {"name": "BS-1 again", "components": again}, BISPECIFIC)
    assert found.status_code == 200, found.content
    assert found.json()["hrid"] == made.json()["hrid"]
    # Another count of a part is another antibody.
    other = [*rows[:2], {**rows[2], "copies": 2}, rows[3]]
    another = create(api, world.project, {"name": "BS-2", "components": other}, BISPECIFIC)
    assert another.status_code == 201, another.content
    assert another.json()["hrid"] != made.json()["hrid"]
    # Its components are its identity's, so they don't change.
    edited = patch(
        api,
        made.json()["hrid"],
        {"values": {"components": other}, "reason": "Two light"},
        api.get(f"/entities/{made.json()['hrid']}").headers["ETag"],
    )
    assert edited.status_code == 422, edited.content


def test_search_finds_a_row_with_every_value_given(world: World) -> None:
    heavy, light, linker = chains(world)
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"name": "BS-1", "components": components(heavy, light, linker)},
        BISPECIFIC,
    ).json()

    def search(value: dict[str, Any]) -> list[str]:
        found = api.post(
            "/entities/search",
            {"type": BISPECIFIC, "where": {"field": "components", "op": "has", "value": value}},
        )
        assert found.status_code == 200, found.content
        return [e["hrid"] for e in found.json()["items"]]

    assert search({"part": heavy["hrid"], "role": "heavy"}) == [made["hrid"]]
    assert search({"part": light["hrid"], "role": "heavy"}) == []
    assert search({"chain": "H2"}) == [made["hrid"]]
    bad = api.post(
        "/entities/search",
        {"type": BISPECIFIC, "where": {"field": "components", "op": "has", "value": {"x": 1}}},
    )
    assert bad.status_code == 422


def test_rows_references_are_edges_of_the_graph(world: World) -> None:
    heavy, light, linker = chains(world)
    made = create(
        api_as(world),
        world.project,
        {"name": "BS-1", "components": components(heavy, light, linker)},
        BISPECIFIC,
    ).json()
    # Its components are its identity's, so its registry entry's (ADR 0138).
    edges = Edge.objects.filter(node_id=made["entry"]["id"], kind="reference")
    assert sorted((e.field, e.position) for e in edges) == [
        ("components.part", 0),
        ("components.part", 1),
        ("components.part", 2),
        ("components.part", 3),
    ]
    found = walk(world, linker["id"], "downstream")
    assert found["hrids"] == [made["hrid"]]


def test_the_database_holds_rows_to_their_types_and_locks(world: World) -> None:
    heavy, light, _ = chains(world)
    made = create(
        api_as(world),
        world.project,
        {"name": "BS-1", "components": components(heavy, light)},
        BISPECIFIC,
    ).json()
    other = create(api_as(world), world.project, {"label": "S-1"}, "acme/protein.Sample").json()
    with (
        pytest.raises(DatabaseError, match="can't refer to"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE acme__protein_bispecific__entry__components SET part_id = %s "
            "WHERE entity_id = %s",
            [other["id"], made["entry"]["id"]],
        )
    # A list of rows is part of its record, so a lock (ADR 0125) freezes it too.
    assert ("acme__protein_sample__additions", "entity_id") in states.guarded_tables()

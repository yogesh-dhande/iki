"""Lists of rows (ADR 0137): each row's fields checked as a type's are, each role held to its
count and what it refers to, stored a column per row field, and every surface showing them.

The bispecifics' chains are real ones, KEGG DRUG's (data/fc_fusions_and_bispecifics.fasta):
emicizumab's two heavy chains and the light chain both share (D10821), and amivantamab's two
heavy chains and two light chains (D11894), whose disulphides pair A with C and B with D."""

from pathlib import Path
from typing import Any, NamedTuple

import pytest
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as, create, patch
from test_graph import walk
from test_runs import world

from iki_server.entities import states
from iki_server.graph.models import Edge

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # the fixture, used here too

BISPECIFIC = "acme/protein.Bispecific"


def _fasta(name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    header = ""
    for line in (Path(__file__).parent / "data" / name).read_text().splitlines():
        if line.startswith(">"):
            header = line[1:].split()[0]
            found[header] = ""
        else:
            found[header] += line.strip()
    return found


KEGG = _fasta("fc_fusions_and_bispecifics.fasta")


def dna(world: World, bases: str = "ACGT") -> Any:
    made = create(api_as(world), world.project, {"bases": bases})
    assert made.status_code == 201, made.content
    return made.json()


class Chains(NamedTuple):
    """Emicizumab's chains, and a DNA linker."""

    heavy: Any
    other_heavy: Any
    light: Any
    linker: Any


def chain(world: World, name: str) -> Any:
    made = create(api_as(world), world.project, {"sequence": KEGG[name]}, "acme/protein.Protein")
    assert made.status_code == 201, made.content
    return made.json()


def chains(world: World) -> Chains:
    return Chains(
        chain(world, "D10821-H1"), chain(world, "D10821-H2"), chain(world, "D10821-L"), dna(world)
    )


def components(c: Chains, linker: bool = False) -> list[dict[str, Any]]:
    """Emicizumab's chains, a row each: its light chain is both arms', so two rows."""
    rows = [
        {"part": c.heavy["id"], "role": "heavy", "chain": "H"},
        {"part": c.other_heavy["id"], "role": "heavy", "chain": "H2"},
        {"part": c.light["id"], "role": "light", "chain": "L"},
        {"part": c.light["id"], "role": "light"},
    ]
    if linker:
        rows.append({"part": c.linker["id"], "role": "linker"})
    return rows


def errors(response: Any) -> dict[str, str]:
    assert response.status_code == 422, response.content
    return {e["pointer"]: e["detail"] for e in response.json()["errors"]}


def test_rows_are_saved_and_read_with_what_they_refer_to(world: World) -> None:
    c = chains(world)
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"name": "BS-1", "components": components(c, linker=True)},
        BISPECIFIC,
    )
    assert made.status_code == 201, made.content
    body = made.json()
    rows = body["values"]["components"]
    # A reference to an identity holds its registry entry (ADR 0115), titled by the record.
    assert rows[0] == {"part": rows[0]["part"], "chain": "H", "role": "heavy"}
    assert rows[3] == {"part": rows[3]["part"], "chain": None, "role": "light"}
    named = [body["refs"][r["part"]]["hrid"] for r in rows]
    assert named == [
        c.heavy["hrid"],
        c.other_heavy["hrid"],
        c.light["hrid"],
        c.light["hrid"],
        c.linker["hrid"],
    ]
    found = api.get(f"/entities/{body['hrid']}").json()
    assert found["values"]["components"] == rows
    # The type says what each row has and what each role takes.
    field = next(
        f
        for f in api.get(f"/entity-types/{BISPECIFIC}").json()["fields"]
        if f["name"] == "components"
    )
    assert field["items"] == "row"
    assert [f["name"] for f in field["row"]["fields"]] == ["part", "chain", "role"]
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
    rows = components(chains(world))
    rows[0] = {"role": "heavy", "chain": "H" * 11, "colour": "red"}
    found = errors(
        create(api_as(world), world.project, {"name": "BS-1", "components": rows}, BISPECIFIC)
    )
    assert found == {
        "#/values/components/0/part": "is required.",
        "#/values/components/0/chain": "must be at most 10 characters long.",
        "#/values/components/0/colour": "isn't one of the row's fields: part, chain, role.",
    }


def test_each_role_takes_its_count_and_its_types(world: World) -> None:
    c = chains(world)
    api = api_as(world)
    one_heavy = [row for row in components(c, linker=True) if row.get("chain") != "H2"]
    found = errors(create(api, world.project, {"name": "B", "components": one_heavy}, BISPECIFIC))
    assert found == {"#/values/components": "needs at least 2 heavy rows, and has 1."}
    wrong = components(c)
    wrong[0]["part"] = c.linker["id"]
    found = errors(create(api, world.project, {"name": "B", "components": wrong}, BISPECIFIC))
    assert found == {
        "#/values/components/0/part": "must refer to a acme/protein.Protein in a heavy row, "
        "not a acme/dna.DNASequence."
    }
    two_linkers = [*components(c, linker=True), {"part": c.linker["id"], "role": "linker"}]
    found = errors(create(api, world.project, {"name": "B", "components": two_linkers}, BISPECIFIC))
    assert found == {"#/values/components": "can have at most 1 linker row, and has 2."}


def test_a_chain_the_antibody_has_two_of_is_two_rows(world: World) -> None:
    """Emicizumab's arms share a light chain: that's two light rows of one protein, the one
    way to write it, so its chains have one record. A row has no count of copies to write
    it as one row of two instead, and one light row is one light chain, too few; nor can a
    heavy chain be given nine copies."""
    c = chains(world)
    api = api_as(world)
    shared = [row for row in components(c) if row.get("chain") != "L"]
    found = errors(create(api, world.project, {"name": "B", "components": shared}, BISPECIFIC))
    assert found == {"#/values/components": "needs at least 2 light rows, and has 1."}
    counted = [{**shared[0], "copies": 9}, *shared[1:2], {**shared[2], "copies": 2}]
    found = errors(create(api, world.project, {"name": "B", "components": counted}, BISPECIFIC))
    assert found["#/values/components/0/copies"] == (
        "isn't one of the row's fields: part, chain, role."
    )
    made = create(api, world.project, {"name": "B", "components": components(c)}, BISPECIFIC)
    assert made.status_code == 201, made.content
    again = create(
        api, world.project, {"name": "B again", "components": components(c)[::-1]}, BISPECIFIC
    )
    assert again.status_code == 200 and again.json()["hrid"] == made.json()["hrid"]


def test_the_chains_say_which_chains_not_which_pairs(world: World) -> None:
    """Amivantamab's heavy chain A pairs with light chain C and B with D. The type is an
    inventory of chains, as it says, so A with D and B with C, a mispaired assembly of
    the same four chains (Schaefer et al., PNAS 108:11187, 2011), is the same record: a
    type that registers assemblies makes each row an arm."""
    a, b, c, d = (chain(world, f"D11894-{x}") for x in "ABCD")
    api = api_as(world)
    paired = [
        {"part": a["id"], "role": "heavy", "chain": "A"},
        {"part": c["id"], "role": "light", "chain": "C"},
        {"part": b["id"], "role": "heavy", "chain": "B"},
        {"part": d["id"], "role": "light", "chain": "D"},
    ]
    made = create(api, world.project, {"name": "Amivantamab", "components": paired}, BISPECIFIC)
    assert made.status_code == 201, made.content
    mispaired = [paired[0], paired[3], paired[2], paired[1]]
    found = create(api, world.project, {"name": "Mispaired", "components": mispaired}, BISPECIFIC)
    assert found.status_code == 200 and found.json()["hrid"] == made.json()["hrid"]
    described = api.get(f"/entity-types/{BISPECIFIC}").json()
    assert described["label"] == "Bispecific antibody's chains"
    assert (
        "doesn't say which light chain pairs with which heavy chain" in (described["description"])
    )


def test_rows_change_as_a_whole_and_each_revision_keeps_them(world: World) -> None:
    heavy, _, _, linker = chains(world)
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
    """ADR 0138: a bispecific's chains are its components, by part and role, as a
    multiset: registered again in another order, or with other chain labels, they're
    found."""
    c = chains(world)
    api = api_as(world)
    rows = components(c, linker=True)
    made = create(api, world.project, {"name": "BS-1", "components": rows}, BISPECIFIC)
    assert made.status_code == 201, made.content
    again = [rows[4], rows[3], {**rows[2], "chain": "X"}, rows[1], rows[0]]
    found = create(api, world.project, {"name": "BS-1 again", "components": again}, BISPECIFIC)
    assert found.status_code == 200, found.content
    assert found.json()["hrid"] == made.json()["hrid"]
    # Two light chains of different proteins are another antibody's.
    other = create(api, world.project, {"sequence": KEGG["D11894-D"]}, "acme/protein.Protein")
    two = [*rows[:3], {**rows[3], "part": other.json()["id"]}, rows[4]]
    another = create(api, world.project, {"name": "BS-2", "components": two}, BISPECIFIC)
    assert another.status_code == 201, another.content
    assert another.json()["hrid"] != made.json()["hrid"]
    # Its components are its identity's, so they don't change.
    edited = patch(
        api,
        made.json()["hrid"],
        {"values": {"components": two}, "reason": "Two light"},
        api.get(f"/entities/{made.json()['hrid']}").headers["ETag"],
    )
    assert edited.status_code == 422, edited.content


def test_search_finds_a_row_with_every_value_given(world: World) -> None:
    c = chains(world)
    heavy, light = c.heavy, c.light
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"name": "BS-1", "components": components(c, linker=True)},
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
    c = chains(world)
    linker = c.linker
    made = create(
        api_as(world),
        world.project,
        {"name": "BS-1", "components": components(c, linker=True)},
        BISPECIFIC,
    ).json()
    # Its components are its identity's, so its registry entry's (ADR 0138).
    edges = Edge.objects.filter(node_id=made["entry"]["id"], kind="reference")
    assert sorted((e.field, e.position) for e in edges) == [
        ("components.part", 0),
        ("components.part", 1),
        ("components.part", 2),
        ("components.part", 3),
        ("components.part", 4),
    ]
    found = walk(world, linker["id"], "downstream")
    assert found["hrids"] == [made["hrid"]]


def test_the_database_holds_rows_to_their_types_and_locks(world: World) -> None:
    made = create(
        api_as(world),
        world.project,
        {"name": "BS-1", "components": components(chains(world))},
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

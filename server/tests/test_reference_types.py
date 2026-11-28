"""References to one of several types (ADR 0136): checked as they're saved, held by the
database whatever writes them, and named by the API, its schemas and the graph."""

from typing import Any

import pytest
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as, create
from test_runs import protein, world

from iki_server.entities import targets
from iki_server.graph.models import Edge
from iki_server.plugins import sync

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # the fixture, used here too

SAMPLE = "acme/protein.Sample"
EITHER = ["acme/protein.Protein", "acme/dna.DNASequence"]


def sample(world: World, label: str, **values: Any) -> Any:
    return create(api_as(world), world.project, {"label": label, **values}, SAMPLE)


def dna(world: World, bases: str = "ACGT") -> Any:
    made = create(api_as(world), world.project, {"bases": bases})
    assert made.status_code == 201, made.content
    return made.json()


def test_a_reference_takes_any_of_its_types(world: World) -> None:
    abl1, puc = protein(api_as(world), world.project), dna(world)
    for i, target in enumerate((abl1, puc)):
        made = sample(world, f"S-{i}", material=target["id"])
        assert made.status_code == 201, made.content
        body = made.json()
        named = body["values"]["material"]
        assert body["refs"][named]["hrid"] == target["hrid"]
    other = sample(world, "S-9").json()
    refused = sample(world, "S-10", material=other["id"])
    assert refused.status_code == 422
    assert refused.json()["errors"] == [
        {
            "pointer": "#/values/material",
            "detail": "must refer to a acme/protein.Protein or a acme/dna.DNASequence, not a "
            "acme/protein.Sample.",
        }
    ]


def test_the_type_names_each_type_it_may_refer_to(world: World) -> None:
    found = api_as(world).get(f"/entity-types/{SAMPLE}").json()
    fields = {f["name"]: f for f in found["fields"]}
    assert fields["material"]["to"] == EITHER
    assert fields["protein"]["to"] == ["acme/protein.Protein"]
    assert fields["label"]["to"] == []
    schema = found["values_schema"]["properties"]["material"]["anyOf"][0]
    assert schema["x-iki-ref"] == EITHER


def test_listing_finds_entities_of_any_of_several_types(world: World) -> None:
    abl1, puc = protein(api_as(world), world.project), dna(world)
    s1 = sample(world, "S-1").json()
    found = api_as(world).get("/entities", type=EITHER).json()["items"]
    assert {e["hrid"] for e in found} == {abl1["hrid"], puc["hrid"]}
    every = api_as(world).get("/entities", type=[*EITHER, SAMPLE]).json()["items"]
    assert {e["hrid"] for e in every} == {abl1["hrid"], puc["hrid"], s1["hrid"]}


def test_the_database_refuses_a_reference_to_another_type(world: World) -> None:
    """Its foreign key is to iki_entity, so a trigger holds it to its types."""
    abl1 = protein(api_as(world), world.project)
    one = sample(world, "S-1", material=abl1["id"]).json()
    two = sample(world, "S-2").json()
    assert "acme__protein_sample" in targets.install(connection.cursor(), _wanted())
    with (
        pytest.raises(DatabaseError, match="can't refer to"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE acme__protein_sample SET material_id = %s WHERE entity_id = %s",
            [two["id"], one["id"]],
        )
    # A reference to one concrete type is held by its foreign key, not a trigger.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgargs FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = 'acme__protein_sample' AND t.tgname = %s",
            [targets.TRIGGER_PREFIX + "ins"],
        )
        [(raw,)] = cursor.fetchall()
    args = [a.decode() for a in bytes(raw).split(b"\x00")[:-1]]
    assert args[0] == "material_id"
    assert "protein_id" not in args


def _wanted() -> list[targets.Table]:
    return targets.tables((p, m, f) for p, m, f, _ in sync._tables())


def test_the_sync_refuses_values_a_new_trigger_wouldnt_allow(world: World) -> None:
    one = sample(world, "S-1").json()
    two = sample(world, "S-2").json()
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TRIGGER {targets.TRIGGER_PREFIX}upd ON acme__protein_sample")
        cursor.execute(
            "UPDATE acme__protein_sample SET material_id = %s WHERE entity_id = %s",
            [two["id"], one["id"]],
        )
    with pytest.raises(targets.Mismatch, match=f"{one['hrid']} names {two['hrid']}"):
        sync.sync()


def test_the_graph_has_an_edge_for_each(world: World) -> None:
    puc = dna(world)
    one = sample(world, "S-1", material=puc["id"]).json()
    assert Edge.objects.filter(node_id=one["id"], field="material", kind="reference").exists()

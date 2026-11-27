"""What a subtype narrows in a field it inherits (ADR 0283): its records as the API takes
them, its type as the API gives it, and the database holding the narrowing whatever writes
it, from the declaring type's table."""

from typing import Any

import pytest
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as, create
from test_runs import protein, world

from iki_server.entities import narrowings, registry
from iki_server.plugins import sync

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # the fixture, used here too

LOT = "acme/protein.Lot"
GMP = "acme/protein.GmpLot"


def lot(world: World, type_name: str = GMP, **values: Any) -> Any:
    return create(api_as(world), world.project, {"label": "Lot 7", **values}, type_name)


def test_a_subtype_resolves_to_its_narrowed_fields() -> None:
    declared = registry.effective(GMP)
    assert declared.lineage == (GMP, LOT, "iki/core.Entity")
    assert declared.fields["protein"].required
    assert (declared.fields["yield_mg"].min, declared.fields["yield_mg"].max) == (1, 5000)
    assert declared.sources["refines.protein"] == GMP
    assert declared.declared_by("protein") == LOT
    # The lot it narrows is as it was.
    assert not registry.effective(LOT).fields["protein"].required


def test_its_records_take_only_what_it_narrows_to(world: World) -> None:
    abl1 = protein(api_as(world), world.project)
    refused = lot(world, yield_mg=0.5)
    assert refused.status_code == 422
    pointers = {e["pointer"] for e in refused.json()["errors"]}
    assert pointers == {"#/values/protein", "#/values/yield_mg"}
    made = lot(world, protein=abl1["id"], yield_mg=12.0)
    assert made.status_code == 201, made.content
    # A lot itself still takes any protein, or none, and any yield.
    assert lot(world, LOT, yield_mg=0.5).status_code == 201


def test_the_api_says_what_it_narrows(world: World) -> None:
    found = api_as(world).get(f"/entity-types/{GMP}").json()
    assert found["ancestors"] == [LOT, "iki/core.Entity"]
    fields = {f["name"]: f for f in found["fields"]}
    assert fields["protein"]["required"]
    assert "protein" in found["values_schema"]["required"]


def test_the_database_holds_it_on_the_declaring_table(world: World) -> None:
    """The column is the lot's, so the trigger is on the lot's table and holds only the
    rows of GMP lots."""
    abl1 = protein(api_as(world), world.project)
    gmp = lot(world, protein=abl1["id"], yield_mg=12.0).json()
    plain = lot(world, LOT).json()
    with connection.cursor() as cursor:
        held = narrowings.install(cursor, narrowings.wanted())
    assert "acme__protein_lot.protein_id:acme/protein.GmpLot:required" in held
    assert "acme__protein_lot.yield_mg:acme/protein.GmpLot:min" in held
    for statement, match in (
        ("UPDATE acme__protein_lot SET protein_id = NULL WHERE entity_id = %s", "need protein"),
        ("UPDATE acme__protein_lot SET yield_mg = 0.1 WHERE entity_id = %s", "at least 1"),
        ("UPDATE acme__protein_lot SET yield_mg = 9000 WHERE entity_id = %s", "at most 5000"),
    ):
        with (
            pytest.raises(DatabaseError, match=match),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement, [gmp["id"]])
    # A plain lot's row is the lot's to hold, and a lot allows it.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE acme__protein_lot SET yield_mg = 0.1 WHERE entity_id = %s", [plain["id"]]
        )


def test_records_that_dont_fit_stop_the_deploy(world: World) -> None:
    abl1 = protein(api_as(world), world.project)
    gmp = lot(world, protein=abl1["id"], yield_mg=12.0).json()
    with connection.cursor() as cursor:
        cursor.execute(
            "DROP TRIGGER IF EXISTS iki_narrowed__acme__protein_lot ON acme__protein_lot"
        )
        cursor.execute(
            "UPDATE acme__protein_lot SET protein_id = NULL WHERE entity_id = %s", [gmp["id"]]
        )
    with (
        pytest.raises(narrowings.Unfit, match=gmp["hrid"]),
        transaction.atomic(),
    ):
        sync.sync()

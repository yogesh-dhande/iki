"""Unique fields: no two entities in a project share a value (ADR 0082)."""

from typing import Any

import pytest
from api_client import Api, problem
from django.db import IntegrityError, connection, transaction
from org_world import World, build_world

from iki_server.entities import services, unique
from iki_server.entities.models import Entity, UniqueValue
from iki_server.files.models import Blob, File
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.plugins import sync

pytestmark = pytest.mark.django_db

KEY = "protein_protein.lims_id"


@pytest.fixture
def world() -> World:
    return build_world()


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def protein(api: Api, project: Project, sequence: str, **values: Any) -> Any:
    return api.post(
        "/entities",
        {
            "type": "protein.Protein",
            "project": str(project.pk),
            "values": {"sequence": sequence, **values},
        },
    )


def test_a_value_is_one_entitys_in_a_project(world: World) -> None:
    api = api_as(world)
    first = protein(api, world.project, "MKV", lims_id="LIMS-1")
    assert first.status_code == 201, first.content
    hrid = first.json()["hrid"]
    body = problem(protein(api, world.project, "MKVL", lims_id="LIMS-1"), 422, "validation_failed")
    assert body["errors"] == [
        {
            "pointer": "#/values/lims_id",
            "detail": f"is {hrid}’s already: no two entities in a project share it.",
        }
    ]
    assert protein(api, world.project, "MKVL", lims_id="LIMS-2").status_code == 201
    assert UniqueValue.objects.filter(key=KEY).count() == 2


def test_another_project_keeps_its_own_entity(world: World) -> None:
    other = Project.objects.create(team=world.team, name="Enzymes")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    api = api_as(world)
    assert protein(api, world.project, "MKV", lims_id="LIMS-1").status_code == 201
    made = protein(api, other, "MKVL", lims_id="LIMS-1")
    assert made.status_code == 201, made.content


def test_changing_to_a_taken_value_is_refused_and_clearing_frees_it(world: World) -> None:
    api = api_as(world)
    first = protein(api, world.project, "MKV", lims_id="LIMS-1").json()
    second = protein(api, world.project, "MKVL", lims_id="LIMS-2")
    url = f"/entities/{second.json()['hrid']}"
    body = problem(
        api.patch(url, {"values": {"lims_id": "LIMS-1"}}, headers={"If-Match": second["ETag"]}),
        422,
        "validation_failed",
    )
    assert body["errors"][0]["detail"].startswith(f"is {first['hrid']}’s already")

    cleared = api.patch(
        f"/entities/{first['hrid']}",
        {"values": {"lims_id": None}},
        headers={"If-Match": f'"{first["revision"]}"'},
    )
    assert cleared.status_code == 200, cleared.content
    moved = api.patch(url, {"values": {"lims_id": "LIMS-1"}}, headers={"If-Match": second["ETag"]})
    assert moved.status_code == 200, moved.content
    assert list(UniqueValue.objects.filter(key=KEY).values_list("value", flat=True)) == ["LIMS-1"]


def test_the_database_refuses_a_duplicate_whatever_writes_it(world: World) -> None:
    api = api_as(world)
    protein(api, world.project, "MKV", lims_id="LIMS-1")
    second = protein(api, world.project, "MKVL").json()
    with (
        pytest.raises(IntegrityError, match="iki_unique_value_once"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE protein_protein SET lims_id = 'LIMS-1' WHERE entity_id = %s", [second["id"]]
        )


def test_a_value_written_at_the_same_moment_is_refused_too(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = api_as(world)
    protein(api, world.project, "MKV", lims_id="LIMS-1")
    # As if another request wrote it between the check and the write.
    monkeypatch.setattr(unique, "taken", lambda *a, **k: {})
    body = problem(protein(api, world.project, "MKVL", lims_id="LIMS-1"), 422, "validation_failed")
    assert body["errors"][0]["pointer"] == "#/values/unique"
    assert Entity.objects.filter(type="protein.Protein").count() == 1


def test_extension_fields_can_be_unique(world: World) -> None:
    blob = Blob.objects.create(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    pdb = File.objects.create(
        blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=world.project
    )
    user = world.users["editor"]
    values = {"structure": str(pdb.pk), "job_id": "JOB-7"}
    services.create(user, "protein.ProteinStructure", world.project, values)
    with pytest.raises(services.Invalid) as raised:
        services.create(user, "protein.ProteinStructure", world.project, values)
    assert list(raised.value.errors) == ["job_id"]


def test_types_say_which_fields_are_unique(world: World) -> None:
    fields = {
        f["name"]: f for f in api_as(world).get("/entity-types/protein.Protein").json()["fields"]
    }
    assert fields["lims_id"]["unique"] is True
    assert fields["organism"]["unique"] is False


def test_sync_fills_in_values_and_stops_at_entities_sharing_one(world: World) -> None:
    api = api_as(world)
    first = protein(api, world.project, "MKV", lims_id="LIMS-1").json()
    second = protein(api, world.project, "MKVL").json()
    with connection.cursor() as cursor:
        # As before the field was marked unique: no trigger, no values kept.
        cursor.execute("DROP TRIGGER iki_unique__lims_id ON protein_protein")
        cursor.execute("DELETE FROM iki_unique_value")
        cursor.execute(
            "UPDATE protein_protein SET lims_id = 'LIMS-1' WHERE entity_id = %s", [second["id"]]
        )
    with pytest.raises(unique.Clash) as raised, transaction.atomic():
        sync.sync()
    assert (
        f"{', '.join(sorted([first['hrid'], second['hrid']]))} in {world.project.name} have "
        f"`{KEY}` “LIMS-1”"
    ) in str(raised.value)

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE protein_protein SET lims_id = 'LIMS-2' WHERE entity_id = %s", [second["id"]]
        )
    report = sync.sync()
    assert KEY in report.unique
    assert sorted(UniqueValue.objects.filter(key=KEY).values_list("value", flat=True)) == [
        "LIMS-1",
        "LIMS-2",
    ]
    # The trigger is back.
    assert problem(protein(api, world.project, "MKVLA", lims_id="LIMS-2"), 422, "validation_failed")

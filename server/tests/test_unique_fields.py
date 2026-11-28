"""Unique fields: no two records in a project share a value (ADR 0082), as the one-field
``unique`` rule each is; and ``within: registry`` rules, a unique fact's among them, of the
entries in a registry (ADR 0115)."""

from typing import Any

import pytest
from api_client import Api, problem
from django.db import IntegrityError, connection, transaction
from org_world import World, build_world
from scan_fixtures import clean_blob

from iki_server.entities import constraints, services, unique
from iki_server.entities.models import Entity, UniqueValue
from iki_server.files.models import File
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.plugins import sync

pytestmark = pytest.mark.django_db

KEY = "acme__protein_protein:lims_id"


def held(key: str = KEY) -> list[str]:
    """The values the database holds under a unique field's rule."""
    return sorted(
        unique.text(v) for v in UniqueValue.objects.filter(key=key).values_list("value", flat=True)
    )


@pytest.fixture
def world() -> World:
    return build_world()


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def protein(api: Api, project: Project, sequence: str, **values: Any) -> Any:
    return api.post(
        "/entities",
        {
            "type": "acme/protein.Protein",
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
    # The field's one-field rule refuses it, said of the field as a key is.
    assert [(e["pointer"], e["detail"], e.get("rule")) for e in body["errors"]] == [
        (
            "#/values/lims_id",
            f"is {hrid}’s already: no two entities in a project share it.",
            "lims_id",
        )
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
    assert held() == ["LIMS-1"]


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
            "UPDATE acme__protein_protein SET lims_id = 'LIMS-1' WHERE entity_id = %s",
            [second["id"]],
        )


def test_a_value_written_at_the_same_moment_is_refused_too(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = api_as(world)
    protein(api, world.project, "MKV", lims_id="LIMS-1")
    # As if another request wrote it between the check and the write.
    monkeypatch.setattr(constraints, "taken", lambda *a, **k: [])
    body = problem(protein(api, world.project, "MKVL", lims_id="LIMS-1"), 422, "validation_failed")
    assert body["errors"] == [
        {
            "pointer": "#/values/lims_id",
            "detail": "Another record has just been saved that this one would break “Unique "
            "lims id” with.",
        }
    ]
    assert (
        Entity.objects.filter(type="acme/protein.Protein", owner_project__isnull=False).count() == 1
    )


def test_extension_fields_can_be_unique(world: World) -> None:
    blob = clean_blob(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    pdb = File.objects.create(
        blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=world.project
    )
    user = world.users["editor"]
    values = {"structure": str(pdb.pk), "job_id": "JOB-7"}
    services.create(user, "acme/protein.ProteinStructure", world.project, values)
    with pytest.raises(services.Invalid) as raised:
        services.create(user, "acme/protein.ProteinStructure", world.project, values)
    assert list(raised.value.errors) == ["job_id"]


def test_types_say_which_fields_are_unique(world: World) -> None:
    found = api_as(world).get("/entity-types/acme/protein.Protein").json()
    fields = {f["name"]: f for f in found["fields"]}
    assert fields["lims_id"]["unique"] is True
    assert fields["host"]["unique"] is False


def test_sync_fills_in_values_and_stops_at_entities_sharing_one(world: World) -> None:
    api = api_as(world)
    first = protein(api, world.project, "MKV", lims_id="LIMS-1").json()
    second = protein(api, world.project, "MKVL").json()
    with connection.cursor() as cursor:
        # As before the field was marked unique: no trigger, no values kept.
        cursor.execute("DROP TRIGGER iki_rule__lims_id ON acme__protein_protein")
        cursor.execute("DELETE FROM iki_unique_value")
        cursor.execute(
            "UPDATE acme__protein_protein SET lims_id = 'LIMS-1' WHERE entity_id = %s",
            [second["id"]],
        )
    with pytest.raises(constraints.Clash) as raised, transaction.atomic():
        sync.sync()
    assert (
        f"{', '.join(sorted([first['hrid'], second['hrid']]))} in {world.project.name} break "
        "“Unique lims id”"
    ) in str(raised.value)

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE acme__protein_protein SET lims_id = 'LIMS-2' WHERE entity_id = %s",
            [second["id"]],
        )
    report = sync.sync()
    assert KEY in report.rules
    assert held() == ["LIMS-1", "LIMS-2"]
    # The trigger is back.
    assert problem(protein(api, world.project, "MKVLA", lims_id="LIMS-2"), 422, "validation_failed")


# --- Looking up, and links out ------------------------------------------------------------------


def lookup(api: Api, value: str, **params: Any) -> Any:
    return api.get(
        "/entities/lookup",
        **{"type": "acme/protein.Protein", "field": "lims_id", "value": value, **params},
    )


def test_an_external_id_finds_its_entity_in_each_project_you_can_read(world: World) -> None:
    other = Project.objects.create(team=world.team, name="Enzymes")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    api = api_as(world)
    mine = protein(api, world.project, "MKV", lims_id="LIMS-1").json()
    theirs = protein(api, other, "MKVL", lims_id="LIMS-1").json()

    found = lookup(api, "LIMS-1")
    assert found.status_code == 200, found.content
    assert [e["hrid"] for e in found.json()] == [mine["hrid"], theirs["hrid"]]
    assert [e["hrid"] for e in lookup(api, "LIMS-1", project=str(other.pk)).json()] == [
        theirs["hrid"]
    ]
    assert lookup(api, "LIMS-2").json() == []
    # A subtype's lookup by its parent's type finds it too; another type's doesn't.
    assert lookup(api, "LIMS-1", type="acme/dna.Sequence").status_code == 422  # no such field there

    # Someone who can read only the first project sees only its entity.
    viewer = world.users["viewer"]
    assert not ProjectMembership.objects.filter(project=other, user=viewer).exists()
    assert [e["hrid"] for e in lookup(api_as(world, "viewer"), "LIMS-1").json()] == [mine["hrid"]]
    problem(lookup(api_as(world, "viewer"), "LIMS-1", project=str(other.pk)), 404, "not_found")


def test_only_a_unique_field_looks_entities_up(world: World) -> None:
    api = api_as(world)
    body = problem(lookup(api, "Homo sapiens", field="host"), 422, "validation_failed")
    assert body["errors"] == [{"parameter": "field", "detail": "isn't a unique field of the type."}]
    problem(lookup(api, "X", type="acme/protein.Nope"), 404, "not_found")


def test_fields_link_to_the_record_in_the_other_system(world: World) -> None:
    api = api_as(world)
    found = api.get("/entity-types/acme/protein.Protein").json()
    proteins = {f["name"]: f for f in found["fields"]}
    assert proteins["lims_id"]["link"] == "https://lims.example.org/proteins/{value}"
    assert proteins["host"]["link"] is None
    found = api.get("/entity-types/acme/protein.ProteinStructure").json()
    structures = {f["name"]: f for f in found["fields"]}
    # From the plugin's setting, as the deployment set it.
    assert structures["job_id"]["link"] == "https://predict.example.org/jobs/{value}"


@pytest.mark.parametrize(
    ("setting", "link"),
    [
        ("https://jobs.example.org/", "https://jobs.example.org/jobs/{value}"),
        ("javascript:alert(1)//", None),
        ("http://jobs.example.org", None),
        ("", None),
    ],
)
def test_a_link_is_only_ever_https(
    monkeypatch: pytest.MonkeyPatch, setting: str, link: str | None
) -> None:
    from iki_server.entities import registry, values

    monkeypatch.setattr(registry, "settings", lambda plugin: {"jobs_url": setting})
    field = values.shape("acme/protein.ProteinStructure").fields()["job_id"]
    assert unique.link(field, "structure_prediction") == link


# --- Within a registry --------------------------------------------------------------------------

ETHANOL = ("CCO", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N")
METHANOL = ("CO", "OKKJLVBELUTLKV-UHFFFAOYSA-N")
ACETONE = ("CC(C)=O", "CSCPPACGZOOCGX-UHFFFAOYSA-N")


def compound(api: Api, project: Project, which: tuple[str, str], **values: Any) -> Any:
    smiles, inchikey = which
    return api.post(
        "/entities",
        {
            "type": "acme/protein.Compound",
            "project": str(project.pk),
            "values": {"smiles": smiles, "inchikey": inchikey, **values},
        },
    )


def test_a_registry_rule_holds_of_its_entries_facts_together(world: World) -> None:
    api = api_as(world)
    first = compound(api, world.project, ETHANOL, vendor="Sigma-Aldrich", catalogue_number="459836")
    assert first.status_code == 201, first.content
    hrid = first.json()["hrid"]
    body = problem(
        compound(api, world.project, METHANOL, vendor="Sigma-Aldrich", catalogue_number="459836"),
        422,
        "validation_failed",
    )
    told = f"Another compound is this vendor's catalogue number already. {hrid} has the same."
    assert body["errors"] == [
        {"pointer": "#/values/vendor", "detail": told},
        {"pointer": "#/values/catalogue_number", "detail": told},
    ]
    # Another vendor may use the number, and so may an entry with no vendor.
    other = compound(api, world.project, METHANOL, vendor="TCI", catalogue_number="459836")
    assert other.status_code == 201, other.content
    assert compound(api, world.project, ACETONE, catalogue_number="459836").status_code == 201

    (rule,) = [c for c in constraints.wanted() if c.rule.name == "one_entry_per_catalogue_number"]
    rows = UniqueValue.objects.filter(key=rule.key)
    assert rows.count() == 2
    assert {r.registry_id for r in rows} == {world.project.registry_id}
    assert all(r.project_id is None for r in rows)
    # The database holds it, whatever writes.
    entry = Entity.objects.get(pk=other.json()["id"]).entry_id
    with (
        pytest.raises(IntegrityError, match="iki_unique_value_once_in_registry"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            f'UPDATE "{rule.table}" SET vendor = %s WHERE entity_id = %s',  # noqa: S608 - ours
            ["Sigma-Aldrich", entry],
        )


def test_an_int_key_is_found_by_its_value_and_held_once_in_a_registry(world: World) -> None:
    api = api_as(world)
    first = compound(api, world.project, ETHANOL, pubchem_cid=702)
    assert first.status_code == 201, first.content
    hrid = first.json()["hrid"]
    body = problem(
        compound(api, world.project, METHANOL, pubchem_cid=702), 422, "validation_failed"
    )
    assert body["errors"] == [
        {
            "pointer": "#/values/pubchem_cid",
            "detail": f"is {hrid}’s already: no two entries in the registry share it.",
        }
    ]
    found = api.get(
        "/entities/lookup", type="acme/protein.Compound", field="pubchem_cid", value="702"
    )
    assert [e["hrid"] for e in found.json()] == [hrid]
    fields = {f["name"]: f for f in api.get("/entity-types/acme/protein.Compound").json()["fields"]}
    assert fields["pubchem_cid"]["unique"] is True
    assert fields["pubchem_cid"]["link"] == "https://pubchem.ncbi.nlm.nih.gov/compound/{value}"

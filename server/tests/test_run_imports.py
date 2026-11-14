"""Imports (ADR 0052): actions with no inputs that take a file as a param and create
entities, each with where it came from. And the plugin settings every run is given."""

from pathlib import Path
from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as
from test_run_results import done, output
from test_runs import PREDICT, bind_vm, launch, protein, world
from test_workers import keep_connection, worker

from iki_server.entities import services
from iki_server.entities.models import Entity, ProjectLink
from iki_server.files import services as files
from iki_server.files.services import EMPTY_SHA256
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs.models import ActionRun, ExternalOrigin
from iki_spec import schemas

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

IMPORT = "protein.import_proteins"
SHA = "a" * 64


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    """Files are stored, as imports need them to be."""
    return s3


def stored(world: World, name: str = "kinases.fasta", project: Project | None = None) -> str:
    """A file (empty, so it's ready at once) the editor uploaded."""
    upload = files.start(
        world.users["editor"],
        project or world.project,
        name=name,
        media_type="chemical/seq-aa-fasta" if name.endswith(".fasta") else "chemical/x-pdb",
        size=0,
        sha256=EMPTY_SHA256,
    )
    upload.refresh_from_db()
    return str(upload.file_id)


def importing(world: World, file_id: str, principal: str = "editor") -> Any:
    return launch(api_as(world, principal), world.project, {}, IMPORT, params={"file": file_id})


def errors(response: Any) -> dict[str, str]:
    body = problem(response, 422, "validation_failed")
    return {e["pointer"]: e["detail"] for e in body["errors"]}


# --- File params --------------------------------------------------------------------------------


def test_an_import_runs_on_a_file_with_no_entities_to_read(world: World) -> None:
    file_id = stored(world)
    made = importing(world, file_id)
    assert made.status_code == 201, made.content
    run = made.json()
    assert (run["inputs"], run["params"]) == ([], {"file": file_id})
    assert run["files"] == {
        file_id: {"name": "kinases.fasta", "media_type": "chemical/seq-aa-fasta", "size": 0}
    }

    pod = worker(world)
    job = pod.lease().json()
    assert not list(schemas.check("run-input", job))
    assert job["inputs"] == {}
    assert job["params"] == {"file": file_id}
    assert job["files"] == {
        file_id: {
            "name": "kinases.fasta",
            "media_type": "chemical/seq-aa-fasta",
            "size": 0,
            "sha256": EMPTY_SHA256,
        }
    }
    # Its worker may download the file it was given.
    got = pod.report(job, f"files/{file_id}")
    assert got.status_code == 200, got.content
    assert got.json()["name"] == "kinases.fasta"


def test_a_file_param_takes_only_a_file_the_person_can_read_of_the_right_type(
    world: World,
) -> None:
    assert errors(launch(api_as(world), world.project, {}, IMPORT)) == {
        "#/params/file": "Choose a value for this param."
    }
    assert errors(importing(world, "kinases.fasta")) == {
        "#/params/file": "The value must be a file's ID: upload the file first."
    }
    assert errors(importing(world, stored(world, "abl.pdb"))) == {
        "#/params/file": "The value must be chemical/seq-aa-fasta or text/plain, and this file "
        "is chemical/x-pdb."
    }
    # A file in a project the person isn't in is as if it weren't there.
    private = Project.objects.create(team=world.team, name="Private")
    ProjectMembership.objects.create(
        project=private, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    hidden = stored(world, project=private)
    assert errors(importing(world, hidden, "runner")) == {
        "#/params/file": "The value refers to no file you can read."
    }


# --- Where imported entities came from ----------------------------------------------------------


def origin(file_id: str, name: str = "ABL1", **extra: Any) -> dict[str, Any]:
    return {"source": "FASTA file", "id": name, "file": file_id, "sha256": SHA, **extra}


def imported(world: World, *created: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """An import run, leased and completed with `created`. Returns the run and the answer."""
    file_id = stored(world)
    run = importing(world, file_id).json()
    pod = worker(world)
    job = pod.lease().json()
    document = output(
        creates=[{**c, "origin": origin(file_id, **c.get("origin", {}))} for c in created]
    )
    return run, done(pod, job, document)


def test_imported_entities_keep_where_they_came_from(world: World) -> None:
    sequence = {"type": "protein.Protein", "values": {"name": "ABL1", "sequence": "MKVLEIC"}}
    run, answer = imported(world, sequence)
    assert answer["run_status"] == "succeeded", answer
    body = api_as(world).get(f"/runs/{run['id']}").json()
    (effect,) = body["effects"]
    assert effect["kind"] == "created"
    file_id = run["params"]["file"]
    origins = api_as(world, "viewer").get(f"/entities/{effect['hrid']}/origins").json()
    assert origins == [
        {
            "source": "FASTA file",
            "id": "ABL1",
            "version": "",
            "url": "",
            "retrieved_at": None,
            "sha256": SHA,
            "file": {"id": file_id, "name": "kinases.fasta"},
            "run": {"id": run["id"], "hrid": run["hrid"], "action": IMPORT},
            "revision": 1,
            "recorded_at": origins[0]["recorded_at"],
        }
    ]

    # Imported again, it's the same protein, found again: that's kept too.
    _, again = imported(world, sequence)
    assert again["run_status"] == "succeeded", again
    later, first = api_as(world).get(f"/entities/{effect['hrid']}/origins").json()
    assert (later["revision"], first["revision"]) == (None, 1)
    assert ExternalOrigin.objects.count() == 2


def test_a_database_record_keeps_its_version_link_and_when_it_was_fetched(world: World) -> None:
    fetched = {
        "type": "protein.Protein",
        "values": {"name": "P00519", "sequence": "MLEIC"},
        "origin": {
            "source": "UniProtKB",
            "name": "P00519",
            "version": "2026_04",
            "url": "https://rest.uniprot.org/uniprotkb/P00519",
            "retrieved_at": "2026-11-11T09:30:00+00:00",
        },
    }
    run, answer = imported(world, fetched)
    assert answer["run_status"] == "succeeded", answer
    row = ExternalOrigin.objects.get(run_id=run["id"])
    assert (row.source, row.identifier, row.version) == ("UniProtKB", "P00519", "2026_04")
    assert row.url == "https://rest.uniprot.org/uniprotkb/P00519"
    assert row.retrieved_at is not None and row.retrieved_at.isoformat() == (
        "2026-11-11T09:30:00+00:00"
    )


@pytest.mark.parametrize(
    ("given", "pointer", "detail"),
    [
        (
            {"retrieved_at": "2026-11-11T09:30:00"},
            "#/creates/0/origin/retrieved_at",
            "Give a date and time with a time zone, such as 2026-11-11T09:30:00Z.",
        ),
        (
            {"file": "0190e9a4-0000-7000-8000-000000000009"},
            "#/creates/0/origin/file",
            "Name a file the run was given in a file param.",
        ),
    ],
)
def test_an_origin_must_be_when_and_what_the_run_was_given(
    world: World, given: dict[str, Any], pointer: str, detail: str
) -> None:
    sequence = {
        "type": "protein.Protein",
        "values": {"name": "ABL1", "sequence": "MKV"},
        "origin": given,
    }
    run, answer = imported(world, sequence)
    assert answer["run_status"] == "failed"
    assert answer["errors"] == [{"pointer": pointer, "detail": detail}]
    assert not ExternalOrigin.objects.filter(run_id=run["id"]).exists()


def test_origins_dont_show_runs_or_files_the_reader_cant(world: World) -> None:
    sequence = {"type": "protein.Protein", "values": {"name": "ABL1", "sequence": "MKVL"}}
    run, _ = imported(world, sequence)
    entity = ActionRun.objects.get(pk=run["id"]).effects.get().entity
    # Shared into a project the run isn't in, it's read by someone who can't see the run.
    elsewhere = Project.objects.create(team=world.team, name="Elsewhere")
    outsider = world.users["team_member"]
    ProjectMembership.objects.create(project=elsewhere, user=outsider, role=ProjectRole.VIEWER)
    ProjectLink.objects.create(entity=entity, project=elsewhere)
    (seen,) = api_as(world, "team_member").get(f"/entities/{entity.hrid}/origins").json()
    assert (seen["source"], seen["id"]) == ("FASTA file", "ABL1")
    assert seen["run"] is None
    assert seen["file"] is None


# --- Plugin settings ----------------------------------------------------------------------------


def test_runs_get_their_plugins_settings_as_the_deployment_set_them(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bind_vm(monkeypatch, tmp_path)
    runner = api_as(world, "runner")
    abl = protein(runner, world.project)
    made = launch(runner, world.project, {"protein": abl["id"]}, PREDICT)
    assert made.status_code == 201, made.content
    # The fixture plugins are built without a deployment, so they take their defaults.
    expected = {
        "jobs_url": "https://predict.example.org",
        "default_model": "colabfold",
        "allowed_presets": ["monomer"],
    }
    assert made.json()["settings"] == expected
    assert ActionRun.objects.get(pk=made.json()["id"]).plugin_settings == expected


def test_runs_of_plugins_without_settings_get_none(world: World) -> None:
    run = importing(world, stored(world)).json()
    assert run["settings"] == {}
    assert worker(world).lease().json()["settings"] == {}


def test_an_import_may_attach_the_file_it_was_given(world: World) -> None:
    run = importing(world, stored(world)).json()
    # As an action that imports structures would be launched: on a structure file.
    file_id = stored(world, "1abc.pdb")
    stored_run = ActionRun.objects.get(pk=run["id"])
    stored_run.spec["creates"].append(
        {"type": "protein.ProteinStructure", "cardinality": "many", "derived_from": []}
    )
    ActionRun.objects.filter(pk=stored_run.pk).update(
        spec=stored_run.spec, params={"file": file_id}
    )
    pod = worker(world)
    job = pod.lease().json()
    structure = {
        "type": "protein.ProteinStructure",
        "key": "s",
        "values": {"structure": file_id, "method": "xray", "chains": [{"$create": "a"}]},
    }
    chain = {"type": "protein.Protein", "key": "a", "values": {"sequence": "MKVW"}}
    body = done(pod, job, output(creates=[chain, structure]))
    assert body["run_status"] == "succeeded", body
    made = Entity.objects.get(type="protein.ProteinStructure")
    values = api_as(world, "editor").get(f"/entities/{made.pk}").json()["values"]
    assert values["structure"] == file_id


# --- Matching on a unique field (ADR 0082) ------------------------------------------------------


def lims(name: str, sequence: str = "MKVLEIC", lims_id: str = "LIMS-1", **values: Any) -> Any:
    return {
        "type": "protein.Protein",
        "values": {"name": name, "sequence": sequence, "lims_id": lims_id, **values},
        "origin": {"id": lims_id},
    }


def test_a_record_that_comes_again_updates_its_entity(world: World) -> None:
    run, answer = imported(world, lims("ABL1"))
    assert answer["run_status"] == "succeeded", answer
    (made,) = api_as(world).get(f"/runs/{run['id']}").json()["effects"]

    again, answer = imported(world, lims("ABL1 kinase", organism="Homo sapiens"))
    assert answer["run_status"] == "succeeded", answer
    (effect,) = api_as(world).get(f"/runs/{again['id']}").json()["effects"]
    assert (effect["hrid"], effect["kind"], effect["revision"]) == (made["hrid"], "updated", 2)
    entity = api_as(world).get(f"/entities/{made['hrid']}").json()
    assert (entity["values"]["name"], entity["values"]["organism"]) == (
        "ABL1 kinase",
        "Homo sapiens",
    )
    assert Entity.objects.filter(type="protein.Protein").count() == 1
    revisions = api_as(world).get(f"/entities/{made['hrid']}/revisions").json()["items"]
    assert (revisions[0]["number"], revisions[0]["via"]) == (2, "run")
    later, first = api_as(world).get(f"/entities/{made['hrid']}/origins").json()
    assert (later["revision"], later["run"]["id"], first["revision"]) == (2, again["id"], 1)

    # Unchanged, it's found again, as a reused duplicate is: no new revision.
    third, answer = imported(world, lims("ABL1 kinase", organism="Homo sapiens"))
    (effect,) = api_as(world).get(f"/runs/{third['id']}").json()["effects"]
    assert (effect["kind"], effect["revision"]) == ("reused", None)


def test_only_the_runs_projects_own_entity_is_matched(world: World) -> None:
    other = Project.objects.create(team=world.team, name="Enzymes")
    elsewhere = services.create(
        world.users["admin"],
        "protein.Protein",
        other,
        {"name": "Theirs", "sequence": "MKKK", "lims_id": "LIMS-1"},
    ).entity
    run, answer = imported(world, lims("Ours"))
    assert answer["run_status"] == "succeeded", answer
    (effect,) = api_as(world).get(f"/runs/{run['id']}").json()["effects"]
    assert effect["kind"] == "created"
    assert effect["hrid"] != Entity.objects.get(pk=elsewhere.pk).hrid


def test_a_record_claims_the_same_entity_without_one(world: World) -> None:
    # Registered before the LIMS knew it: the same sequence, with no lims_id yet.
    before = services.create(
        world.users["editor"], "protein.Protein", world.project, {"sequence": "MKVLEIC"}
    ).entity
    run, answer = imported(world, lims("ABL1"))
    assert answer["run_status"] == "succeeded", answer
    (effect,) = api_as(world).get(f"/runs/{run['id']}").json()["effects"]
    hrid = Entity.objects.get(pk=before.pk).hrid
    assert (effect["hrid"], effect["kind"], effect["revision"]) == (hrid, "updated", 2)
    assert api_as(world).get(f"/entities/{hrid}").json()["values"]["lims_id"] == "LIMS-1"
    # Then it's that record's: it comes again as a match.
    again, answer = imported(world, lims("ABL1"))
    (effect,) = api_as(world).get(f"/runs/{again['id']}").json()["effects"]
    assert (effect["hrid"], effect["kind"]) == (hrid, "reused")
    assert Entity.objects.filter(type="protein.Protein").count() == 1


def test_two_records_cant_be_one_entity(world: World) -> None:
    run, _ = imported(world, lims("ABL1"))
    (made,) = api_as(world).get(f"/runs/{run['id']}").json()["effects"]
    _, answer = imported(world, lims("ABL1 again", lims_id="LIMS-2"))
    assert answer["run_status"] == "failed"
    assert answer["errors"] == [
        {
            "pointer": "#/creates/0/values/lims_id",
            "detail": f"It has the same sequence as {made['hrid']}, whose lims_id is LIMS-1 "
            "already: they'd be one entity, which can't be two records.",
        }
    ]
    # Nor in one run: the first makes the entity, the second would be it.
    _, answer = imported(world, lims("A", "MKKW", "LIMS-3"), lims("B", "MKKW", "LIMS-4"))
    assert answer["run_status"] == "failed"
    assert [e["pointer"] for e in answer["errors"]] == ["#/creates/1/values/lims_id"]


def test_a_match_that_cant_change_fails_the_run(world: World) -> None:
    run, _ = imported(world, lims("ABL1"))
    (made,) = api_as(world).get(f"/runs/{run['id']}").json()["effects"]
    entity = Entity.objects.get(hrid=made["hrid"])
    services.set_archived(world.users["editor"], entity, True)
    _, answer = imported(world, lims("ABL1 kinase"))
    assert answer["run_status"] == "failed", answer
    failed = Entity.objects.get(hrid=made["hrid"])
    assert failed.revision == entity.revision + 1  # archived, and nothing since
    _, answer = imported(world, lims("ABL1", sequence="MKKK", lims_id="LIMS-1"))
    assert answer["run_status"] == "failed"

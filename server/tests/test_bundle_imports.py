"""Importing bundles: uploaded by an Org Admin who is the project's Admin, checked, and
planned before anything changes (ADR 0096)."""

import contextlib
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import requests
from api_client import Api, problem
from django.db import transaction
from org_world import World, build_world

from iki_server.audit.models import AuditEvent
from iki_server.bundles import export
from iki_server.bundles.models import BundleImport, ImportEntity
from iki_server.entities import registry, services
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
)
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

PUBLIC = "ed25519:riAtz4Nv0SFcXARze-_ieUqB027N1ie892Fk7U0Ao-4"


@pytest.fixture
def world(s3: Any, settings: Any) -> World:
    settings.IKI_BUNDLE_TRUSTED_KEYS = {"test": (PUBLIC,)}
    made = build_world()
    OrgRoleAssignment.objects.create(user=made.users["admin"], role=OrgRole.ORG_ADMIN)
    return made


class _UndoError(Exception):
    pass


def bundle_of(world: World, tmp_path: Path, make: Callable[[], list[Any]]) -> Path:
    """A bundle of what ``make`` makes, which is then undone, as if made elsewhere."""
    out = tmp_path / f"bundle-{len(list(tmp_path.iterdir()))}.tar.gz"
    with contextlib.suppress(_UndoError), transaction.atomic():
        export.write(world.project, make(), out, by=world.users["admin"], reason="r", selection="s")
        raise _UndoError
    return out


def protein(world: World, sequence: str, **values: Any) -> Any:
    return services.create(
        world.users["admin"], "protein.Protein", world.project, {"sequence": sequence, **values}
    ).entity


def run_tasks() -> None:
    while Runner(queues=["default"]).run_ready():
        pass


def upload(api: Api, project: Project, path: Path) -> dict[str, Any]:
    """Uploads a bundle as the web app does, and waits for its plan."""
    data = path.read_bytes()
    started = api.post(
        f"/projects/{project.pk}/imports",
        {"name": path.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    )
    assert started.status_code == 201, started.content
    body = started.json()
    assert body["state"] == "uploading"
    for part in body["parts"]:
        start = (part["number"] - 1) * body["parts"][0]["size"]
        chunk = data[start : start + part["size"]]
        assert requests.put(part["url"], data=chunk, timeout=30).status_code == 200
    assert api.post(f"/imports/{body['id']}/complete").status_code == 202
    run_tasks()
    planned: dict[str, Any] = api.get(f"/imports/{body['id']}").json()
    return planned


def entities_of(api: Api, made: dict[str, Any], **query: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = api.get(f"/imports/{made['id']}/entities", **query).json()[
        "items"
    ]
    return items


def test_a_bundle_of_new_entities_is_planned_and_nothing_changes(
    world: World, tmp_path: Path
) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKTAYIAK", name="ABL1")])
    api = Api(world.browser("admin"))
    before = Entity.objects.count()
    made = upload(api, world.project, path)
    assert made["state"] == "planned", made
    assert made["trusted_as"] == "test"
    assert made["source"]["deployment"] == "test"
    assert made["source"]["exported_by"]["name"] == world.users["admin"].name
    plan = made["plan"]
    assert plan["applicable"] is True and plan["problems"] == []
    assert plan["counts"]["new"] == 1 and plan["counts"]["conflict"] == 0
    assert {t["type"] for t in plan["types"]} >= {"protein.Protein", "core.Sequence"}
    (row,) = entities_of(api, made)
    assert row["action"] == "new" and row["hrid_here"] == row["hrid"]
    assert row["title"] == "ABL1"
    people = api.get(f"/imports/{made['id']}/people").json()["items"]
    assert [p["user"]["id"] for p in people] == [str(world.users["admin"].pk)]
    assert Entity.objects.count() == before
    planned = AuditEvent.objects.get(action="bundle.import_planned")
    assert planned.actor_id == world.users["admin"].pk
    assert planned.data["sha256"] == made["sha256"]


def test_a_taken_hrid_is_renumbered_and_shared_values_are_conflicts(
    world: World, tmp_path: Path
) -> None:
    path = bundle_of(
        world,
        tmp_path,
        lambda: [protein(world, "MKVLAAGG"), protein(world, "MKVLCCDD", lims_id="LIMS-7")],
    )
    first, second = sorted(registry_lines(path), key=lambda e: e["hrid"])
    here = protein(world, "MKVLEEFF", lims_id="LIMS-7")
    Entity.objects.filter(pk=here.pk).update(hrid=first["hrid"])
    here.refresh_from_db()
    same = protein(world, "MKVLAAGG")  # the first one's identity, as another entity
    made = upload(Api(world.browser("admin")), world.project, path)
    rows = {r["hrid"]: r for r in entities_of(Api(world.browser("admin")), made)}
    assert rows[first["hrid"]]["action"] == "conflict"
    assert (
        rows[first["hrid"]]["why"]
        == f"It's the same as {same.hrid} here (their identity fields match)."
    )
    assert rows[second["hrid"]]["action"] == "conflict"
    assert rows[second["hrid"]]["why"] == (
        f"Its Lims id LIMS-7 is {here.hrid}'s here already: no two entities in a project share it."
    )
    assert made["plan"]["applicable"] is False
    assert "2 entities are in conflict" in made["plan"]["problems"][0]


def registry_lines(path: Path) -> list[dict[str, Any]]:
    from iki_server.bundles import reading

    return list(reading.read(path, {}, limit=1024**3).lines("entities.ndjson"))


def test_a_taken_hrid_alone_is_renumbered(world: World, tmp_path: Path) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKQQRRSS")])
    (line,) = registry_lines(path)
    here = protein(world, "MKQQRRTT")
    Entity.objects.filter(pk=here.pk).update(hrid=line["hrid"])
    made = upload(Api(world.browser("admin")), world.project, path)
    (row,) = entities_of(Api(world.browser("admin")), made)
    assert row["action"] == "new" and row["hrid_here"] == ""
    assert row["why"] == f"{line['hrid']} is another entity's here, so it gets a new HRID."
    assert made["plan"]["counts"]["renumbered"] == 1 and made["plan"]["applicable"] is True


def test_entities_here_are_skipped_updated_or_in_conflict(world: World, tmp_path: Path) -> None:
    admin = world.users["admin"]
    kept = protein(world, "MKAAAAAA", name="kept")
    newer = protein(world, "MKBBBBBB", name="newer")
    split = protein(world, "MKCCCCCC", name="split")

    def changed() -> list[Any]:
        for entity in (newer, split):
            services.update(admin, entity, {"name": "changed there"}, expected_revision=1)
        return [kept, newer, split]

    path = bundle_of(world, tmp_path, changed)
    services.update(admin, split, {"name": "changed here"}, expected_revision=1)
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    rows = {r["id"]: r for r in entities_of(api, made)}
    assert (rows[str(kept.pk)]["action"], rows[str(kept.pk)]["why"]) == (
        "skip",
        "Here already, as it is.",
    )
    assert (rows[str(newer.pk)]["action"], rows[str(newer.pk)]["adds"]) == ("update", 1)
    assert rows[str(split.pk)]["action"] == "conflict"
    assert "changed both here and where the bundle is from" in rows[str(split.pk)]["why"]
    assert [r["id"] for r in entities_of(api, made, action="update")] == [str(newer.pk)]


def test_an_entity_of_another_project_is_a_conflict(world: World, tmp_path: Path) -> None:
    mine = protein(world, "MKDDDDDD")
    path = bundle_of(world, tmp_path, lambda: [mine])
    other = Project.objects.create(team=world.team, name="Screens 2")
    ProjectMembership.objects.create(
        project=other, user=world.users["admin"], role=ProjectRole.ADMIN
    )
    made = upload(Api(world.browser("admin")), other, path)
    (row,) = entities_of(Api(world.browser("admin")), made)
    assert row["action"] == "conflict"
    assert row["why"].startswith(f"It's here already as {mine.hrid}, in another project")


def test_types_must_be_here_at_the_bundles_version(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKEEEEEE")])
    real = registry.types()
    monkeypatch.setattr(
        registry, "types", lambda: {k: v for k, v in real.items() if k != "protein.Protein"}
    )
    made = upload(Api(world.browser("admin")), world.project, path)
    assert made["plan"]["applicable"] is False
    assert made["plan"]["problems"][0].startswith(
        "This deployment has no protein.Protein: deploy protein "
    )


def test_an_untrusted_or_broken_bundle_is_refused(
    world: World, tmp_path: Path, settings: Any
) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKFFFFFF")])
    settings.IKI_BUNDLE_TRUSTED_KEYS = {}
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    assert made["state"] == "refused"
    assert made["error"].startswith(f"It's signed with test's key {PUBLIC}, which this deployment")
    assert made["plan"] is None
    assert not ImportEntity.objects.exists()
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a bundle")
    broken = upload(api, world.project, junk)
    assert broken["state"] == "refused"
    assert broken["error"].startswith("It isn't a bundle Iki can read")
    assert AuditEvent.objects.filter(action="bundle.import_refused").count() == 2


def test_importing_needs_an_org_admin_who_is_the_projects_admin(world: World) -> None:
    body = {"name": "b.tar.gz", "size": 10, "sha256": "0" * 64}
    path = f"/projects/{world.project.pk}/imports"
    problem(Api(world.browser("org_admin")).post(path, body), 403, "forbidden")
    OrgRoleAssignment.objects.filter(user=world.users["admin"]).delete()
    problem(Api(world.browser("admin")).post(path, body), 403, "forbidden")
    problem(Api(world.browser("admin")).get(path), 403, "forbidden")


def test_a_discarded_import_keeps_its_record_and_not_its_archive(
    world: World, tmp_path: Path, django_capture_on_commit_callbacks: Any
) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKGGGGGG")])
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    kept = BundleImport.objects.get(pk=made["id"])
    key = storage.imported_key(kept.upload_id)
    assert storage.exists(key)
    # Discarding deletes the archive once it's committed.
    with django_capture_on_commit_callbacks(execute=True):
        assert api.delete(f"/imports/{made['id']}").status_code == 204
    assert not storage.exists(key)
    assert api.get(f"/imports/{made['id']}").json()["state"] == "discarded"
    assert not ImportEntity.objects.filter(bundle_import=kept).exists()
    assert AuditEvent.objects.filter(action="bundle.import_discarded").exists()
    other = Api(world.browser("editor"))
    problem(other.get(f"/imports/{made['id']}"), 404, "not_found")

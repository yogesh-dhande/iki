"""Exporting bundles from Iki: asked for by a project's Admin, written in the background,
downloaded for a week, and in the audit log (ADR 0096)."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from api_client import Api, problem
from django.core.management import call_command
from django.utils import timezone
from org_world import World, build_world
from test_file_fields import add_file
from test_oauth import bearer, signed_in_token

from iki_server.audit.models import AuditEvent
from iki_server.bundles import exports, reading
from iki_server.bundles.models import BundleExport
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.org import holds
from iki_server.org.models import Project, ProjectRole
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

TRUSTED = {"test": ("ed25519:riAtz4Nv0SFcXARze-_ieUqB027N1ie892Fk7U0Ao-4",)}


@pytest.fixture
def world(s3: Any) -> World:
    return build_world()


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def made(world: World, values: dict[str, Any], type_name: str = "acme/protein.Protein") -> Any:
    return services.create(world.users["admin"], type_name, world.project, values).entity


def collection(world: World, members: list[Any]) -> Any:
    return made(
        world,
        {"name": "Validated kinases", "members": [str(m.pk) for m in members]},
        "iki/core.Collection",
    )


def test_an_admin_exports_a_collection_and_downloads_it(world: World, tmp_path: Path) -> None:
    first = made(world, {"sequence": "MKT", "name": "ABL1"})
    second = made(world, {"sequence": "MKV", "name": "SRC"})
    kinases = collection(world, [first, second])
    api = Api(world.browser("admin"))
    response = api.post(
        f"/projects/{world.project.pk}/bundles",
        {"collection": kinases.hrid.lower(), "reason": "Promote to production"},
    )
    assert response.status_code == 202, response.content
    started = response.json()
    assert started["state"] == "queued"
    assert started["selection"] == f"{kinases.hrid} (Validated kinases), with its members"
    assert started["requested_by"]["name"] == world.users["admin"].name
    asked = AuditEvent.objects.get(action="bundle.export_requested")
    assert asked.actor_id == world.users["admin"].pk
    assert asked.data["reason"] == "Promote to production"
    assert asked.via == "web"

    assert run_tasks() == 1
    ready = api.get(f"/bundles/{started['id']}").json()
    assert ready["state"] == "ready", ready
    assert ready["counts"] == {"entities": 3, "revisions": 3, "files": 0, "outside": 0, "runs": 0}
    assert ready["file_name"].endswith(".iki-bundle.tar.gz")
    expires = datetime.fromisoformat(ready["expires_at"])
    assert timedelta(days=6, hours=23) < expires - timezone.now() <= timedelta(days=7)
    listed = api.get(f"/projects/{world.project.pk}/bundles").json()["items"]
    assert [e["id"] for e in listed] == [started["id"]]

    download = api.get(f"/bundles/{started['id']}/content")
    assert download.status_code == 302
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(requests.get(download["Location"], timeout=30).content)
    bundle = reading.read(archive, TRUSTED, limit=1024**3)
    assert bundle.sha256 == ready["sha256"]
    assert {e["hrid"] for e in bundle.lines("entities.ndjson")} == {
        kinases.hrid,
        first.hrid,
        second.hrid,
    }
    written = AuditEvent.objects.get(action="bundle.exported")
    assert written.actor_id == world.users["admin"].pk
    assert written.data["export"] == started["id"]
    assert written.data["sha256"] == ready["sha256"]
    downloaded = AuditEvent.objects.get(action="bundle.downloaded")
    assert downloaded.data == {"export": started["id"], "sha256": ready["sha256"]}


def test_each_download_checks_the_records_in_the_retained_archive(world: World) -> None:
    admin = world.users["admin"]
    other = Project.objects.create(team=world.team, name="Private reagents")
    membership = other.memberships.create(user=admin, role=ProjectRole.ADMIN)
    private = services.create(admin, "acme/protein.Lot", other, {"label": "Reagent"}).entity
    assert isinstance(private, Entity)
    picked = made(world, {"sequence": "MAAA", "source": str(private.pk)})
    services.update(admin, picked, {"source": None}, expected_revision=1)
    world.project.memberships.create(user=world.users["outsider"], role=ProjectRole.ADMIN)
    started = exports.start(admin, world.project, entities=[picked.hrid], reason="Archive")
    run_tasks()
    path = f"/bundles/{started.pk}/content"
    owner = Api(world.browser("admin"))
    assert owner.get(path).status_code == 302
    denied = problem(Api(world.browser("outsider")).get(path), 409, "refused")
    assert str(private.pk) not in str(denied) and private.hrid not in str(denied)
    membership.delete()
    problem(owner.get(path), 409, "refused")
    assert AuditEvent.objects.filter(action="bundle.downloaded", target_id=started.pk).count() == 1
    other.memberships.create(user=admin, role=ProjectRole.VIEWER)
    assert owner.get(path).status_code == 302
    BundleExport.objects.filter(pk=started.pk).update(references=None)
    assert "fresh export" in problem(owner.get(path), 409, "refused")["detail"]


def test_export_selection_names_a_view_for_readers_of_its_targets(world: World) -> None:
    admin = world.users["admin"]
    other = Project.objects.create(team=world.team, name="Targets")
    other.memberships.create(user=admin, role=ProjectRole.ADMIN)
    world.project.memberships.create(user=world.users["outsider"], role=ProjectRole.ADMIN)
    target = services.create(
        admin, "acme/protein.ProteinStructure", other, {"structure": add_file(other).pk}
    ).entity
    view = made(
        world,
        {
            "name": "Structure",
            "targets": [str(target.pk)],
            "target_revisions": [1],
            "viewers": ["structure"],
            "state": {"version": 1},
        },
        "iki/core.View",
    )
    started = exports.start(admin, world.project, entities=[view.hrid], reason="Keep the view")
    path = f"/bundles/{started.pk}"
    assert Api(world.browser("admin")).get(path).json()["selection"] == view.hrid
    reader = Api(world.browser("outsider"))
    for detail in (reader.get(path), reader.get(f"/projects/{world.project.pk}/bundles")):
        assert detail.status_code == 200
        assert (
            view.hrid not in detail.content.decode() and str(view.pk) not in detail.content.decode()
        )
    assert reader.get(path).json()["selection"] == "Selected records"


def test_a_dynamic_collection_takes_what_its_search_finds_now(world: World) -> None:
    made(world, {"sequence": "MKT", "name": "ABL1"})
    search = {"project": str(world.project.pk), "type": "acme/protein.Protein"}
    dynamic = made(world, {"name": "All proteins", "search": search}, "iki/core.Collection")
    started = exports.start(
        world.users["admin"], world.project, reason="r", collection=str(dynamic.pk)
    )
    assert started.selection == f"{dynamic.hrid} (All proteins), with the 1 it finds now"
    assert len(started.picked) == 2  # the collection, and the protein it finds


def test_exports_need_an_admin_a_reason_and_entities_of_the_project(world: World) -> None:
    protein = made(world, {"sequence": "MKT"})
    path = f"/projects/{world.project.pk}/bundles"
    editor = Api(world.browser("editor"))
    problem(editor.post(path, {"entities": [protein.hrid], "reason": "r"}), 403, "forbidden")
    admin = Api(world.browser("admin"))
    body = problem(
        admin.post(path, {"entities": [protein.hrid], "reason": " "}), 422, "validation_failed"
    )
    assert body["errors"][0]["pointer"] == "#/reason"
    body = problem(admin.post(path, {"reason": "r"}), 422, "validation_failed")
    assert body["errors"][0]["detail"] == "Pick a collection, or entities, to export."
    body = problem(
        admin.post(path, {"entities": ["PRT-999999"], "reason": "r"}), 422, "validation_failed"
    )
    assert body["errors"][0]["detail"] == "There's no entity PRT-999999 you can read."
    body = problem(
        admin.post(path, {"collection": protein.hrid, "reason": "r"}), 422, "validation_failed"
    )
    assert body["errors"][0]["detail"] == f"{protein.hrid} isn't a collection."
    assert not BundleExport.objects.exists()
    # Others can't see the export, or that it exists.
    started = exports.start(
        world.users["admin"], world.project, reason="r", entities=[protein.hrid]
    )
    problem(editor.get(f"/bundles/{started.pk}"), 404, "not_found")
    problem(editor.get(f"/bundles/{started.pk}/content"), 404, "not_found")
    body = problem(admin.get(f"/bundles/{started.pk}/content"), 409, "refused")
    assert body["detail"] == "It isn't ready yet."


def test_a_token_needs_the_admin_scope_to_export(world: World) -> None:
    protein = made(world, {"sequence": "MKT"})
    path = f"/projects/{world.project.pk}/bundles"
    body = {"entities": [protein.hrid], "reason": "For production"}
    _, tokens = signed_in_token(world, "admin", scope="read write", projects=[world.project])
    problem(bearer(tokens["access_token"], "POST", path, body), 403, "insufficient_scope")
    _, tokens = signed_in_token(world, "admin", scope="read admin", projects=[world.project])
    assert bearer(tokens["access_token"], "POST", path, body).status_code == 202


def test_a_failed_export_says_why(world: World, settings: Any) -> None:
    protein = made(world, {"sequence": "MKT"})
    started = exports.start(
        world.users["admin"], world.project, reason="r", entities=[protein.hrid]
    )
    settings.IKI_BUNDLE_SIGNING_KEY = ""
    run_tasks()
    started.refresh_from_db()
    assert started.state == "failed"
    assert "no bundle signing key" in started.error
    assert not AuditEvent.objects.filter(action="bundle.exported").exists()


def test_archives_expire_after_a_week_unless_the_project_is_held(world: World) -> None:
    protein = made(world, {"sequence": "MKT"})
    admin = world.users["admin"]
    started = exports.start(admin, world.project, reason="r", entities=[protein.hrid])
    run_tasks()
    key = storage.bundle_key(started.pk)
    assert storage.exists(key)
    BundleExport.objects.filter(pk=started.pk).update(expires_at=timezone.now())
    hold = holds.place(world.users["org_admin"], world.project, "Matter 12")
    call_command("maintain")
    assert storage.exists(key)  # held
    holds.release(world.users["org_admin"], hold, reason="Matter resolved")
    call_command("maintain")
    started.refresh_from_db()
    assert started.state == "expired"
    assert not storage.exists(key)
    body = problem(
        Api(world.browser("admin")).get(f"/bundles/{started.pk}/content"), 409, "refused"
    )
    assert body["detail"] == "It expired: its archive was deleted. Export again."


def test_an_archived_project_can_still_be_exported(world: World) -> None:
    assert (
        Api(world.browser("admin")).get(f"/projects/{world.archived.pk}/bundles").status_code == 200
    )

"""Importing bundles: uploaded by an Org Admin who is the project's Admin, checked, and
planned before anything changes (ADR 0096)."""

import contextlib
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import requests
from api_client import Api, problem
from django.db import transaction
from django.db.utils import ProgrammingError
from org_world import World, build_world

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.bundles import export
from iki_server.bundles.models import BundleImport, ImportEntity
from iki_server.entities import registry, services
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.files.models import Blob, File
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
        world.users["admin"],
        "acme/protein.Protein",
        world.project,
        {"sequence": sequence, **values},
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
    assert {t["type"] for t in plan["types"]} >= {"acme/protein.Protein", "iki/core.Sequence"}
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
    # Its registry entry's HRID (ADR 0115), which its record has too.
    Entity.objects.filter(pk__in=[here.pk, here.entry_id]).update(hrid=first["hrid"])
    here.refresh_from_db()
    same = protein(world, "MKVLAAGG")  # the first one's identity, as another entity
    made = upload(Api(world.browser("admin")), world.project, path)
    rows = {r["hrid"]: r for r in entities_of(Api(world.browser("admin")), made)}
    assert rows[first["hrid"]]["action"] == "conflict"
    assert rows[first["hrid"]]["why"] == (
        f"Kinase Atlas has {same.hrid} already, a record of the same registry entry: a "
        "project has one record of each."
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
    # Its registry entry's HRID (ADR 0115), which its record has too.
    Entity.objects.filter(pk__in=[here.pk, here.entry_id]).update(hrid=line["hrid"])
    made = upload(Api(world.browser("admin")), world.project, path)
    (row,) = entities_of(Api(world.browser("admin")), made)
    assert row["action"] == "new" and row["hrid_here"] == ""
    assert row["why"] == (
        f"{line['hrid']} is another entity's HRID here: it's registered with a new one."
    )
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
        registry, "types", lambda: {k: v for k, v in real.items() if k != "acme/protein.Protein"}
    )
    made = upload(Api(world.browser("admin")), world.project, path)
    assert made["plan"]["applicable"] is False
    assert made["plan"]["problems"][0].startswith(
        "This deployment has no acme/protein.Protein: deploy acme/protein "
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


# --- Applying ----------------------------------------------------------------------------


PDB = b"HEADER    TRANSFERASE\nATOM      1  N   MET A   1      11.104  13.207   2.100\nEND\n"


def applied(api: Api, made: dict[str, Any], reason: str = "Moving the kinase work here") -> Any:
    response = api.post(f"/imports/{made['id']}/apply", {"reason": reason})
    assert response.status_code == 202, response.content
    assert response.json()["state"] == "applying"
    run_tasks()
    return api.get(f"/imports/{made['id']}").json()


def test_applying_makes_its_entities_with_their_history_files_and_authors(
    world: World, tmp_path: Path
) -> None:
    admin = world.users["admin"]
    sha = hashlib.sha256(PDB).hexdigest()

    def elsewhere() -> list[Any]:
        chemist = User.objects.create_user("rosalind@lab-a.example", "Rosalind Franklin")
        ProjectMembership.objects.create(
            project=world.project, user=chemist, role=ProjectRole.EDITOR
        )
        kinase = services.create(
            chemist,
            "acme/protein.Protein",
            world.project,
            {"sequence": "MKTAYIAKQR", "name": "ABL1"},
            reason="From the LIMS",
        ).entity
        kinase = Entity.objects.get(pk=kinase.pk)
        services.update(admin, kinase, {"name": "ABL1 kinase"}, expected_revision=1, reason="Named")
        key = storage.blob_key(sha)
        storage.client().put_object(Bucket=storage.bucket(), Key=key, Body=PDB)
        blob = Blob.objects.create(sha256=sha, size=len(PDB), storage_uri=storage.uri(key))
        pdb = File.objects.create(
            blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=world.project
        )
        structure = services.create(
            chemist,
            "acme/protein.ProteinStructure",
            world.project,
            {"structure": str(pdb.pk), "method": "xray", "chains": [str(kinase.pk)]},
        ).entity
        return [Entity.objects.get(pk=kinase.pk), Entity.objects.get(pk=structure.pk)]

    path = bundle_of(world, tmp_path, elsewhere)
    storage.client().delete_object(Bucket=storage.bucket(), Key=storage.blob_key(sha))
    lines = {e["type"]: e for e in registry_lines(path)}
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    assert made["plan"]["applicable"], made["plan"]
    people = {p["email"]: p for p in api.get(f"/imports/{made['id']}/people").json()["items"]}
    assert people["rosalind@lab-a.example"]["placeholder"] is True
    assert people[admin.email]["placeholder"] is False

    done = applied(api, made)
    assert done["state"] == "applied", done
    assert done["reason"] == "Moving the kinase work here"
    assert done["applied_by"]["id"] == str(admin.pk)

    # Each entity has its ID, HRID, revisions and values, as they were.
    kinase_line = lines["acme/protein.Protein"]
    kinase = Entity.objects.get(pk=kinase_line["id"])
    assert kinase.hrid == kinase_line["hrid"]
    assert kinase.owner_project_id == world.project.pk
    revisions = list(kinase.revisions.order_by("number"))
    assert [(r.number, r.reason) for r in revisions] == [(1, "From the LIMS"), (2, "Named")]
    chemist = revisions[0].user
    assert chemist is not None and chemist.is_placeholder and not chemist.is_active
    assert (chemist.email, chemist.imported_from) == ("rosalind@lab-a.example", "test")
    assert revisions[1].user_id == admin.pk
    assert [r.created_at.isoformat() for r in revisions] == [
        r["created_at"] for r in sorted(kinase_line["revisions"], key=lambda r: r["number"])
    ]
    body = api.get(f"/entities/{kinase.hrid}").json()
    assert body["values"]["name"] == "ABL1 kinase" and body["revision"] == 2
    history = api.get(f"/entities/{kinase.hrid}/revisions").json()["items"]
    assert len(history) == 2
    # Its file's bytes came in the bundle.
    structure = Entity.objects.get(pk=lines["acme/protein.ProteinStructure"]["id"])
    detail = api.get(f"/entities/{structure.hrid}").json()
    assert detail["values"]["chains"] == [str(kinase.entry_id)]
    content = api.get(f"/files/{detail['values']['structure']}/content")
    assert content.status_code == 302
    assert requests.get(content["Location"], timeout=30).content == PDB
    # Its page says where it's from.
    origins = api.get(f"/entities/{kinase.hrid}/bundles")
    assert origins.status_code == 200, origins.content
    (origin,) = origins.json()
    assert origin["deployment"] == "test" and origin["hrid"] == kinase.hrid
    assert origin["action"] == "new" and origin["revisions"] == 2
    assert origin["bundle_import"] == made["id"]
    # Nobody else gets its number.
    assert protein(world, "MKNEWONE").hrid > kinase.hrid
    event = AuditEvent.objects.get(action="bundle.imported")
    assert event.actor_id == admin.pk
    assert event.data["created"] == 2 and event.data["placeholders"] == 1
    assert event.data["files"] == 1 and event.data["reason"] == "Moving the kinase work here"
    problem(api.delete(f"/imports/{made['id']}"), 409, "refused")


def test_applying_renumbers_taken_hrids_and_adds_later_revisions(
    world: World, tmp_path: Path
) -> None:
    admin = world.users["admin"]
    here = protein(world, "MKHERE", name="here")

    def changed() -> list[Any]:
        services.update(admin, here, {"name": "changed there"}, expected_revision=1)
        return [Entity.objects.get(pk=here.pk), protein(world, "MKRENUMBER")]

    path = bundle_of(world, tmp_path, changed)
    renumbered = next(e for e in registry_lines(path) if e["id"] != str(here.pk))
    taken = protein(world, "MKTAKEN")
    # Its registry entry's HRID (ADR 0115), which its record has too.
    Entity.objects.filter(pk__in=[taken.pk, taken.entry_id]).update(hrid=renumbered["hrid"])
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    here.refresh_from_db()
    assert here.revision == 2
    assert api.get(f"/entities/{here.hrid}").json()["values"]["name"] == "changed there"
    moved = Entity.objects.get(pk=renumbered["id"])
    assert moved.hrid != renumbered["hrid"]
    (origin,) = api.get(f"/entities/{moved.hrid}/bundles").json()
    assert origin["hrid"] == renumbered["hrid"]
    rows = {r["id"]: r for r in entities_of(api, done)}
    assert rows[renumbered["id"]]["hrid_here"] == moved.hrid
    assert rows[str(here.pk)]["action"] == "update"


def test_a_record_arrives_in_its_states_with_their_changes(world: World, tmp_path: Path) -> None:
    """ADR 0125: a bundle carries each record's state fields as values and each change to
    them as a revision, and a state that locks it locks it here."""
    from iki_server.entities import states
    from iki_server.org.models import LabRoleGrant

    admin = world.users["admin"]
    grant = world.project.memberships.get(user=admin)
    for role in ("acme/protein.analyst", "acme/protein.reviewer"):
        LabRoleGrant.objects.create(membership=grant, role=role, granted_by=admin)
    editor = world.users["editor"]
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=editor),
        role="acme/protein.analyst",
        granted_by=admin,
    )
    from iki_server.org import permissions

    permissions.forget(admin)
    permissions.forget(editor)

    def tested() -> list[Any]:
        made = services.create(admin, "acme/protein.Sample", world.project, {"label": "S-1"})
        sample = Entity.objects.get(pk=made.entity.pk)
        states.set_state(editor, sample, "status", "in_testing", expected_revision=1)
        states.set_state(editor, sample, "status", "in_review", expected_revision=2)
        states.set_state(
            admin, sample, "status", "reviewed", expected_revision=3, reason="Looks right"
        )
        return [Entity.objects.get(pk=sample.pk)]

    path = bundle_of(world, tmp_path, tested)
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    (row,) = entities_of(api, done)
    here = api.get(f"/entities/{row['hrid_here']}").json()
    assert (here["values"]["status"], here["locked_by"], here["revision"]) == (
        "reviewed",
        ["status"],
        4,
    )
    history = api.get(f"/entities/{row['hrid_here']}/revisions").json()["items"]
    assert [
        (r["state"]["field"]["name"], r["state"]["to"]["name"]) if r["state"] else None
        for r in history
    ] == [("status", "reviewed"), ("status", "in_review"), ("status", "in_testing"), None]
    assert history[0]["reason"] == "Looks right"
    # Locked here as it was there.
    changed = api.patch(
        f"/entities/{row['hrid_here']}",
        {"values": {"notes": "x"}},
        headers={"If-Match": '"4"'},
    )
    assert changed.status_code == 409, changed.content


def test_the_people_a_record_names_come_with_it(world: World, tmp_path: Path) -> None:
    """ADR 0122: whom a record names is kept, as they are here: the same person by their
    email address, or a placeholder, whom the record needs reassigning from."""
    from iki_server.org.models import LabRoleGrant

    admin = world.users["admin"]
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["editor"]),
        role="acme/protein.analyst",
        granted_by=admin,
    )

    def named() -> list[Any]:
        elsewhere = User.objects.create_user("rosalind@lab-a.example", "Rosalind Franklin")
        world.project.memberships.create(user=elsewhere, role=ProjectRole.EDITOR)
        made = services.create(
            admin,
            "acme/protein.Sample",
            world.project,
            {
                "label": "S-1",
                "analyst": str(world.users["editor"].pk),
                "received_by": str(elsewhere.pk),
                "helpers": [str(elsewhere.pk), str(admin.pk)],
            },
        )
        return [made.entity]

    path = bundle_of(world, tmp_path, named)
    assert not User.objects.filter(email="rosalind@lab-a.example").exists()
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    (row,) = entities_of(api, done)
    here = api.get(f"/entities/{row['hrid_here']}").json()
    placeholder = User.objects.get(email="rosalind@lab-a.example")
    assert not placeholder.is_active
    values = here["values"]
    assert values["analyst"] == str(world.users["editor"].pk)
    assert values["received_by"] == str(placeholder.pk)
    assert values["helpers"] == [str(placeholder.pk), str(admin.pk)]
    # Who isn't here can't be assigned it: the record says so.
    assert [s["field"] for s in here["needs_reassigning"]] == ["helpers"]


def test_a_view_comes_with_the_entities_it_shows_each_at_its_revision(
    world: World, tmp_path: Path
) -> None:
    """A view of several entities (ADR 0110) goes with them, and arrives showing each at the
    revision it pinned, with what its viewers kept and its own history."""
    admin = world.users["admin"]

    def elsewhere() -> list[Any]:
        kinase = protein(world, "MKVIEWKINASE", name="kinase")
        antigen = protein(world, "MKVIEWANTIGEN", name="antigen")
        services.update(admin, antigen, {"name": "antigen, renamed"}, expected_revision=1)
        values = {
            "name": "Kinase with its antigen",
            "targets": [kinase.pk, antigen.pk],
            "target_revisions": [1, 1],
            "viewers": ["sequence", "sequence"],
            "state": {"version": 2, "targets": [{"label": "kinase"}, {}]},
        }
        view = cast(Entity, services.create(admin, "iki/core.View", world.project, values).entity)
        services.update(admin, view, {"target_revisions": [1, 2]}, expected_revision=1)
        return [Entity.objects.get(pk=view.pk)]

    path = bundle_of(world, tmp_path, elsewhere)
    lines = {e["type"]: e for e in registry_lines(path) if e["type"] == "iki/core.View"}
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    view = Entity.objects.get(pk=lines["iki/core.View"]["id"])
    body = api.get(f"/entities/{view.hrid}").json()
    shown = [body["refs"][t]["title"] for t in body["values"]["targets"]]
    assert shown == ["kinase", "antigen, renamed"]
    assert body["values"]["target_revisions"] == [1, 2]
    assert body["values"]["state"]["targets"][0] == {"label": "kinase"}
    assert body["revision"] == 2
    first = api.get(f"/entities/{view.hrid}/revisions/1").json()
    assert first["values"]["target_revisions"] == [1, 1]


def test_what_changed_since_it_was_planned_stops_it(world: World, tmp_path: Path) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKDRIFT")])
    (line,) = registry_lines(path)
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    taken = protein(world, "MKSLIPPEDIN")
    # Its registry entry's HRID (ADR 0115), which its record has too.
    Entity.objects.filter(pk__in=[taken.pk, taken.entry_id]).update(hrid=line["hrid"])
    done = applied(api, made)
    assert done["state"] == "planned"
    assert done["error"].startswith("Something here changed since it was planned")
    assert done["plan"]["counts"]["renumbered"] == 1
    assert not Entity.objects.filter(pk=line["id"]).exists()
    # Applying the new plan imports it.
    assert applied(api, done)["state"] == "applied"
    assert Entity.objects.filter(pk=line["id"]).exists()


def test_applying_needs_a_reason_and_a_plan_that_can_be(world: World, tmp_path: Path) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKREASON")])
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    problem(api.post(f"/imports/{made['id']}/apply", {"reason": "  "}), 422, "validation_failed")
    BundleImport.objects.filter(pk=made["id"]).update(plan={**made["plan"], "applicable": False})
    problem(api.post(f"/imports/{made['id']}/apply", {"reason": "go"}), 409, "refused")
    problem(
        Api(world.browser("editor")).post(f"/imports/{made['id']}/apply", {"reason": "go"}),
        404,
        "not_found",
    )


# --- Runs --------------------------------------------------------------------------------


def ran(world: World, made: Any, read: Any, *, hrid: str = "RUN-009001") -> Any:
    """A finished run that read ``read`` and made ``made``, as a worker leaves it."""
    from django.utils import timezone

    from iki_server.runs.models import (
        ActionRun,
        Job,
        JobLog,
        Lineage,
        RunEffect,
        RunInput,
        RunOutput,
    )

    now = timezone.now()
    run = ActionRun.objects.create(
        hrid=hrid,
        action="acme/protein.compute_properties",
        plugin_version="1.0.0",
        plugin_sha="a" * 40,
        spec={"name": "compute_properties"},
        project=world.project,
        requested_by=world.users["admin"],
        via="web",
        reason="Weigh them",
        tier="backend",
        environment="python",
        params={"window": 9},
        seed=42,
        status="succeeded",
        started_at=now,
        finished_at=now,
        timeout=600,
        max_attempts=1,
    )
    job = Job.objects.create(
        run=run,
        attempt=1,
        state="succeeded",
        project=world.project,
        tier="backend",
        environment="acme/protein.python",
        started_at=now,
        finished_at=now,
    )
    JobLog.objects.create(job=job, offset=0, text="Weighing ABL1\n")
    RunOutput.objects.create(run=run, job=job, document={"creates": []})
    RunInput.objects.create(run=run, name="protein", position=0, entity_id=read.pk, revision=1)
    RunEffect.objects.create(run=run, entity_id=made.pk, kind="created", revision=1)
    Lineage.objects.create(
        entity_id=made.pk, parent_id=read.pk, parent_revision=1, run=run, input="protein"
    )
    return run


def test_runs_come_with_what_they_made_and_read(world: World, tmp_path: Path) -> None:
    from iki_server.runs.models import (
        ActionRun,
        EntityMaking,
        ExternalOrigin,
        JobLog,
        Lineage,
        RunEffect,
        RunInput,
    )

    said = {"kind": "computational", "software": [{"name": "AlphaFold", "version": "2.3.2"}]}

    def elsewhere() -> list[Any]:
        read = protein(world, "MKREADIN", name="read")
        made = protein(world, "MKMADEBY", name="made")
        run = ran(world, made, read)
        # How it was made (ADR 0106): Iki's record, and its source's word.
        origin = ExternalOrigin.objects.create(
            run=run, entity_id=made.pk, revision=1, source="AlphaFold DB", identifier="AF-1"
        )
        origin.made = said
        origin.save(update_fields=["made"])
        from iki_server.runs import makings
        from iki_spec.model import Software

        makings.record(run.pk, made.pk, [Software("PDBFixer", "1.12")], origin)
        return [made]  # what it read comes too

    path = bundle_of(world, tmp_path, elsewhere)
    from iki_server.bundles import reading

    bundle = reading.read(path, {}, limit=1024**3)
    assert bundle.manifest["counts"]["entities"] == 2
    assert bundle.manifest["counts"]["runs"] == 1
    (line,) = bundle.lines("runs.ndjson")
    assert line["attempts"][0]["log"] == "Weighing ABL1\n"
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    assert made["plan"]["counts"]["runs"] == 1, made["plan"]
    done = applied(api, made)
    assert done["state"] == "applied", done
    run = ActionRun.objects.get(pk=line["id"])
    assert (run.hrid, run.status, run.seed, run.reason) == (
        "RUN-009001",
        "succeeded",
        42,
        "Weigh them",
    )
    assert run.project_id == world.project.pk
    assert run.imported["from"] == "test" and run.imported["inputs"] == []
    (job,) = run.jobs.all()
    assert JobLog.objects.get(job=job).text == "Weighing ABL1\n"
    assert run.output.document == {"creates": []}
    assert RunInput.objects.filter(run=run).count() == 1
    assert RunEffect.objects.filter(run=run, kind="created").count() == 1
    assert Lineage.objects.filter(run=run).count() == 1
    assert ExternalOrigin.objects.get(run=run).made == said
    assert sorted(
        EntityMaking.objects.filter(run=run).values_list("by", "software", "version")
    ) == [("iki", "PDBFixer", "1.12"), ("source", "AlphaFold", "2.3.2")]
    body = api.get(f"/runs/{run.hrid}").json()
    assert body["imported"]["deployment"] == "test"
    assert body["imported"]["hrid"] == "RUN-009001"
    assert body["imported"]["bundle_import"] == made["id"]
    # Runs here later aren't given its number.
    from iki_server.runs import services as run_services

    assert run_services._next_hrid() > "RUN-009001"


def reading_runs(path: Path) -> list[dict[str, Any]]:
    from iki_server.bundles import reading

    return list(reading.read(path, {}, limit=1024**3).lines("runs.ndjson"))


def test_a_run_whose_input_isnt_coming_keeps_it_on_record(world: World, tmp_path: Path) -> None:
    from iki_server.runs.models import ActionRun, RunInput

    other = Project.objects.create(team=world.team, name="Screens 3")
    ProjectMembership.objects.create(
        project=other, user=world.users["admin"], role=ProjectRole.ADMIN
    )

    def elsewhere() -> list[Any]:
        read = services.create(
            world.users["admin"], "acme/protein.Protein", other, {"sequence": "MKOTHER"}
        ).entity
        made = protein(world, "MKMADEHERE")
        ran(world, made, read, hrid="RUN-009002")
        return [made]

    path = bundle_of(world, tmp_path, elsewhere)
    (line,) = reading_runs(path)
    mine = protein(world, "MKMINEHERE")
    ran(world, mine, mine, hrid="RUN-009002")  # its HRID is another run's here
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    assert made["plan"]["applicable"], made["plan"]
    assert made["plan"]["outside"]["items"][0]["why"] == "run"
    assert made["plan"]["counts"]["runs_renumbered"] == 1
    done = applied(api, made)
    assert done["state"] == "applied", done
    run = ActionRun.objects.get(pk=line["id"])
    assert run.hrid != "RUN-009002" and run.imported["hrid"] == "RUN-009002"
    assert not RunInput.objects.filter(run=run).exists()
    elsewhere_inputs = api.get(f"/runs/{run.hrid}").json()["imported"]["inputs_elsewhere"]
    assert [i["name"] for i in elsewhere_inputs] == ["protein"]


def test_an_unexpected_error_imports_nothing_and_says_so(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iki_server.bundles import applying

    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKCRASH")])
    (line,) = registry_lines(path)
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)

    def crash(self: Any) -> dict[str, int]:
        raise RuntimeError("boom")

    monkeypatch.setattr(applying._Writer, "write", crash)
    done = applied(api, made)
    assert done["state"] == "failed"
    assert done["error"] == applying.UNEXPECTED
    assert not Entity.objects.filter(pk=line["id"]).exists()
    assert AuditEvent.objects.filter(action="bundle.import_failed").exists()


def test_an_apps_viewer_names_a_renumbered_entity_by_its_hrid_here(
    world: World, tmp_path: Path
) -> None:
    admin = world.users["admin"]

    def elsewhere() -> list[Any]:
        shown = protein(world, "MKSHOWN", name="shown")
        spec = {
            "version": 1,
            "panels": [
                {
                    "id": "shown",
                    "component": "iki.viewer",
                    "props": {"entity": shown.hrid, "viewer": "sequence"},
                }
            ],
        }
        app = services.create(
            admin, "iki/core.App", world.project, {"name": "Kinase", "spec": spec}
        ).entity
        return [Entity.objects.get(pk=app.pk), shown]

    path = bundle_of(world, tmp_path, elsewhere)
    lines = {e["type"]: e for e in registry_lines(path)}
    source_hrid = lines["acme/protein.Protein"]["hrid"]
    taken = protein(world, "MKTAKENHERE")
    # Its registry entry's HRID (ADR 0115), which its record has too.
    Entity.objects.filter(pk__in=[taken.pk, taken.entry_id]).update(hrid=source_hrid)
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    shown = Entity.objects.get(pk=lines["acme/protein.Protein"]["id"])
    assert shown.hrid != source_hrid
    app = Entity.objects.get(pk=lines["iki/core.App"]["id"])
    body = api.get(f"/entities/{app.hrid}").json()
    assert body["values"]["spec"]["panels"][0]["props"]["entity"] == shown.hrid
    assert body["revision"] == 2
    latest = app.revisions.order_by("-number").first()
    assert latest is not None and latest.user_id == admin.pk
    assert (
        latest.reason
        == f"Imported from test: its viewers name {source_hrid} as {shown.hrid}, its HRID here."
    )
    assert AuditEvent.objects.get(action="bundle.imported").data["apps_renamed"] == 1
    # What refers to it says what it was called there, so files made there still find it.
    referrer = protein(world, "MKREFERS", source=str(shown.pk))
    refs = api.get(f"/entities/{referrer.hrid}").json()["refs"]
    # It refers to the registry entry (ADR 0115), which had that HRID there too.
    assert refs[str(shown.entry_id)]["hrid"] == shown.hrid
    assert refs[str(shown.entry_id)]["formerly"] == [source_hrid]


def test_hrid_sequences_move_past_kept_hrids_as_the_app_role() -> None:
    """The app's role has only USAGE on sequences (ADR 0025): moving one past an HRID an
    import kept works with that, and never moves it back."""
    from io import StringIO

    from django.core.management import call_command
    from django.db import connection

    from iki_server.bundles import applying
    from iki_server.runs.models import HRID_SEQUENCE

    role = "iki_test_bundle_app"
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {role} NOLOGIN")
    call_command("grant_app_privileges", "--role", role, stdout=StringIO())
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {role}")
        cursor.execute("SELECT nextval(%s::regclass)", [HRID_SEQUENCE])
        start = cursor.fetchone()[0]
        # However far off, at once: a bundle can't make it draw numbers one by one.
        applying._advance(HRID_SEQUENCE, start + 10**12)
        applying._advance(HRID_SEQUENCE, start + 10)  # behind it already: only draws one
        cursor.execute("SELECT nextval(%s::regclass)", [HRID_SEQUENCE])
        assert cursor.fetchone()[0] == start + 10**12 + 2
        # It moves HRID sequences and nothing else, and the app can't setval itself.
        with pytest.raises(ProgrammingError, match="not an HRID sequence"), transaction.atomic():
            cursor.execute("SELECT iki_hrid_advance(%s, %s)", ["iki_audit_event_id_seq", 10])
        with pytest.raises(ProgrammingError, match="permission denied"), transaction.atomic():
            cursor.execute("SELECT setval(%s::regclass, 10)", [HRID_SEQUENCE])
        cursor.execute("RESET ROLE")

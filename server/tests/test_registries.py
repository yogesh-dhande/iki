"""Registries (ADR 0115): an identity is registered once, as a registry entry with one ID;
each project keeps its own record of it. What another project knows never leaks: a
registration tells only that the identity exists and its ID, an entry's ID grants
nothing, registry facts change only by registrars, and registries are kept apart."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world, post

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity
from iki_server.notifications.models import Notification
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Registry

pytestmark = pytest.mark.django_db

PROTEIN = "acme/protein.Protein"
#: Haemoglobin alpha's first residues.
SEQUENCE = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMF"


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def rival(world: World) -> Project:
    """Another project in the same registry, where only the outsider is, as Editor and
    Admin: someone who mustn't learn anything of the Kinase Atlas's."""
    project = Project.objects.create(team=world.team, name="Rival Screens")
    ProjectMembership.objects.create(
        project=project, user=world.users["outsider"], role=ProjectRole.ADMIN
    )
    return project


def api_as(world: World, principal: str) -> Api:
    return Api(world.browser(principal))


def create(api: Api, project: Project, values: dict[str, Any], type_name: str = PROTEIN) -> Any:
    return api.post("/entities", {"type": type_name, "project": str(project.pk), "values": values})


def registrar(world: World, principal: str = "auditor") -> Api:
    """Someone the Org Admin appoints a registrar of the default registry."""
    admin = api_as(world, "org_admin")
    main = Registry.default()
    user = world.users[principal]
    assert admin.put(f"/registries/{main.pk}/registrars/{user.pk}").status_code in (200, 201)
    return api_as(world, principal)


# --- Registering ---------------------------------------------------------------------------------


def test_registering_again_tells_only_that_it_exists_and_its_id(
    world: World, rival: Project
) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    first = create(
        editor,
        world.project,
        {"sequence": SEQUENCE, "name": "Secret lead", "summary": "Binds the target at 3 nM."},
    )
    assert first.status_code == 201, first.content
    mine = first.json()
    assert mine["registered"] and not mine["reused"]
    assert mine["entry"]["hrid"] == mine["hrid"]
    again = create(outsider, rival, {"sequence": SEQUENCE})
    assert again.status_code == 201, again.content
    theirs = again.json()
    # The same entry and ID; a record of the rival project's own, with none of the Atlas's
    # data, nor who registered it, or when.
    assert not theirs["registered"] and not theirs["reused"]
    assert theirs["hrid"] == mine["hrid"] and theirs["entry"]["id"] == mine["entry"]["id"]
    assert theirs["id"] != mine["id"]
    assert theirs["values"].get("name") is None and theirs["values"].get("summary") is None
    assert theirs["created_by"]["id"] == str(world.users["outsider"].pk)
    assert "Secret" not in again.content.decode() and "3 nM" not in again.content.decode()
    assert str(world.users["editor"].pk) not in again.content.decode()
    # Registering it again where it has a record is that record, as it is.
    reused = create(editor, world.project, {"sequence": SEQUENCE})
    assert reused.status_code == 200 and reused.json()["reused"]
    assert reused.json()["id"] == mine["id"]


def test_another_projects_record_stays_unreadable(world: World, rival: Project) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    mine = create(editor, world.project, {"sequence": SEQUENCE, "name": "Secret lead"}).json()
    theirs = create(outsider, rival, {"sequence": SEQUENCE}).json()
    problem(outsider.get(f"/entities/{mine['id']}"), 404, "not_found")
    # By the shared ID, each reads only their own project's record.
    assert outsider.get(f"/entities/{mine['hrid']}").json()["id"] == theirs["id"]
    assert editor.get(f"/entities/{mine['hrid']}").json()["id"] == mine["id"]
    # The entry's history doesn't say who registered it, or when, to the rival.
    revisions = outsider.get(f"/entries/{mine['hrid']}/revisions").json()["items"]
    assert [r["kind"] for r in revisions] == ["created"]
    assert revisions[0]["user"] is None and revisions[0]["created_at"] is None
    assert editor.get(f"/entries/{mine['hrid']}/revisions").json()["items"][0]["user"]
    # Searching finds only the rival's own records.
    found = outsider.post("/entities/search", {"type": PROTEIN}).json()["items"]
    assert [e["id"] for e in found] == [theirs["id"]]
    # The entry lists only the records the reader can read.
    entry = outsider.get(f"/entries/{mine['hrid']}").json()
    assert [r["id"] for r in entry["records"]] == [theirs["id"]]
    assert entry["holders"] is None


def test_an_entrys_id_alone_grants_nothing(world: World, rival: Project) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    mine = create(editor, world.project, {"sequence": SEQUENCE}).json()
    entry = mine["entry"]
    for path in (
        f"/entries/{entry['hrid']}",
        f"/entries/{entry['id']}",
        f"/entries/{entry['id']}/revisions",
        f"/entities/{entry['hrid']}",
        f"/entities/{entry['id']}",
    ):
        problem(outsider.get(path), 404, "not_found")
    # Nor can a record of the rival's refer to it, nor get a record of it but by
    # registering it.
    made = create(
        outsider,
        rival,
        {"sequence": "MKV", "source": entry["id"]},
    )
    problem(made, 422, "validation_failed")
    added = outsider.post(f"/entries/{entry['id']}/records", {"project": str(rival.pk)})
    problem(added, 404, "not_found")
    # The Org Admin reads no project's data by their role, nor the registry's entries.
    problem(api_as(world, "org_admin").get(f"/entries/{entry['id']}"), 404, "not_found")


def test_references_hold_the_entry(world: World, rival: Project) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    dna = create(editor, world.project, {"bases": "ATGGTG"}, "acme/dna.DNASequence").json()
    protein = create(editor, world.project, {"sequence": SEQUENCE, "encoded_by": dna["id"]}).json()
    # Named by the record, it's stored as the entry, which every project's record shares.
    assert protein["values"]["encoded_by"] == dna["entry"]["id"]
    assert protein["refs"][dna["entry"]["id"]]["hrid"] == dna["hrid"]
    # A project that holds the identity can refer to it by the entry's ID.
    theirs = create(outsider, rival, {"bases": "ATGGTG"}, "acme/dna.DNASequence").json()
    made = create(outsider, rival, {"sequence": "MKVL", "encoded_by": theirs["entry"]["id"]})
    assert made.status_code == 201, made.content


# --- Facts -----------------------------------------------------------------------------------


def test_a_fact_is_set_once_and_then_only_by_a_registrar(world: World, rival: Project) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    mine = create(editor, world.project, {"sequence": SEQUENCE, "ec_number": "1.1.1.1"}).json()
    assert mine["values"]["ec_number"] == "1.1.1.1"
    # Registering it with another value is refused, without saying what it is.
    refused = problem(
        create(outsider, rival, {"sequence": SEQUENCE, "ec_number": "2.7.11.1"}),
        422,
        "validation_failed",
    )
    assert "1.1.1.1" not in str(refused)
    assert "registrars" in str(refused["errors"])
    # So is registering it again with another value where it has a record already.
    problem(
        create(editor, world.project, {"sequence": SEQUENCE, "ec_number": "2.7.11.1"}),
        422,
        "validation_failed",
    )
    # A holder may set a fact the entry hasn't got.
    theirs = create(outsider, rival, {"sequence": SEQUENCE, "hazard": "bsl1"}).json()
    assert theirs["values"]["hazard"] == "bsl1"
    assert editor.get(f"/entities/{mine['id']}").json()["values"]["hazard"] == "bsl1"
    # Editing a record can't change one that's set.
    etag = editor.get(f"/entities/{mine['id']}")["ETag"]
    changed = editor.patch(
        f"/entities/{mine['id']}",
        {"values": {"hazard": "bsl2"}, "reason": "Reclassified"},
        headers={"If-Match": etag},
    )
    problem(changed, 422, "validation_failed")
    # Nor can anyone but a registrar change it on the entry.
    entry = editor.get(f"/entries/{mine['hrid']}")
    problem(
        editor.patch(
            f"/entries/{mine['hrid']}",
            {"values": {"hazard": "bsl2"}, "reason": "Reclassified"},
            headers={"If-Match": entry["ETag"]},
        ),
        403,
        "forbidden",
    )
    curator = registrar(world)
    seen = curator.get(f"/entries/{mine['hrid']}")
    assert seen.status_code == 200 and seen.json()["holders"] == 2
    # A registrar's change needs a reason, and the revision they read.
    missing = curator.patch(
        f"/entries/{mine['hrid']}", {"values": {"hazard": "bsl2"}, "reason": ""}
    )
    assert missing.status_code == 422
    done = curator.patch(
        f"/entries/{mine['hrid']}",
        {"values": {"hazard": "bsl2"}, "reason": "Biosafety committee, 2026-11-17"},
        headers={"If-Match": seen["ETag"]},
    )
    assert done.status_code == 200, done.content
    assert done.json()["values"]["hazard"] == "bsl2"
    assert outsider.get(f"/entities/{theirs['id']}").json()["values"]["hazard"] == "bsl2"
    event = AuditEvent.objects.get(action="registry.facts_changed")
    assert event.actor_id == world.users["auditor"].pk
    assert event.data["reason"] == "Biosafety committee, 2026-11-17"
    # Every holder sees who changed it and why.
    revisions = outsider.get(f"/entries/{mine['hrid']}/revisions").json()["items"]
    assert revisions[0]["kind"] == "curated" and revisions[0]["user"]["name"]
    assert revisions[0]["reason"] == "Biosafety committee, 2026-11-17"
    # And what it changed, to review it.
    number = revisions[0]["number"]
    detail = outsider.get(f"/entries/{mine['hrid']}/revisions/{number}").json()
    assert detail["changes"] == {"hazard": {"before": "bsl1", "after": "bsl2"}}
    assert detail["values"]["sequence"] == SEQUENCE and detail["user"]["name"]
    # The first revision, which the rival's project didn't make, says what it set but not
    # who made it, or when.
    first = outsider.get(f"/entries/{mine['hrid']}/revisions/1").json()
    assert first["user"] is None and first["created_at"] is None
    assert first["values"]["ec_number"] == "1.1.1.1"
    problem(outsider.get(f"/entries/{mine['hrid']}/revisions/99"), 404, "not_found")


def test_a_derived_value_is_the_entrys(world: World, rival: Project) -> None:
    editor = api_as(world, "editor")
    made = create(editor, world.project, {"sequence": SEQUENCE}).json()
    record = Entity.objects.get(pk=made["id"])
    entry = Entity.objects.get(pk=made["entry"]["id"])
    assert record.entry_id == entry.pk and entry.owner_project_id is None
    assert entry.registry_id == Registry.default().pk and record.registry_id is None


# --- Superseding ---------------------------------------------------------------------------


def test_a_registrar_supersedes_and_holders_move_their_records(
    world: World, rival: Project
) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    wrong = create(editor, world.project, {"sequence": SEQUENCE + "K"}).json()
    right = create(editor, world.project, {"sequence": SEQUENCE}).json()
    dna = create(editor, world.project, {"bases": "ATGAAA"}, "acme/dna.DNASequence").json()
    citing = create(
        editor, world.project, {"sequence": "MKVLA", "source": wrong["entry"]["id"]}
    ).json()
    theirs = create(outsider, rival, {"sequence": SEQUENCE + "K"}).json()
    curator = registrar(world)
    # Only a registrar supersedes.
    problem(
        editor.post(
            f"/entries/{wrong['hrid']}/supersede",
            {"replacement": right["hrid"], "reason": "Mistyped residue"},
        ),
        403,
        "forbidden",
    )
    problem(
        curator.post(
            f"/entries/{wrong['hrid']}/supersede",
            {"replacement": dna["hrid"], "reason": "Not the same kind"},
        ),
        422,
        "validation_failed",
    )
    done = curator.post(
        f"/entries/{wrong['hrid']}/supersede",
        {"replacement": right["hrid"], "reason": "Mistyped residue"},
    )
    assert done.status_code == 200, done.content
    assert done.json()["superseded"]["by"]["hrid"] == right["hrid"]
    # Each holding project's Admins, and whoever added its record, are told.
    told = set(
        Notification.objects.filter(kind="registry.entry_superseded").values_list(
            "recipient__email", flat=True
        )
    )
    assert world.users["editor"].email in told and world.users["outsider"].email in told
    assert world.users["admin"].email in told
    # Registering it again is refused, naming the replacement.
    again = problem(create(outsider, rival, {"sequence": SEQUENCE + "K"}), 409, "refused")
    assert right["hrid"] in str(again)
    # The rival moves its record: its HRID is the replacement's now.
    moved = outsider.post(f"/entities/{theirs['id']}/move", {"reason": "Superseded"})
    assert moved.status_code == 200, moved.content
    assert moved.json()["hrid"] == right["hrid"]
    assert moved.json()["entry"]["id"] == right["entry"]["id"]
    # The Atlas already has the replacement: it can't move onto it.
    refused = editor.post(f"/entities/{wrong['id']}/move", {"reason": "Superseded"})
    assert refused.status_code in (409, 422)
    # Its records that cite the old entry still do, until it moves.
    assert (
        editor.get(f"/entities/{citing['id']}").json()["values"]["source"] == wrong["entry"]["id"]
    )
    assert AuditEvent.objects.filter(action="entry.superseded").count() == 1
    assert AuditEvent.objects.filter(action="entity.moved").count() == 1


# --- Several projects, several registries ------------------------------------------------------


def test_an_id_held_in_two_of_your_projects_asks_which(world: World) -> None:
    editor = api_as(world, "editor")
    second = Project.objects.create(team=world.team, name="Second Screen")
    ProjectMembership.objects.create(
        project=second, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    mine = create(editor, world.project, {"sequence": SEQUENCE}).json()
    other = create(editor, second, {"sequence": SEQUENCE}).json()
    asked = problem(editor.get(f"/entities/{mine['hrid']}"), 409, "choose_project")
    assert "Second Screen" in str(asked) and "Kinase Atlas" in str(asked)
    chosen = editor.get(f"/entities/{mine['hrid']}", project=str(second.pk))
    assert chosen.json()["id"] == other["id"]


def test_registries_are_kept_apart(world: World) -> None:
    admin = api_as(world, "org_admin")
    made = admin.post(
        "/registries",
        {"name": "Client A", "description": "A CRO client's.", "teams": [str(world.team.pk)]},
    )
    assert made.status_code == 201, made.content
    client = made.json()
    # Only an Org Admin adds one.
    problem(api_as(world, "editor").post("/registries", {"name": "Mine"}), 403, "forbidden")
    project = api_as(world, "team_member").post(
        "/projects",
        {"team": str(world.team.pk), "name": "Client A work", "registry": client["id"]},
    )
    assert project.status_code == 201, project.content
    theirs = Project.objects.get(pk=project.json()["id"])
    assert theirs.registry_id is not None and str(theirs.registry_id) == client["id"]
    member = api_as(world, "team_member")
    editor = api_as(world, "editor")
    mine = create(editor, world.project, {"sequence": SEQUENCE}).json()
    # The same identity in another registry is another entry, with an ID of its own,
    # and tells nothing of the first.
    apart = create(member, theirs, {"sequence": SEQUENCE}).json()
    assert apart["registered"] and apart["hrid"] != mine["hrid"]
    assert apart["entry"]["registry"]["id"] == client["id"]
    # A reference across registries is refused.
    ProjectMembership.objects.create(
        project=theirs, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    across = create(editor, theirs, {"sequence": "MKV", "source": mine["entry"]["id"]})
    problem(across, 422, "validation_failed")
    # A team the registry doesn't name can't put a project in it.
    refused = admin.post(
        "/projects",
        {"team": str(world.other_team.pk), "name": "Elsewhere", "registry": client["id"]},
    )
    assert refused.status_code in (403, 422)


def test_a_projects_registry_is_fixed_once_it_has_entities(world: World) -> None:
    admin = api_as(world, "org_admin")
    client = admin.post("/registries", {"name": "Client B", "all_teams": True}).json()
    empty = Project.objects.create(team=world.team, name="Empty")
    moved = admin.patch(f"/projects/{empty.pk}", {"registry": client["id"]})
    assert moved.status_code == 200, moved.content
    assert AuditEvent.objects.filter(action="project.registry_changed").count() == 1
    create(api_as(world, "editor"), world.project, {"sequence": SEQUENCE})
    refused = admin.patch(f"/projects/{world.project.pk}", {"registry": client["id"]})
    assert refused.status_code in (409, 422), refused.content
    # A project's own Admin can't change it.
    ProjectMembership.objects.create(
        project=empty, user=world.users["admin"], role=ProjectRole.ADMIN
    )
    main = str(Registry.default().pk)
    problem(
        api_as(world, "admin").patch(f"/projects/{empty.pk}", {"registry": main}),
        403,
        "forbidden",
    )


# --- The Org Admin's pages ---------------------------------------------------------------------


def test_org_admins_add_registries_and_appoint_registrars_on_their_pages(world: World) -> None:
    admin = world.browser("org_admin")
    listed = admin.get("/settings/registries/").content.decode()
    assert Registry.default().name in listed and "Add a registry" in listed
    refused = post(admin, "/settings/registries/", {"name": "Client A"})
    assert refused.status_code == 200
    assert "Choose the teams that use it" in refused.content.decode()
    made = post(
        admin,
        "/settings/registries/",
        {"name": "Client A", "description": "A CRO client's.", "teams": [str(world.team.pk)]},
    )
    assert made.status_code == 302
    client = Registry.objects.get(name="Client A")
    assert list(client.teams.all()) == [world.team] and not client.all_teams
    page = f"/settings/registries/{client.pk}/"
    appointed = post(admin, page, {"action": "appoint", "person": "auditor@example.com"})
    assert appointed.status_code == 302, appointed.content.decode()[:2000]
    assert client.registrars.filter(user=world.users["auditor"]).exists()
    shown = admin.get(page).content.decode()
    assert "auditor@example.com" in shown
    assert "Remove Auditor as a registrar" in shown
    dismissed = post(admin, page, {"action": "dismiss", "user": str(world.users["auditor"].pk)})
    assert dismissed.status_code == 302
    assert not client.registrars.exists()
    saved = post(admin, page, {"action": "save", "name": "Client A (CRO)", "all_teams": "on"})
    assert saved.status_code == 302
    client.refresh_from_db()
    assert (client.name, client.all_teams) == ("Client A (CRO)", True)
    actions = set(
        AuditEvent.objects.filter(action__startswith="registry.").values_list("action", flat=True)
    )
    assert {
        "registry.created",
        "registry.updated",
        "registry.registrar_appointed",
        "registry.registrar_removed",
    } <= actions
    # Nobody else sees these pages.
    for who in ("admin", "auditor", "editor"):
        assert world.browser(who).get(page).status_code == 403


def test_an_entry_says_where_you_could_add_a_record_of_it(world: World) -> None:
    editor = api_as(world, "editor")
    second = Project.objects.create(team=world.team, name="Second Screen")
    ProjectMembership.objects.create(
        project=second, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    made = create(editor, world.project, {"sequence": SEQUENCE}).json()
    entry = editor.get(f"/entries/{made['hrid']}").json()
    assert entry["records"] == [
        {
            "id": made["id"],
            "project": {
                "id": str(world.project.pk),
                "name": world.project.name,
                "slug": world.project.slug,
            },
        }
    ]
    assert [p["name"] for p in entry["add_to"]] == ["Second Screen"]
    added = editor.post(f"/entries/{made['entry']['id']}/records", {"project": str(second.pk)})
    assert added.status_code == 201, added.content
    assert added.json()["hrid"] == made["hrid"]
    assert editor.get(f"/entries/{made['hrid']}").json()["add_to"] == []
    # A project that doesn't allow sharing offers none.
    third = Project.objects.create(team=world.team, name="Third Screen")
    ProjectMembership.objects.create(
        project=third, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    Project.objects.filter(pk__in=[world.project.pk, second.pk]).update(allow_sharing=False)
    assert editor.get(f"/entries/{made['hrid']}").json()["add_to"] == []

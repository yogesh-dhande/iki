"""Registries (ADR 0115): an identity is registered once, as a registry entry with one ID;
each project keeps its own record of it. What another project knows never leaks: a
registration tells only that the identity exists and its ID, an entry's ID grants
nothing, registry facts change only by registrars, and registries are kept apart."""

from pathlib import Path
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world, post

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity
from iki_server.notifications import services as notifications
from iki_server.notifications.models import Notification
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Registry,
)
from iki_server.webhooks.models import Delivery, Webhook

pytestmark = pytest.mark.django_db

PROTEIN = "acme/protein.Protein"
#: Haemoglobin alpha's first residues.
SEQUENCE = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMF"
#: Two types of complex under iki/core.Complex's identity (ADR 0165): a lab's generic one,
#: of any parts in any roles, and its bispecific antibodies, of heavy and light chains.
MULTIMER = "acme/protein.Multimer"
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


#: Emicizumab's chains (KEGG DRUG D10821): its two heavy chains, and the light chain both
#: its arms share.
EMICIZUMAB = {k: v for k, v in _fasta("fc_fusions_and_bispecifics.fasta").items() if "D10821" in k}


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


def entry_read(api: Api, record: dict[str, Any]) -> dict[str, str]:
    """If-Match: a record's registry entry's revision, as read now (ADR 0289)."""
    return {"If-Match": api.get(f"/entries/{record['entry']['id']}")["ETag"]}


def record_read(api: Api, path: str) -> dict[str, str]:
    """If-Match: a record's revision, as read now."""
    return {"If-Match": api.get(path)["ETag"]}


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


def test_a_shared_entry_fact_indicates_a_reference_only_its_owner_can_read(
    world: World, rival: Project
) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    other = create(editor, world.project, {"sequence": "MRKAAA", "name": "Private"}).json()
    other_entry = other["entry"]["id"]
    mine = create(editor, world.project, {"sequence": SEQUENCE, "related_entry": other_entry})
    assert mine.status_code == 201, mine.content
    theirs = create(outsider, rival, {"sequence": SEQUENCE})
    assert theirs.status_code == 201, theirs.content
    entry_path = f"/entries/{mine.json()['entry']['id']}"
    all_paths = [f"/entities/{theirs.json()['id']}", entry_path, f"{entry_path}/revisions/1"]
    for path in all_paths:
        response = outsider.get(path)
        assert response.status_code == 200
        view = response.json()
        assert view["values"]["related_entry"] == "unreadable"
        body = response.content.decode()
        assert other_entry not in body and other["hrid"] not in body
    assert view["changes"]["related_entry"]["after"] == "unreadable"
    for path in [entry_path, f"{entry_path}/revisions/1"]:
        view = editor.get(path).json()
        assert view["values"]["related_entry"] == other_entry
        assert view["refs"][other_entry]["hrid"] == other["hrid"]


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


def test_a_fixed_fact_is_given_once_and_never_changed(world: World, rival: Project) -> None:
    """A fixed fact (ADR 0142) is set once, by whoever registers the entry or first gives it,
    and then no one changes it: not a holder, not a registrar. A wrong one is superseded."""
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    mine = create(editor, world.project, {"sequence": SEQUENCE, "standard": "WHO-1"}).json()
    assert mine["values"]["standard"] == "WHO-1"
    # Registering it with another is refused, without saying what it is.
    refused = problem(
        create(outsider, rival, {"sequence": SEQUENCE, "standard": "NIST-2"}),
        422,
        "validation_failed",
    )
    assert "WHO-1" not in str(refused)
    assert "fixed" in str(refused["errors"]) and "supersedes" in str(refused["errors"])
    # With the same one, or none, it's the same entry.
    same = create(outsider, rival, {"sequence": SEQUENCE, "standard": "WHO-1"})
    assert same.status_code == 201 and same.json()["entry"]["id"] == mine["entry"]["id"]
    # A registrar can't change it either, though they change other facts.
    curator = registrar(world)
    seen = curator.get(f"/entries/{mine['hrid']}")
    changed = problem(
        curator.patch(
            f"/entries/{mine['hrid']}",
            {"values": {"standard": "NIST-2"}, "reason": "Typo"},
            headers={"If-Match": seen["ETag"]},
        ),
        422,
        "validation_failed",
    )
    assert "supersedes" in str(changed["errors"])
    types = {t["name"]: t for t in editor.every("/entity-types")}
    standard = next(f for f in types[PROTEIN]["fields"] if f["name"] == "standard")
    assert (standard["registry"], standard["immutable"]) == ("fact", True)


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
    hook = Webhook.objects.create(
        project=rival,
        created_by=world.users["outsider"],
        url="https://hooks.example.org",
        events=["entry.superseded"],
    )
    # Only a registrar supersedes.
    problem(
        editor.post(
            f"/entries/{wrong['hrid']}/supersede",
            {"replacement": right["hrid"], "reason": "Mistyped residue"},
            headers=entry_read(curator, wrong),
        ),
        403,
        "forbidden",
    )
    problem(
        curator.post(
            f"/entries/{wrong['hrid']}/supersede",
            {"replacement": dna["hrid"], "reason": "Not the same kind"},
            headers=entry_read(curator, wrong),
        ),
        422,
        "validation_failed",
    )
    # It's made from the entry's revision the registrar read (ADR 0289).
    supersede = f"/entries/{wrong['hrid']}/supersede"
    replacing = {"replacement": right["hrid"], "reason": "Mistyped residue"}
    problem(curator.post(supersede, replacing), 428, "precondition_required")
    read = entry_read(curator, wrong)
    stale = curator.post(supersede, replacing, headers={"If-Match": '"99"'})
    problem(stale, 412, "stale")
    assert stale["ETag"] == read["If-Match"]
    assert not AuditEvent.objects.filter(action="entry.superseded").exists()
    done = curator.post(supersede, replacing, headers=read)
    assert done.status_code == 200, done.content
    assert done["ETag"] == f'"{done.json()["revision"]}"'
    assert done.json()["superseded"]["by"]["hrid"] == right["hrid"]
    # A holder learns which entry to move to and reads its facts once it holds it.
    ref = f"/entities/{theirs['id']}"
    for detail in (
        outsider.get(ref).json()["entry"],
        outsider.get(f"/entries/{wrong['entry']['id']}").json(),
    ):
        successor = detail["superseded"]
        assert successor["by_id"] == right["entry"]["id"]
        assert successor["by"]["hrid"] == successor["by"]["title"] == right["hrid"]
    assert outsider.get(f"/entries/{right['entry']['id']}").status_code == 404
    data = Delivery.objects.get(webhook=hook, event="entry.superseded").payload["data"]
    assert data["replacement"] == {"id": right["entry"]["id"], "hrid": right["hrid"]}
    assert right["id"] not in str(data) and SEQUENCE not in str(data)
    notice = Notification.objects.get(
        kind="registry.entry_superseded", recipient=world.users["outsider"]
    )
    assert right["hrid"] in notice.title and notice.path == ref
    assert str(notice.entity_id) == theirs["id"]
    # Both current notices and earlier project-scoped notices follow current data access.
    original = world.users["outsider"]
    OrgRoleAssignment.objects.create(user=original, role=OrgRole.ORG_ADMIN)
    past = Notification.objects.create(
        recipient=original, project=rival, kind=notice.kind, title=notice.title, path=notice.path
    )

    def visible_notices() -> set[Any]:
        return set(
            notifications.inbox(type(original).objects.get(pk=original.pk))
            .filter(pk__in=[notice.pk, past.pk])
            .values_list("pk", flat=True)
        )

    assert visible_notices() == {notice.pk, past.pk}
    rival.memberships.filter(user=original).delete()
    assert not visible_notices()
    rival.memberships.create(user=original, role=ProjectRole.ADMIN)
    assert visible_notices() == {notice.pk, past.pk}
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
    # The rival's independent holder must state why it is moving its record.
    for body in ({}, {"reason": "   "}):
        invalid = problem(
            outsider.post(f"{ref}/move", body, headers=record_read(outsider, ref)),
            422,
            "validation_failed",
        )
        assert invalid["errors"][0]["pointer"] == "#/reason"
        unchanged = outsider.get(ref).json()
        assert unchanged["entry"]["id"] == wrong["entry"]["id"]
        assert unchanged["revision"] == theirs["revision"]
    assert not AuditEvent.objects.filter(action="entity.moved").exists()

    # The rival moves its record: its HRID is the replacement's now.
    # From the record's revision its holder read: one changed since is refused.
    why = {"reason": "  Use the curator's corrected entry  "}
    problem(outsider.post(f"{ref}/move", why), 428, "precondition_required")
    stale = outsider.post(f"{ref}/move", why, headers={"If-Match": '"99"'})
    problem(stale, 412, "stale")
    assert stale["ETag"] == record_read(outsider, ref)["If-Match"]
    assert not AuditEvent.objects.filter(action="entity.moved").exists()
    moved = outsider.post(f"{ref}/move", why, headers=record_read(outsider, ref))
    assert moved.status_code == 200, moved.content
    assert moved.json()["hrid"] == right["hrid"]
    assert moved.json()["entry"]["id"] == right["entry"]["id"]
    # The Atlas already has the replacement: it can't move onto it.
    refused = editor.post(
        f"/entities/{wrong['id']}/move",
        {"reason": "Superseded"},
        headers=record_read(editor, f"/entities/{wrong['id']}"),
    )
    assert refused.status_code in (409, 422)
    # Its records that cite the old entry still do, until it moves.
    assert (
        editor.get(f"/entities/{citing['id']}").json()["values"]["source"] == wrong["entry"]["id"]
    )
    assert AuditEvent.objects.filter(action="entry.superseded").count() == 1
    event = AuditEvent.objects.get(action="entity.moved", target_id=theirs["id"])
    assert event.actor_id == world.users["outsider"].pk
    assert event.data["reason"] == "Use the curator's corrected entry"
    revision = outsider.get(f"{ref}/revisions/{moved.json()['revision']}").json()
    assert revision["reason"] == event.data["reason"]


# --- Registering as another type (ADR 0165) ----------------------------------------------------


def chains(api: Api, project: Project) -> tuple[str, str, str]:
    """Emicizumab's chains in the project, as their entries' IDs: its two heavy chains and
    its light chain."""
    made = [
        create(api, project, {"sequence": EMICIZUMAB[name]}).json()["entry"]["id"]
        for name in ("D10821-H1", "D10821-H2", "D10821-L")
    ]
    return made[0], made[1], made[2]


def parts(chain: tuple[str, str, str], roles: tuple[str, str, str]) -> list[dict[str, Any]]:
    """Emicizumab's parts and copies, in these roles: one of each heavy chain, and two of
    the light chain."""
    heavy, other, light = chain
    return [
        {"part": heavy, "copies": 1, "role": roles[0]},
        {"part": other, "copies": 1, "role": roles[1]},
        {"part": light, "copies": 2, "role": roles[2]},
    ]


#: How a lab's generic complexes name emicizumab's chains, and how a bispecific does.
GENERIC = ("chain_a", "chain_b", "chain_c")
ANTIBODY = ("heavy", "heavy", "light")


def errors(response: Any) -> dict[str, str]:
    body = problem(response, 422, "validation_failed")
    return {e["pointer"]: e["detail"] for e in body["errors"]}


def test_a_registrar_registers_an_entry_again_as_another_type_of_its_identity(
    world: World, rival: Project
) -> None:
    """Emicizumab's chains first registered as a lab's generic multimer, by a project's
    import, say: the registry's registrars register them as the bispecific they are, and
    each holding project moves its record there, with the values the two types share."""
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    mine, theirs = chains(editor, world.project), chains(outsider, rival)
    assert mine == theirs
    multimer = create(
        editor,
        world.project,
        {
            "name": "Emicizumab",
            "components": parts(mine, GENERIC),
            "description": "Bridges factor IXa and factor X, for hemophilia A",
        },
        MULTIMER,
    ).json()
    held = create(
        outsider, rival, {"name": "ACE910", "components": parts(theirs, GENERIC)}, MULTIMER
    ).json()
    assert held["entry"]["id"] == multimer["entry"]["id"]
    entry = multimer["entry"]["id"]
    collection = create(
        editor,
        world.project,
        {"name": "Hemophilia A bispecifics", "members": [entry]},
        "iki/core.Collection",
    ).json()
    # Registering it as a bispecific names what it's registered as.
    again = create(editor, world.project, {"components": parts(mine, ANTIBODY)}, BISPECIFIC)
    assert errors(again) == {
        "#/type": f"This is registered already, as a {MULTIMER} ({multimer['hrid']}): "
        "register it as one."
    }
    curator = registrar(world)
    assert curator.get(f"/entries/{entry}").json()["can"]["supersede_as"] == [
        {"type": BISPECIFIC, "fields": ["components", "arms"]}
    ]
    assert editor.get(f"/entries/{entry}").json()["can"]["supersede_as"] == []
    url = f"/entries/{multimer['hrid']}/supersede"
    # Each try names the entry's revision as read (ADR 0289): a refused one changes nothing.
    read = {"If-Match": curator.get(f"/entries/{multimer['hrid']}")["ETag"]}
    reason = "Its chains are emicizumab's: the lab registers bispecifics as one"
    arms = [
        {"heavy": mine[0], "light": mine[2], "target": "F9A"},
        {"heavy": mine[1], "light": mine[2], "target": "F10"},
    ]
    as_bispecific = {"components": parts(mine, ANTIBODY), "arms": arms}
    # Only a registrar does it.
    problem(
        editor.post(url, {"type": BISPECIFIC, "values": as_bispecific, "reason": reason}),
        403,
        "forbidden",
    )
    # As a type under its identity, with entities of its own, other than its own.
    for wrong, detail in (
        (PROTEIN, f"must be a iki/core.Complex, as {multimer['hrid']} is."),
        ("iki/core.Complex", "iki/core.Complex is abstract: choose one of its subtypes."),
        (MULTIMER, f"{multimer['hrid']} is a {MULTIMER} already."),
        ("acme/protein.Nothing", "There's no entity type acme/protein.Nothing."),
    ):
        assert errors(
            curator.post(
                url,
                {"type": wrong, "reason": reason},
                headers=read,
            )
        ) == {"#/type": detail}
    # A bispecific's parts take its roles, so the multimer's are refused until they're given.
    assert errors(
        curator.post(
            url,
            {"type": BISPECIFIC, "reason": reason},
            headers=read,
        )
    ) == {
        **{
            f"#/values/components/{i}/role": "isn't one of its roles: heavy, light, linker."
            for i in range(3)
        },
        "#/values/components": "needs at least 1 heavy row, and has 0. needs at least 1 light "
        "row, and has 0.",
    }
    # Its identity is the same: another copy of the light chain is another molecule.
    other = parts(mine, ANTIBODY)
    other[2]["copies"] = 1
    assert errors(
        curator.post(
            url,
            {"type": BISPECIFIC, "values": {"components": other}, "reason": reason},
            headers=read,
        )
    ) == {
        "#/values/components": f"must be {multimer['hrid']}'s: an entry registered as another "
        "type has the same identity."
    }
    # An entry has no record's fields; the new type's are checked as a registration's are.
    assert errors(
        curator.post(
            url,
            {"type": BISPECIFIC, "values": {**as_bispecific, "name": "E"}, "reason": reason},
            headers=read,
        )
    ) == {"#/values/name": f"{BISPECIFIC}'s entries have no field `name`."}
    assert "#/values/arms/0/target" in errors(
        curator.post(
            url,
            {
                "type": BISPECIFIC,
                "values": {**as_bispecific, "arms": [{**arms[0], "target": "factor IXa"}]},
                "reason": reason,
            },
            headers=read,
        )
    )
    # An existing entry or a type, and a reason.
    assert "#/replacement" in errors(
        curator.post(
            url,
            {"type": BISPECIFIC, "replacement": PROTEIN, "reason": reason},
            headers=read,
        )
    )
    assert "#/values" in errors(
        curator.post(
            url,
            {"replacement": multimer["hrid"], "values": {"arms": arms}, "reason": reason},
            headers=read,
        )
    )
    assert errors(
        curator.post(
            url,
            {"type": BISPECIFIC, "values": as_bispecific, "reason": "  "},
            headers=read,
        )
    ) == {"#/reason": "Say why: its holders are told."}
    # From the entry as the registrar read it: one changed since is refused (ADR 0289).
    as_one = {"type": BISPECIFIC, "values": as_bispecific, "reason": reason}
    problem(curator.post(url, as_one), 428, "precondition_required")
    problem(curator.post(url, as_one, headers={"If-Match": '"99"'}), 412, "stale")
    assert not AuditEvent.objects.filter(action="entry.superseded").exists()
    assert Entity.objects.get(pk=entry).superseded_by_id is None

    done = curator.post(
        url,
        {"type": BISPECIFIC, "values": as_bispecific, "reason": reason},
        headers=read,
    )
    assert done.status_code == 200, done.content
    superseded = done.json()["superseded"]
    assert superseded["by"]["type"] == BISPECIFIC and superseded["by"]["hrid"].startswith("BSP-")
    assert superseded["reason"] == reason and done.json()["can"]["supersede_as"] == []
    new = curator.get(f"/entries/{superseded['by_id']}").json()
    assert new["type"] == BISPECIFIC and new["revision"] == 1
    assert [c["role"] for c in new["values"]["components"]] == list(ANTIBODY)
    assert new["values"]["arms"] == arms
    (created,) = curator.get(f"/entries/{new['id']}/revisions").json()["items"]
    assert created["kind"] == "created" and created["reason"] == reason
    # The old entry keeps its ID and values; its identity is the new entry's.
    old = Entity.objects.get(pk=entry)
    replacement = Entity.objects.get(pk=new["id"])
    assert old.identity_hash is None and replacement.identity_hash
    assert old.superseded_by_id == replacement.pk
    assert curator.get(f"/entries/{entry}").json()["values"]["components"] == parts(mine, GENERIC)
    event = AuditEvent.objects.get(action="entry.superseded", target_id=entry)
    assert event.data["replacement"] == {
        "id": new["id"],
        "hrid": new["hrid"],
        "type": BISPECIFIC,
    }
    assert event.data["registered"] is True and event.data["reason"] == reason
    told = Notification.objects.filter(kind="registry.entry_superseded")
    assert {n.recipient_id for n in told} >= {world.users["editor"].pk, world.users["outsider"].pk}
    # Registering the identity finds the new entry: as a multimer, it says what it is now.
    assert errors(
        create(editor, world.project, {"components": parts(mine, GENERIC)}, MULTIMER)
    ) == {
        "#/type": f"This is registered already, as a {BISPECIFIC} ({new['hrid']}): register "
        "it as one."
    }

    # A holder moves its record: it can't become a bispecific's, so it's the project's new
    # record of it, with what the two types' records share, and the old one is archived.
    ref = f"/entities/{multimer['id']}"
    record = editor.get(ref).json()
    assert record["entry"]["superseded"]["held"] is False
    carries = record["entry"]["superseded"]["carries"]
    assert set(carries) == {"name", "description"}
    assert "components" not in carries and "arms" not in carries
    assert errors(
        editor.post(
            f"{ref}/move",
            {"reason": "Moved with the registry", "values": {"components": as_bispecific}},
            headers=record_read(editor, ref),
        )
    ) == {
        "#/values/components": f"is {new['hrid']}'s registry entry's: the record has its own "
        "fields."
    }
    moved = editor.post(
        f"{ref}/move", {"reason": "Moved with the registry"}, headers=record_read(editor, ref)
    )
    assert moved.status_code == 200, moved.content
    body = moved.json()
    assert body["id"] != multimer["id"] and body["hrid"] == new["hrid"]
    assert body["type"] == BISPECIFIC and body["entry"]["id"] == new["id"]
    assert body["values"]["name"] == "Emicizumab"
    assert body["values"]["description"] == "Bridges factor IXa and factor X, for hemophilia A"
    assert [r["hrid"] for r in body["repointed"]] == [collection["hrid"]]
    assert editor.get(f"/entities/{collection['id']}").json()["values"]["members"] == [new["id"]]
    (first,) = editor.get(f"/entities/{body['id']}/revisions").json()["items"]
    assert first["kind"] == "created" and first["reason"] == "Moved with the registry"
    # The old record keeps its ID, values and history, archived.
    kept = editor.get(ref).json()
    assert kept["archived"] and kept["hrid"] == multimer["hrid"]
    assert kept["entry"]["superseded"]["held"] is True
    assert kept["values"]["components"] == parts(mine, GENERIC)
    kinds = [r["kind"] for r in editor.get(f"{ref}/revisions").json()["items"]]
    assert kinds == ["archived", "created"]
    event = AuditEvent.objects.get(action="entity.moved", target_id=multimer["id"])
    assert event.data["record"] == {"id": body["id"], "type": BISPECIFIC}
    assert event.data["to"] == {"id": new["id"], "hrid": new["hrid"]}

    # The rival moves its own, once it isn't locked.
    there = f"/entities/{held['id']}"
    Entity.objects.filter(pk=held["id"]).update(locked_by=["review"])
    problem(
        outsider.post(
            f"{there}/move",
            {"reason": "Moved with the registry"},
            headers=record_read(outsider, there),
        ),
        409,
        "refused",
    )
    assert outsider.get(there).json()["entry"]["id"] == entry
    Entity.objects.filter(pk=held["id"]).update(locked_by=[])
    theirs_moved = outsider.post(
        f"{there}/move",
        {"reason": "Moved with the registry"},
        headers=record_read(outsider, there),
    )
    assert theirs_moved.status_code == 200, theirs_moved.content
    assert theirs_moved.json()["values"]["name"] == "ACE910"
    assert theirs_moved.json()["entry"]["id"] == new["id"]
    assert set(
        Entity.objects.filter(entry_id=new["id"]).values_list("owner_project", flat=True)
    ) == {
        world.project.pk,
        rival.pk,
    }


def test_what_two_types_share_goes_with_the_identity_and_its_unique_values(
    world: World,
) -> None:
    """Vector Laboratories' biotinylated goat anti-rabbit IgG (BA-1000, RRID:AB_2313606)
    registered as a primary antibody by mistake: the registrar registers it as the
    secondary antibody it is, with its RRID, a unique fact of what every reagent has; the
    project's new record has its LIMS ID, unique in the project, and the old one can't have
    it back."""
    editor = api_as(world, "editor")
    reagent = {"vendor": "Vector Laboratories", "catalog_number": "BA-1000"}
    wrong = create(
        editor,
        world.project,
        {**reagent, "rrid": "AB_2313606", "host": "goat", "lims_id": "AB-0412"},
        "acme/protein.PrimaryAntibody",
    ).json()
    curator = registrar(world)
    (option,) = curator.get(f"/entries/{wrong['hrid']}").json()["can"]["supersede_as"]
    # Its host is a primary's field, not one every reagent has: the registrar gives it.
    assert option == {
        "type": "acme/protein.SecondaryAntibody",
        "fields": ["raised_against", "host"],
    }
    done = curator.post(
        f"/entries/{wrong['hrid']}/supersede",
        {
            "type": "acme/protein.SecondaryAntibody",
            "values": {"raised_against": "rabbit", "host": "goat"},
            "reason": "BA-1000 is a goat anti-rabbit IgG: a secondary antibody",
        },
        headers=record_read(curator, f"/entries/{wrong['hrid']}"),
    )
    assert done.status_code == 200, done.content
    by = done.json()["superseded"]["by"]
    assert by["hrid"].startswith("SAB-")
    new = curator.get(f"/entries/{by['hrid']}").json()["values"]
    assert new == {
        **reagent,
        "rrid": "AB_2313606",
        "raised_against": "rabbit",
        "host": "goat",
    }
    # Its RRID is the new entry's: no other entry may have it, and the old one keeps it,
    # as its history does.
    assert curator.get(f"/entries/{wrong['hrid']}").json()["values"]["rrid"] == "AB_2313606"
    other = create(
        editor,
        world.project,
        {"vendor": "Vector Laboratories", "catalog_number": "BA-9200", "rrid": "AB_2313606"},
        "acme/protein.SecondaryAntibody",
    )
    assert "#/values/rrid" in errors(other)
    ref = f"/entities/{wrong['id']}"
    moved = editor.post(
        f"{ref}/move",
        {"reason": "Registered as a secondary"},
        headers=record_read(editor, ref),
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["values"]["lims_id"] == "AB-0412"
    # The LIMS ID is the new record's: the old one can't be restored with it.
    kept = editor.get(ref).json()
    restored = editor.patch(
        ref,
        {"archived": False, "reason": "Moved by mistake"},
        headers={"If-Match": f'"{kept["revision"]}"'},
    )
    detail = problem(restored, 409, "refused")["detail"]
    assert "LIMS ID" in detail and by["hrid"] in detail
    assert editor.get(ref).json()["archived"] is True
    clash = create(
        editor,
        world.project,
        {"vendor": "Abcam", "catalog_number": "ab6721", "lims_id": "AB-0412"},
        "acme/protein.SecondaryAntibody",
    )
    assert errors(clash) == {
        "#/values/lims_id": f"is {by['hrid']}’s already: no two entities in a project share it."
    }


def test_a_move_to_another_type_stops_at_what_cant_refer_to_it(world: World) -> None:
    """A binding measurement is of a bispecific: if the bispecific was registered wrongly,
    and is a generic multimer, the measurement can't refer to it, and the move says so."""
    editor = api_as(world, "editor")
    mine = chains(editor, world.project)
    bispecific = create(
        editor,
        world.project,
        {"name": "Emicizumab", "components": parts(mine, ANTIBODY)},
        BISPECIFIC,
    ).json()
    binding = create(
        editor,
        world.project,
        {"bispecific": bispecific["entry"]["id"], "target": "F9A", "kd": 1.58},
        "acme/protein.Binding",
    ).json()
    curator = registrar(world)
    done = curator.post(
        f"/entries/{bispecific['hrid']}/supersede",
        {"type": MULTIMER, "reason": "A research reagent, not one of the lab's bispecifics"},
        headers=record_read(curator, f"/entries/{bispecific['hrid']}"),
    )
    assert done.status_code == 200, done.content
    replacement = done.json()["superseded"]["by"]
    ref = f"/entities/{bispecific['id']}"
    stuck = problem(
        editor.post(f"{ref}/move", {"reason": "Moved"}, headers=record_read(editor, ref)),
        409,
        "refused",
    )
    assert f"{binding['hrid']}'s Bispecific must be a {BISPECIFIC}" in stuck["detail"]
    assert editor.get(ref).json()["archived"] is False
    # Archived, it keeps its reference, and the record moves.
    archived = editor.patch(
        f"/entities/{binding['id']}",
        {"archived": True, "reason": "Measured against the wrong molecule"},
        headers={"If-Match": f'"{binding["revision"]}"'},
    )
    assert archived.status_code == 200, archived.content
    moved = editor.post(f"{ref}/move", {"reason": "Moved"}, headers=record_read(editor, ref))
    assert moved.status_code == 200, moved.content
    assert moved.json()["type"] == MULTIMER and moved.json()["hrid"] == replacement["hrid"]
    kept = editor.get(f"/entities/{binding['id']}").json()
    assert kept["values"]["bispecific"] == bispecific["entry"]["id"]


def test_a_mistaken_entry_is_superseded_by_another_type_of_its_kind(world: World) -> None:
    """A multimer registered with a copy of the light chain too few is the lab's bispecific,
    registered right: an entry of another type under the same identity replaces it."""
    editor = api_as(world, "editor")
    mine = chains(editor, world.project)
    short = parts(mine, GENERIC)
    short[2]["copies"] = 1
    wrong = create(editor, world.project, {"components": short}, MULTIMER).json()
    right = create(editor, world.project, {"components": parts(mine, ANTIBODY)}, BISPECIFIC).json()
    curator = registrar(world)
    done = curator.post(
        f"/entries/{wrong['hrid']}/supersede",
        {"replacement": right["hrid"], "reason": "Emicizumab has two copies of its light chain"},
        headers=record_read(curator, f"/entries/{wrong['hrid']}"),
    )
    assert done.status_code == 200, done.content
    assert done.json()["superseded"]["by"]["hrid"] == right["hrid"]
    # Its identity stays its own: registering it is refused, naming the replacement.
    again = problem(create(editor, world.project, {"components": short}, MULTIMER), 409, "refused")
    assert right["hrid"] in again["detail"]
    # The project holds the replacement already: it archives one of the two.
    problem(
        editor.post(
            f"/entities/{wrong['id']}/move",
            {"reason": "Moved"},
            headers=record_read(editor, f"/entities/{wrong['id']}"),
        ),
        409,
        "refused",
    )


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
    listed = admin.get("/admin/registries/").content.decode()
    assert Registry.default().name in listed and "Add a registry" in listed
    refused = post(admin, "/admin/registries/", {"name": "Client A"})
    assert refused.status_code == 200
    assert "Choose the teams that use it" in refused.content.decode()
    made = post(
        admin,
        "/admin/registries/",
        {"name": "Client A", "description": "A CRO client's.", "teams": [str(world.team.pk)]},
    )
    assert made.status_code == 302
    client = Registry.objects.get(name="Client A")
    assert list(client.teams.all()) == [world.team] and not client.all_teams
    page = f"/admin/registries/{client.pk}/"
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

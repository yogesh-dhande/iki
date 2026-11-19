"""Notifications a type declares (ADR 0129): on a state's change or a field's, the fixture's
Sample tells the people its fields name, holders of a lab role in its project, its
creator and its watchers, in Iki and by email, each only while they can read it, as they
choose, and audited."""

import datetime as dt
from typing import Any

import pytest
from api_client import problem
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from org_world import World
from test_api_entities import api_as, create
from test_runs import world
from test_states import ANALYST, QA, REVIEWER, SAMPLE, roles, set_
from test_workers import keep_connection

from iki_server.audit import log as audit
from iki_server.audit.models import AuditEvent
from iki_server.entities import services as entities
from iki_server.entities import states
from iki_server.entities.models import Entity
from iki_server.notifications import services, watches
from iki_server.notifications.models import Notification, NotificationRecord, NotificationSetting
from iki_server.org import services as org
from iki_server.setup.models import Installation
from iki_server.tasks.models import TaskRecord
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers and test_runs

IN_REVIEW = f"{SAMPLE}.in_review"
RELEASED = f"{SAMPLE}.released"
QUARANTINED = f"{SAMPLE}.quarantined"
RELABELLED = f"{SAMPLE}.relabelled"
LOGGED = f"{SAMPLE}.logged"


@pytest.fixture
def email_on(settings: Any) -> None:
    settings.IKI_EMAIL_ENABLED = True
    settings.IKI_PUBLIC_URL = "https://iki.example.com"
    settings.DEFAULT_FROM_EMAIL = "Iki <iki@example.com>"


def told(world: World, principal: str, kind: str = IN_REVIEW) -> list[Notification]:
    return list(
        Notification.objects.filter(recipient=world.users[principal], kind=kind).order_by("id")
    )


def sample(world: World, label: str = "S-1", **values: Any) -> dict[str, Any]:
    made = create(api_as(world, "editor"), world.project, {"label": label, **values}, SAMPLE)
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def submitted(world: World, label: str = "S-1", **values: Any) -> dict[str, Any]:
    """A sample the editor tested and submitted for review."""
    made = sample(world, label, **values)
    set_(world, "editor", made["id"], "status", "in_testing")
    done: dict[str, Any] = set_(world, "editor", made["id"], "status", "in_review")
    return done


def sent(record: str) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action="notification.sent", target_id=record).order_by(
            "occurred_at"
        )
    )


def send_emails() -> int:
    return Runner(queues=["default"]).run_ready()


@pytest.fixture
def lab(world: World) -> World:
    """The editor tests samples, the admin and the viewer review them, the mixed member is QA."""
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(REVIEWER,), mixed=(QA,))
    return world


# --- The type says what it sends ----------------------------------------------------------------


def test_a_types_page_previews_its_notifications(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{SAMPLE}").json()
    by_name = {n["name"]: n for n in found["notifications"]}
    assert by_name["in_review"] == {
        "name": "in_review",
        "kind": IN_REVIEW,
        "label": "Ready for review",
        "description": "Reviewers hear when an analyst submits a sample's results for review.",
        "declared_by": SAMPLE,
        "when": "When its Status changes to In review",
        "only_some": False,
        "tells": ["Everyone with the Reviewer role in its project", "Whoever watches it"],
        "email": True,
        "required": False,
    }
    assert by_name["released"]["when"] == "When its Status changes from Reviewed to Released"
    assert by_name["released"]["required"] is True
    assert by_name["quarantined"]["only_some"] is True
    assert by_name["relabelled"]["when"] == "When its Protein’s Lims id changes"
    assert by_name["logged"]["when"] == "When one is made"


# --- Who is told --------------------------------------------------------------------------------


def test_a_state_entering_tells_its_roles_and_not_whoever_set_it(lab: World) -> None:
    done = submitted(lab, purity=97.5)
    (note,) = told(lab, "admin")
    assert note.title == "S-1 is ready for review"
    assert note.body == "Purity 97.5, by nobody, on no protein."
    assert note.path == f"/entities/{done['id']}"
    assert (note.project, note.actor, note.count) == (lab.project, lab.users["editor"], 1)
    assert note.entity_id is not None and str(note.entity_id) == done["id"]
    assert len(told(lab, "viewer")) == 1
    # The editor set it: they're not told of their own change. QA isn't named.
    assert told(lab, "editor") == told(lab, "mixed") == []
    (event,) = sent(done["id"])
    assert event.actor == lab.users["editor"]
    assert event.data["kind"] == IN_REVIEW
    assert {p["name"]: p["as"] for p in event.data["told"]} == {"Admin": "role", "Viewer": "role"}
    assert event.data["not_told"] == []


def test_leaving_a_state_and_the_states_between_say_when(lab: World) -> None:
    done = submitted(lab, analyst=str(lab.users["editor"].pk))
    set_(lab, "admin", done["id"], "status", "reviewed")
    # Released only from Reviewed: going there from elsewhere tells nobody.
    set_(lab, "mixed", done["id"], "status", "released")
    (note,) = told(lab, "editor", RELEASED)
    assert note.title == f"{done['hrid']}: Released"
    set_(lab, "mixed", done["id"], "status", "in_testing")
    set_(lab, "mixed", done["id"], "status", "released")
    assert len(told(lab, "editor", RELEASED)) == 1


def test_a_field_naming_people_tells_them_and_the_creator_is_told_too(lab: World) -> None:
    roles(lab, admin=(REVIEWER, ANALYST))
    done = submitted(lab, analyst=str(lab.users["admin"].pk))
    set_(lab, "admin", done["id"], "status", "reviewed")
    set_(lab, "mixed", done["id"], "status", "released")
    # The admin is its analyst; the editor made it.
    assert len(told(lab, "admin", RELEASED)) == 1
    assert len(told(lab, "editor", RELEASED)) == 1
    (event,) = sent(done["id"])[-1:]
    assert {p["name"]: p["as"] for p in event.data["told"]} == {
        "Admin": "field",
        "Editor": "creator",
    }


def test_a_record_being_made_tells_whom_it_names(lab: World) -> None:
    done = sample(lab, received_by=str(lab.users["viewer"].pk))
    (note,) = told(lab, "viewer", LOGGED)
    assert note.title == f"{done['hrid']}: Logged"
    assert sent(done["id"])[0].data["told"][0]["as"] == "field"


def test_conditions_choose_the_records_that_send_it(lab: World) -> None:
    today = timezone.now().date()
    late = sample(
        lab,
        "S-late",
        helpers=[str(lab.users["viewer"].pk)],
        due_on=str(today - dt.timedelta(days=1)),
    )
    soon = sample(lab, "S-soon", helpers=[str(lab.users["viewer"].pk)], due_on=str(today))
    set_(lab, "mixed", late["id"], "custody", "quarantined", "Spilled")
    set_(lab, "mixed", soon["id"], "custody", "quarantined", "Spilled")
    (note,) = told(lab, "viewer", QUARANTINED)
    assert note.entity_id is not None and str(note.entity_id) == soon["id"]


def test_a_change_to_the_record_it_refers_to_tells_of_it(lab: World) -> None:
    api = api_as(lab, "editor")
    protein = create(api, lab.project, {"sequence": "MKVLA"}, "acme/protein.Protein").json()
    done = sample(lab, protein=protein["id"], received_by=str(lab.users["viewer"].pk))
    other = sample(lab, "S-2", received_by=str(lab.users["viewer"].pk))
    etag = api.get(f"/entities/{protein['id']}")["ETag"]
    changed = api.patch(
        f"/entities/{protein['id']}", {"values": {"lims_id": "P-77"}}, headers={"If-Match": etag}
    )
    assert changed.status_code == 200, changed.content
    (note,) = told(lab, "viewer", RELABELLED)
    assert note.entity_id is not None and str(note.entity_id) == done["id"]
    assert note.title == f"{done['hrid']}: Its protein's LIMS ID changed"
    (event,) = sent(done["id"])[-1:]
    assert event.data["about"] == {"id": protein["id"], "hrid": protein["hrid"]}
    assert sent(other["id"])[-1].data["kind"] == LOGGED


def test_an_agents_change_tells_the_person_it_acted_for(lab: World) -> None:
    roles(lab, admin=(REVIEWER, ANALYST))
    made = sample(lab)
    entity = Entity.objects.get(pk=made["id"])
    admin = lab.users["admin"]
    agent = audit.AgentInfo(client_id="claude-code", client_name="Claude Code")
    with audit.using(audit.Context(via="api", agent=agent)):
        states.set_state(admin, entity, "status", "in_testing", expected_revision=1)
        states.set_state(admin, entity, "status", "in_review", expected_revision=2)
    # Their agent made it, not their own hands: they're told, with who did it.
    (note,) = told(lab, "admin")
    assert note.agent is not None and note.agent.client_name == "Claude Code"
    body = api_as(lab, "admin").get("/notifications").json()["items"][0]
    assert body["agent"] == {"client_id": "claude-code", "name": "Claude Code"}
    assert body["actor"]["name"] == "Admin"
    assert (body["kind_label"], body["count"]) == ("Ready for review", 1)
    assert body["records"] == [{"id": made["id"], "hrid": made["hrid"], "type": SAMPLE}]


def test_nobody_who_cant_read_it_is_told_and_the_inbox_forgets_what_they_cant(lab: World) -> None:
    done = sample(lab)
    viewer = lab.users["viewer"]
    watches.choose(viewer, Entity.objects.get(pk=done["id"]), "watching")
    watches.choose(lab.users["admin"], Entity.objects.get(pk=done["id"]), "watching")
    lab.users["admin"].is_active = False
    lab.users["admin"].save()
    set_(lab, "editor", done["id"], "status", "in_testing")
    set_(lab, "editor", done["id"], "status", "in_review")
    assert told(lab, "admin") == []
    (event,) = sent(done["id"])[-1:]
    assert {p["name"]: p["why"] for p in event.data["not_told"]} == {"Admin": "deactivated"}
    # The viewer leaves the project: the notification is gone from their inbox and API.
    assert len(told(lab, "viewer")) == 1
    org.remove_project_membership(lab.project.memberships.get(user=viewer), by=lab.users["admin"])
    assert not services.inbox(viewer).filter(kind=IN_REVIEW).exists()


# --- Choices ------------------------------------------------------------------------------------


def test_someone_turns_one_off_everywhere_or_in_a_project(lab: World) -> None:
    viewer = lab.users["viewer"]
    services.set_choices(viewer, {IN_REVIEW: "off"})
    first = submitted(lab)
    assert told(lab, "viewer") == []
    assert {p["name"]: p["why"] for p in sent(first["id"])[-1].data["not_told"]} == {
        "Viewer": "turned it off"
    }
    # On again everywhere, but off in this project: still not told here.
    services.set_choices(viewer, {IN_REVIEW: "email"}, [(lab.project.pk, IN_REVIEW, "off")])
    submitted(lab, "S-2")
    assert told(lab, "viewer") == []
    services.set_choices(viewer, projects=[(lab.project.pk, IN_REVIEW, None)])
    submitted(lab, "S-3")
    assert len(told(lab, "viewer")) == 1


def test_a_required_one_cant_be_turned_off_or_muted(lab: World) -> None:
    editor = lab.users["editor"]
    with pytest.raises(services.Invalid, match="its plugin requires it"):
        services.set_choices(editor, {RELEASED: "off"})
    done = submitted(lab)
    watches.choose(editor, Entity.objects.get(pk=done["id"]), "muted")
    # A row someone kept from before it was required doesn't turn it off either.
    NotificationSetting.objects.create(user=editor, kind=RELEASED, delivery="off")
    set_(lab, "admin", done["id"], "status", "reviewed")
    set_(lab, "mixed", done["id"], "status", "released")
    assert len(told(lab, "editor", RELEASED)) == 1


def test_watchers_hear_and_muters_dont(lab: World) -> None:
    roles(lab, runner=())
    done = sample(lab)
    entity = Entity.objects.get(pk=done["id"])
    assert watches.of(lab.users["runner"], entity).can_watch
    watches.choose(lab.users["runner"], entity, "watching")
    watches.choose(lab.users["viewer"], entity, "muted")
    set_(lab, "editor", done["id"], "status", "in_testing")
    set_(lab, "editor", done["id"], "status", "in_review")
    assert len(told(lab, "runner")) == 1
    assert told(lab, "viewer") == []
    reasons = {p["name"]: p["why"] for p in sent(done["id"])[-1].data["not_told"]}
    assert reasons == {"Viewer": "muted it"}
    assert {p["name"]: p["as"] for p in sent(done["id"])[-1].data["told"]}["Runner"] == "watcher"


def test_watching_through_the_api(lab: World) -> None:
    done = sample(lab)
    api = api_as(lab, "viewer")
    assert api.get(f"/entities/{done['id']}/watch").json() == {
        "mode": "default",
        "can_watch": True,
        "can_mute": True,
    }
    answer = api.put(f"/entities/{done['id']}/watch", {"mode": "watching"})
    assert answer.json()["mode"] == "watching"
    event = AuditEvent.objects.get(action="notifications.watch_changed")
    assert event.data == {"from": "default", "to": "watching"}
    assert api.put(f"/entities/{done['id']}/watch", {"mode": "default"}).json()["mode"] == (
        "default"
    )
    # Only what you can read.
    outsider = api_as(lab, "outsider").get(f"/entities/{done['id']}/watch")
    problem(outsider, 404, "not_found")


def test_choices_for_a_types_notifications_through_the_api(lab: World) -> None:
    api = api_as(lab, "viewer")
    body = api.get("/me/notification-settings").json()
    by_kind = {k["kind"]: k for k in body["kinds"]}
    review = by_kind[IN_REVIEW]
    assert review["group"] == "Samples (Acme/protein)"
    assert (review["type"], review["can_turn_off"], review["per_project"]) == (SAMPLE, True, True)
    assert (by_kind[RELEASED]["required"], by_kind[RELEASED]["can_turn_off"]) == (True, False)
    assert by_kind[QUARANTINED]["default"] == "in_app"
    changed = api.patch(
        "/me/notification-settings",
        {"projects": [{"project": str(lab.project.pk), "kind": IN_REVIEW, "delivery": "off"}]},
    ).json()
    assert changed["projects"] == [
        {
            "project": {
                "id": str(lab.project.pk),
                "name": lab.project.name,
                "slug": lab.project.slug,
            },
            "kind": IN_REVIEW,
            "delivery": "off",
        }
    ]
    # Not in a project they can't see, and not Iki's own kinds, which are chosen everywhere.
    from iki_server.org.models import Project

    hidden = Project.objects.create(team=lab.team, name="Hidden")
    for project, kind in ((hidden.pk, IN_REVIEW), (lab.project.pk, "project.added")):
        refused = api.patch(
            "/me/notification-settings",
            {"projects": [{"project": str(project), "kind": kind, "delivery": "in_app"}]},
        )
        problem(refused, 422, "validation_failed")


# --- Told together, and emailed minimally ---------------------------------------------------------


@pytest.mark.usefixtures("email_on")
def test_many_records_at_once_are_told_together_in_one_email(lab: World) -> None:
    made = [submitted(lab, f"S-{n}")["hrid"] for n in range(3)]
    (note,) = told(lab, "admin")
    assert (note.count, note.title, note.entity_id) == (3, "3 Samples: Ready for review", None)
    assert note.path == f"/projects/{lab.project.slug}/entities?type={SAMPLE}"
    assert NotificationRecord.objects.filter(notification=note).count() == 3
    items = api_as(lab, "admin").get("/notifications").json()["items"]
    assert sorted(r["hrid"] for r in items[0]["records"]) == sorted(made)
    send_emails()
    to_admin = [m for m in mail.outbox if m.to == ["admin@example.com"]]
    assert len(to_admin) == 1
    assert to_admin[0].subject.endswith("3 Samples: Ready for review")
    assert (
        AuditEvent.objects.filter(
            action="notification.emailed", target_id=str(lab.users["admin"].pk)
        ).count()
        == 1
    )
    # Once it's emailed, the next starts anew.
    submitted(lab, "S-9")
    assert [n.count for n in told(lab, "admin")] == [3, 1]


@pytest.mark.usefixtures("email_on")
def test_emails_carry_no_values_only_what_it_is_and_a_link(lab: World) -> None:
    # A value with a decimal point: no UUID or HRID in the email's link has one, so "42."
    # is the value however it's written (42.137, 42.14), and never a chance match.
    done = submitted(lab, purity=42.137, notes="secret batch notes")
    send_emails()
    (message,) = [m for m in mail.outbox if m.to == ["admin@example.com"]]
    assert message.subject == f"[Example Bio] {done['hrid']}: Ready for review"
    assert isinstance(message, mail.EmailMultiAlternatives)
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    for text in (message.body, html):
        assert "42." not in text and "Purity" not in text and "secret" not in text
        assert "Reviewers hear when an analyst submits" in text
        assert f"https://iki.example.com/entities/{done['id']}" in text
        assert "From Editor." in text
    # With details off, it doesn't even say what.
    installation = Installation.get()
    installation.email_details = False
    installation.save()
    second = submitted(lab, "S-2")
    send_emails()
    last = mail.outbox[-1]
    assert last.subject == "[Example Bio] You have a new notification"
    assert second["hrid"] not in last.body


@pytest.mark.usefixtures("email_on")
def test_an_email_waits_for_others_to_join_it(lab: World) -> None:
    with override_settings(IKI_NOTIFICATION_BATCH_SECONDS=120):
        submitted(lab)
    (note,) = told(lab, "admin")
    task = TaskRecord.objects.get(args__0=str(note.pk))
    assert task.run_after > timezone.now() + dt.timedelta(seconds=100)
    send_emails()
    note.refresh_from_db()
    assert note.emailed_at is None
    assert [m for m in mail.outbox if "Ready for review" in m.subject] == []


@pytest.mark.usefixtures("email_on")
def test_an_email_isnt_sent_to_someone_who_cant_read_it_any_more(lab: World) -> None:
    submitted(lab)
    org.remove_project_membership(
        lab.project.memberships.get(user=lab.users["viewer"]), by=lab.users["admin"]
    )
    send_emails()
    # They're told they were removed, but not of a sample they can't read any more.
    to_viewer = [m.subject for m in mail.outbox if m.to == ["viewer@example.com"]]
    assert to_viewer and not [s for s in to_viewer if "Ready for review" in s]


def test_a_bulk_import_tells_nobody_record_by_record(lab: World) -> None:
    with entities.quietly():
        entities.create(
            lab.users["editor"],
            SAMPLE,
            lab.project,
            {"label": "S-q", "received_by": str(lab.users["viewer"].pk)},
        )
    assert told(lab, "viewer", LOGGED) == []


# --- However many are told ----------------------------------------------------------------------


def test_who_can_read_it_is_asked_once_however_many_a_role_holds(lab: World) -> None:
    """A role of 200 costs what a role of 5 does: who can read the record, and the record
    its message names, is asked of everyone at once."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from iki_server.accounts.models import User
    from iki_server.entities.models import Revision
    from iki_server.notifications import declared
    from iki_server.org.models import (
        LabRoleGrant,
        ProjectMembership,
        ProjectRole,
        Team,
        TeamMembership,
    )

    team = Team.objects.create(name="Reviewers")
    grant = ProjectMembership.objects.create(
        project=lab.project, team=team, role=ProjectRole.VIEWER
    )
    LabRoleGrant.objects.create(membership=grant, role=REVIEWER)

    def holders(count: int) -> None:
        start = TeamMembership.objects.filter(team=team).count()
        for i in range(start, count):
            person = User.objects.create_user(f"reviewer{i}@example.com", f"Reviewer {i}")
            TeamMembership.objects.create(team=team, user=person)
            # Off, so what's counted is deciding whom to tell, not writing to each inbox.
            NotificationSetting.objects.create(user=person, kind=IN_REVIEW, delivery="off")

    for principal in ("admin", "viewer"):
        NotificationSetting.objects.create(
            user=lab.users[principal], kind=IN_REVIEW, delivery="off"
        )
    api = api_as(lab, "editor")
    protein = create(api, lab.project, {"sequence": "MKVLA"}, "acme/protein.Protein").json()
    done = submitted(lab, protein=protein["id"])
    entity = Entity.objects.get(pk=done["id"])
    revision = Revision.objects.filter(entity=entity).order_by("-number").first()
    assert revision is not None
    change = {"status": ("in_testing", "in_review")}

    def queries(count: int) -> int:
        holders(count)
        with CaptureQueriesContext(connection) as captured:
            declared.changed(entity, revision, change, lab.users["editor"])
        event = sent(done["id"])[-1]
        assert sum(p["why"] == "turned it off" for p in event.data["not_told"]) == count + 2
        return len(captured)

    assert queries(5) == queries(200)


# --- Signed states ------------------------------------------------------------------------------


def test_a_state_set_by_signing_for_it_tells_of_it_too(world: World) -> None:
    """Its last signature sets it (ADR 0126), and its notifications are sent then."""
    from test_signatures import LOT, lot, ready, signed

    ready(world)
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(QA,))
    made = lot(world)
    signed(world, "editor", made["id"], "in_review", reason="Ready")
    (note,) = told(world, "admin", f"{LOT}.in_review")
    assert note.title == f"{made['hrid']}: Signed for review"
    assert note.actor == world.users["editor"]
    # The signer set it themselves: they aren't told of their own change.
    assert told(world, "editor", f"{LOT}.in_review") == []
    (event,) = sent(made["id"])
    assert event.data["revision"] == 2
    assert {p["name"]: p["as"] for p in event.data["told"]} == {"Admin": "role"}

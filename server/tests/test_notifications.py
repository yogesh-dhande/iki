"""Notifications: who is told what, the inbox API, email, and the settings pages."""

import smtplib
from datetime import timedelta
from typing import Any

import pytest
from api_client import Api, problem
from django.core import mail
from django.db import IntegrityError, transaction
from django.tasks import TaskResultStatus
from django.utils import timezone
from org_world import World, build_world, post
from test_oauth import bearer, signed_in_token

from iki_server.accounts import service_accounts
from iki_server.audit.models import AuditEvent
from iki_server.notifications import kinds, services
from iki_server.notifications.models import EmailPreference, Notification
from iki_server.oauth import services as oauth_services
from iki_server.oauth.models import Client
from iki_server.org import services as org
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.setup.models import Installation
from iki_server.tasks.models import TaskRecord
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def email_on(settings: Any) -> None:
    settings.IKI_EMAIL_ENABLED = True
    settings.IKI_PUBLIC_URL = "https://iki.example.com"
    settings.DEFAULT_FROM_EMAIL = "Iki <iki@example.com>"


def inbox(world: World, principal: str) -> list[Notification]:
    return list(Notification.objects.filter(recipient=world.users[principal]).order_by("id"))


def send_emails() -> int:
    return Runner(queues=["default"]).run_ready()


# --- Who is told -------------------------------------------------------------


def test_people_are_told_when_they_are_added_to_a_project(world: World) -> None:
    admin, outsider = world.users["admin"], world.users["outsider"]
    org.grant_project_role(world.project, ProjectRole.EDITOR, user=outsider, by=admin)
    (note,) = inbox(world, "outsider")
    assert note.kind == "project.added"
    assert note.title == "Admin added you to Kinase Atlas as Editor"
    assert note.path == "/projects/kinase-atlas/"
    assert (note.project, note.actor, note.read_at) == (world.project, admin, None)
    # The person who made the change isn't told about it.
    assert inbox(world, "admin") == []


def test_role_changes_and_removals_are_told_too(world: World) -> None:
    admin, viewer = world.users["admin"], world.users["viewer"]
    org.grant_project_role(world.project, ProjectRole.EDITOR, user=viewer, by=admin)
    membership = ProjectMembership.objects.get(project=world.project, user=viewer)
    org.remove_project_membership(membership, by=admin)
    changed, removed = inbox(world, "viewer")
    assert changed.kind == "project.role_changed"
    assert changed.title == "Admin made you Editor in Kinase Atlas (you were Viewer)"
    assert removed.kind == "project.removed"
    assert removed.path == "/projects/"


def test_nobody_is_told_about_their_own_change_or_a_whole_teams_grant(world: World) -> None:
    org_admin, admin = world.users["org_admin"], world.users["admin"]
    # An Org Admin joining a project themselves.
    org.grant_project_role(world.project, ProjectRole.ADMIN, user=org_admin, by=org_admin)
    org.grant_project_role(world.project, ProjectRole.VIEWER, team=world.team, by=admin)
    assert not Notification.objects.exists()


def test_team_members_are_told_when_they_join_or_leave(world: World) -> None:
    org_admin, outsider = world.users["org_admin"], world.users["outsider"]
    org.add_team_member(world.team, outsider, by=org_admin)
    org.remove_team_member(world.team, outsider, by=org_admin)
    added, removed = inbox(world, "outsider")
    assert added.title == "Org Admin added you to the team Structural Biology"
    assert added.path == "/teams/structural-biology/"
    assert removed.kind == "team.removed"


def test_org_admins_are_told_when_someone_asks_for_an_app(world: World) -> None:
    client = Client.objects.create(
        name="Claude Code", client_type="public", authorization_grant_type="authorization-code"
    )
    oauth_services.request_approval(client, by=world.users["editor"])
    (note,) = inbox(world, "org_admin")
    assert note.kind == "oauth.app_awaiting_approval"
    assert note.title == "Editor asks you to approve Claude Code"
    assert note.path == f"/settings/apps/{client.pk}/"
    # Deactivated Org Admins aren't told.
    assert inbox(world, "inactive") == []


def test_service_accounts_and_deactivated_people_are_never_told(world: World) -> None:
    robot = service_accounts.create("LIMS sync", "", by=world.users["org_admin"])
    sent = services.notify(
        [robot, world.users["inactive"]], kinds.PROJECT_ADDED, title="Hello", path="/"
    )
    assert sent == []


def test_notifications_only_lead_to_pages_on_this_site(world: World) -> None:
    for path in ("https://evil.example/", "//evil.example/", "projects/x/"):
        with pytest.raises(ValueError, match="paths on this site"):
            services.notify([world.users["viewer"]], kinds.PROJECT_ADDED, title="x", path=path)
    with pytest.raises(IntegrityError), transaction.atomic():
        Notification.objects.create(
            recipient=world.users["viewer"], kind="project.added", title="x", path="//evil/"
        )


def test_kinds_must_be_registered_once_with_a_proper_name() -> None:
    with pytest.raises(ValueError, match="Register it first"):
        kinds.get("dna.sequenced")
    with pytest.raises(ValueError, match="look like"):
        kinds.register(kinds.Kind("Sequenced", "x", "x", group="DNA"))
    with pytest.raises(ValueError, match="already registered"):
        kinds.register(kinds.Kind("project.added", "Different", "x", group="Projects"))
    # Registering the same kind again (a module imported twice) is fine.
    assert kinds.register(kinds.PROJECT_ADDED) is kinds.PROJECT_ADDED


def test_notifications_belong_to_the_change_that_made_them(world: World) -> None:
    with pytest.raises(RuntimeError), transaction.atomic():
        org.add_team_member(world.team, world.users["outsider"], by=world.users["org_admin"])
        raise RuntimeError("rolled back")
    assert not Notification.objects.exists()


# --- The inbox ------------------------------------------------------------------


def notes_for(world: World, principal: str, count: int, **extra: Any) -> list[Notification]:
    user = world.users[principal]
    return [
        services.notify([user], kinds.PROJECT_ADDED, title=f"Note {n}", path="/", **extra)[0]
        for n in range(count)
    ]


def test_the_inbox_is_newest_first_and_pages_stay_stable(world: World) -> None:
    notes_for(world, "viewer", 3)
    api = Api(world.browser("viewer"))
    first = api.get("/notifications", limit=2).json()
    assert [n["title"] for n in first["items"]] == ["Note 2", "Note 1"]
    # A new one arriving doesn't push anything onto the next page.
    notes_for(world, "viewer", 1)
    rest = api.get("/notifications", limit=2, cursor=first["next_cursor"]).json()
    assert [n["title"] for n in rest["items"]] == ["Note 0"]
    assert rest["next_cursor"] is None


def test_reading_notifications(world: World) -> None:
    first, _, third = notes_for(world, "viewer", 3)
    api = Api(world.browser("viewer"))
    assert api.get("/notifications/summary").json() == {"unread": 3}

    marked = api.patch(f"/notifications/{first.pk}", {"read": True})
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None
    assert [n["title"] for n in api.get("/notifications", unread=True).json()["items"]] == [
        "Note 2",
        "Note 1",
    ]
    assert api.patch(f"/notifications/{first.pk}", {"read": False}).json()["read_at"] is None

    # "Mark all read" covers only what the person had seen.
    Notification.objects.filter(pk=third.pk).update(created_at=timezone.now() + timedelta(hours=1))
    result = api.post("/notifications/read", {"up_to": timezone.now().isoformat()}).json()
    assert result == {"marked": 2, "unread": 1}


def test_nobody_reads_anyone_elses_notifications(world: World) -> None:
    (note,) = notes_for(world, "viewer", 1)
    editor = Api(world.browser("editor"))
    assert editor.get("/notifications").json()["items"] == []
    problem(editor.patch(f"/notifications/{note.pk}", {"read": True}), 404, "not_found")
    assert editor.post("/notifications/read", {}).json() == {"marked": 0, "unread": 0}
    note.refresh_from_db()
    assert note.read_at is None


def test_notifications_about_a_project_go_when_access_does(world: World) -> None:
    viewer, admin = world.users["viewer"], world.users["admin"]
    (about_project,) = notes_for(world, "viewer", 1, project=world.project)
    membership = ProjectMembership.objects.get(project=world.project, user=viewer)
    org.remove_project_membership(membership, by=admin)
    titles = [
        n["title"] for n in Api(world.browser("viewer")).get("/notifications").json()["items"]
    ]
    # The one saying they were removed stays; the one about the project's contents goes.
    assert titles == ["Admin removed you from Kinase Atlas"]
    assert about_project.title not in titles


def test_tokens_see_only_their_projects_notifications_and_need_write_to_mark(
    world: World,
) -> None:
    other = Project.objects.create(team=world.team, name="Second Screen")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    notes_for(world, "editor", 1, project=world.project)
    notes_for(world, "editor", 1, project=other)
    notes_for(world, "editor", 1)  # About no project: an org-wide matter.
    _, tokens = signed_in_token(world, scope="read", projects=[world.project])
    token = tokens["access_token"]
    items = bearer(token, "GET", "/notifications").json()["items"]
    assert [n["project"]["name"] for n in items] == ["Kinase Atlas"]
    response = bearer(token, "PATCH", f"/notifications/{items[0]['id']}", {"read": True})
    problem(response, 403, "insufficient_scope")
    problem(bearer(token, "POST", "/notifications/read", {}), 403, "insufficient_scope")


# --- Email --------------------------------------------------------------------


@pytest.mark.usefixtures("email_on")
def test_notifications_are_emailed_once(world: World) -> None:
    org.grant_project_role(
        world.project, ProjectRole.EDITOR, user=world.users["outsider"], by=world.users["admin"]
    )
    assert send_emails() == 1
    (message,) = mail.outbox
    assert isinstance(message, mail.EmailMultiAlternatives)
    assert message.to == ["outsider@example.com"]
    assert message.subject == "[Example Bio] Admin added you to Kinase Atlas as Editor"
    assert "https://iki.example.com/projects/kinase-atlas/" in message.body
    assert "https://iki.example.com/account/notifications/" in message.body
    assert message.extra_headers["Auto-Submitted"] == "auto-generated"
    assert message.extra_headers["X-Auto-Response-Suppress"] == "All"
    html, _ = message.alternatives[0]
    assert isinstance(html, str)
    assert "Kinase Atlas" in html

    # A retried task never sends it twice.
    (task,) = TaskRecord.objects.all()
    TaskRecord.objects.filter(pk=task.pk).update(status=TaskResultStatus.READY)
    send_emails()
    assert len(mail.outbox) == 1


@pytest.mark.usefixtures("email_on")
def test_emails_can_leave_out_the_details(world: World) -> None:
    Installation.objects.filter(pk=1).update(email_details=False)
    org.grant_project_role(
        world.project, ProjectRole.EDITOR, user=world.users["outsider"], by=world.users["admin"]
    )
    send_emails()
    (message,) = mail.outbox
    assert isinstance(message, mail.EmailMultiAlternatives)
    assert message.subject == "[Example Bio] You have a new notification"
    assert "Kinase Atlas" not in message.body
    html, _ = message.alternatives[0]
    assert isinstance(html, str)
    assert "Kinase Atlas" not in html
    assert "https://iki.example.com/projects/kinase-atlas/" in message.body


@pytest.mark.usefixtures("email_on")
def test_people_choose_which_notifications_they_get_by_email(world: World) -> None:
    outsider = world.users["outsider"]
    EmailPreference.objects.create(user=outsider, kind="project.added", email=False)
    org.grant_project_role(
        world.project, ProjectRole.EDITOR, user=outsider, by=world.users["admin"]
    )
    assert not TaskRecord.objects.exists()
    assert len(inbox(world, "outsider")) == 1  # Still shown in Iki.


def test_nothing_is_emailed_while_email_is_off(world: World) -> None:
    org.grant_project_role(
        world.project, ProjectRole.EDITOR, user=world.users["outsider"], by=world.users["admin"]
    )
    assert not TaskRecord.objects.exists()


@pytest.mark.usefixtures("email_on")
def test_turning_email_off_after_the_fact_stops_the_email(world: World) -> None:
    outsider = world.users["outsider"]
    org.grant_project_role(
        world.project, ProjectRole.EDITOR, user=outsider, by=world.users["admin"]
    )
    EmailPreference.objects.create(user=outsider, kind="project.added", email=False)
    send_emails()
    assert mail.outbox == []
    assert TaskRecord.objects.get().return_value == "turned off"


@pytest.mark.usefixtures("email_on")
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (smtplib.SMTPResponseException(421, b"try later"), TaskResultStatus.READY),
        (smtplib.SMTPResponseException(550, b"no such mailbox"), TaskResultStatus.FAILED),
        (smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"no")}), TaskResultStatus.FAILED),
        (ConnectionRefusedError(), TaskResultStatus.READY),
    ],
)
def test_temporary_mail_errors_are_retried_and_permanent_ones_arent(
    world: World, monkeypatch: pytest.MonkeyPatch, error: Exception, status: str
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr("django.core.mail.EmailMultiAlternatives.send", refuse)
    org.grant_project_role(
        world.project, ProjectRole.EDITOR, user=world.users["outsider"], by=world.users["admin"]
    )
    send_emails()
    task = TaskRecord.objects.get()
    assert task.status == status
    assert Notification.objects.get().emailed_at is None


# --- Email choices: API and page -------------------------------------------------


def test_email_choices_through_the_api(world: World) -> None:
    api = Api(world.browser("viewer"))
    body = api.get("/me/notification-email").json()
    assert body["email_available"] is False
    assert body["address"] == "viewer@example.com"
    by_kind = {k["kind"]: k for k in body["kinds"]}
    assert by_kind["project.added"]["email"] is True
    # Org Admin matters are only offered to Org Admins.
    assert "oauth.app_awaiting_approval" not in by_kind

    changed = api.patch("/me/notification-email", {"email": {"project.added": False}}).json()
    assert {k["kind"]: k["email"] for k in changed["kinds"]}["project.added"] is False
    event = AuditEvent.objects.get(action="notifications.email_preferences_changed")
    assert event.data == {"project.added": {"from": True, "to": False}}

    refused = api.patch("/me/notification-email", {"email": {"oauth.app_awaiting_approval": True}})
    body = problem(refused, 422, "validation_failed")
    assert body["errors"][0]["pointer"] == "#/email"


def test_the_preferences_page_saves_choices(world: World) -> None:
    browser = world.browser("org_admin")
    page = browser.get("/account/notifications/")
    assert page.status_code == 200
    text = page.content.decode()
    assert "An app is waiting for approval" in text
    assert "hasn’t set up email" in text
    response = post(browser, "/account/notifications/", {"email:project.added": "on"})
    assert response.status_code == 302
    choices = services.email_choices(world.users["org_admin"])
    assert choices["project.added"] is True
    assert choices["team.added"] is False
    assert browser.get("/account/")["Location"] == "/account/notifications/"


# --- The organization's email settings ----------------------------------------------


def test_only_org_admins_open_the_email_settings(world: World) -> None:
    assert world.browser("admin").get("/settings/email/").status_code == 403
    page = world.browser("org_admin").get("/settings/email/")
    assert page.status_code == 200
    assert "Email isn’t set up" in page.content.decode()


def test_org_admins_choose_whether_emails_carry_details(world: World) -> None:
    browser = world.browser("org_admin")
    post(browser, "/settings/email/", {"action": "details"})
    assert Installation.get().email_details is False
    event = AuditEvent.objects.get(action="notifications.email_details_changed")
    assert event.data == {"details": False}


@pytest.mark.usefixtures("email_on")
def test_org_admins_can_send_themselves_a_test_email(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = world.browser("org_admin")
    response = post(browser, "/settings/email/", {"action": "test"})
    assert response.status_code == 302
    (message,) = mail.outbox
    assert message.to == ["org_admin@example.com"]

    def broken(*args: Any, **kwargs: Any) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)
    page = browser.get(post(browser, "/settings/email/", {"action": "test"})["Location"])
    assert "Couldn’t send it: SMTPAuthenticationError" in page.content.decode()


# --- Pages ---------------------------------------------------------------------


def test_server_pages_show_the_unread_count(world: World) -> None:
    notes_for(world, "org_admin", 2)
    text = world.browser("org_admin").get("/teams/").content.decode()
    assert 'aria-label="Notifications, 2 unread"' in text
    assert '<span class="badge" aria-hidden="true">2</span>' in text
    assert ">Settings</a>" in text


def test_the_badge_caps_large_counts(world: World) -> None:
    notes_for(world, "org_admin", 100)
    text = world.browser("org_admin").get("/teams/").content.decode()
    assert 'aria-label="Notifications, 100 unread"' in text
    assert '<span class="badge" aria-hidden="true">99+</span>' in text


def test_the_inbox_is_the_web_apps_page(world: World) -> None:
    response = world.browser("viewer").get("/notifications/")
    assert response.status_code == 200
    assert 'id="root"' in response.content.decode()

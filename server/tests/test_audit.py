"""The audit log: what is recorded, that it can't be changed, and the app's database role."""

import os
from collections.abc import Callable, Iterator
from io import StringIO

import pytest
from api_client import Api
from browser import Browser, complete_setup, path_of, sign_in
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection, transaction
from fake_idp import FakeIdP
from org_world import World, build_world

from iki_server import health
from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.audit.models import AuditEvent
from iki_server.core import db_roles

pytestmark = pytest.mark.django_db

ADMIN = {"sub": "admin-1", "email": "ada@example.com", "name": "Ada Lovelace"}
SCIENTIST = {"sub": "sci-1", "email": "rosalind@example.com", "name": "Rosalind Franklin"}
#: Roles are the server's, not a database's: each parallel worker creates its own.
APP_ROLE = f"iki_test_app_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("occurred_at", "id").values_list("action", flat=True))


# --- What is recorded ------------------------------------------------------------


def test_setup_and_sign_ins_are_recorded(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    assert actions() == [
        "setup.link_created",
        "user.created",
        "user.signed_in",
        "org_role.granted",
        "identity_provider.enabled",
        "setup.completed",
    ]
    link = AuditEvent.objects.get(action="setup.link_created")
    assert (link.via, link.actor, link.data["recovery"]) == ("cli", None, False)
    done = AuditEvent.objects.get(action="setup.completed")
    assert done.actor == User.objects.get(email="ada@example.com")
    assert (done.via, done.client_ip) == ("web", "127.0.0.1")
    assert done.request_id

    other = Browser()
    sign_in(other, fake_idp, SCIENTIST)
    other.submit("/teams/", action="/logout/")
    assert actions()[-3:] == ["user.created", "user.signed_in", "user.signed_out"]
    signed_in = AuditEvent.objects.filter(action="user.signed_in").first()  # newest first
    assert signed_in is not None and signed_in.actor_label == "rosalind@example.com"


def test_refused_sign_ins_are_recorded(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    impostor = {"sub": "someone-else", "email": "ADA@example.com", "name": "Not Ada"}
    response = sign_in(Browser(), fake_idp, impostor)
    assert response.status_code == 403
    event = AuditEvent.objects.get(action="user.sign_in_refused")
    assert event.actor is None
    assert event.data == {"reason": "email_in_use", "email": "ADA@example.com"}


def test_forged_callbacks_are_not_recorded(client: Browser, fake_idp: FakeIdP) -> None:
    """Anyone can send a forged callback, so it must not be able to grow the audit log."""
    complete_setup(client, fake_idp, ADMIN)
    before = AuditEvent.objects.count()
    from allauth.socialaccount.models import SocialAccount

    provider = SocialAccount.objects.get().provider
    Browser().get(f"/accounts/oidc/{provider}/login/callback/?code=x&state=forged")
    assert AuditEvent.objects.count() == before


def test_deactivation_and_revoked_sessions_are_recorded() -> None:
    world = build_world()
    world.browser("viewer").get("/")
    admin = world.browser("org_admin")
    admin.submit(f"/people/{world.users['viewer'].pk}/", {"action": "deactivate"})
    assert actions()[-2:] == ["user.sessions_revoked", "user.deactivated"]
    revoked = AuditEvent.objects.get(action="user.sessions_revoked")
    assert revoked.data == {"sessions": 1, "tokens_revoked": 0, "reason": "deactivated"}


def test_key_rotation_is_recorded_as_a_command() -> None:
    call_command("rotate_encryption", stdout=StringIO())
    event = AuditEvent.objects.get()
    assert (event.action, event.via, event.data) == ("encryption.rotated", "cli", {"secrets": 0})


def test_unknown_actions_are_programming_errors() -> None:
    with pytest.raises(ValueError, match="unknown audit action"):
        audit.record("project.teleported")


def test_events_reach_the_log_stream_only_once_committed(
    caplog: pytest.LogCaptureFixture,
    django_capture_on_commit_callbacks: Callable[..., object],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[attr-defined]
        event = audit.record("encryption.rotated", data={"secrets": 2})
    (logged,) = [r.msg for r in caplog.records if r.name == "iki.audit"]
    assert logged["event"] == "encryption.rotated"  # type: ignore[index]
    assert logged["audit_id"] == str(event.id)  # type: ignore[index]
    assert logged["data"] == {"secrets": 2}  # type: ignore[index]

    caplog.clear()
    with pytest.raises(RuntimeError), transaction.atomic():
        audit.record("encryption.rotated")
        raise RuntimeError
    assert not [r for r in caplog.records if r.name == "iki.audit"]


# --- Append-only ------------------------------------------------------------------


@pytest.fixture
def event() -> AuditEvent:
    return audit.record("encryption.rotated", data={"secrets": 1})


@pytest.mark.parametrize(
    "statement",
    ["UPDATE iki_audit_event SET action = 'x'", "DELETE FROM iki_audit_event"],
)
def test_no_role_can_change_or_delete_events(event: AuditEvent, statement: str) -> None:
    """Even the table's owner (here, a superuser) is refused by the trigger."""
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        connection.cursor().execute(statement)
    assert AuditEvent.objects.get().action == "encryption.rotated"


def test_no_role_can_truncate_events(event: AuditEvent) -> None:
    # The test database turns this trigger off so tests can be flushed; check it in place.
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        cursor = connection.cursor()
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # no pending FK checks block ALTER
        cursor.execute("ALTER TABLE iki_audit_event ENABLE TRIGGER iki_audit_event_no_truncate")
        cursor.execute("TRUNCATE iki_audit_event")


def test_django_cant_delete_events_either(event: AuditEvent) -> None:
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        event.delete()


# --- The app's database role --------------------------------------------------------


@pytest.fixture
def as_app_role() -> Iterator[Callable[[str], None]]:
    """Creates and grants the app role; runs statements as it, inside the test's transaction."""
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {APP_ROLE} NOLOGIN")
    call_command("grant_app_privileges", "--role", APP_ROLE, stdout=StringIO())

    def run(statement: str) -> None:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {APP_ROLE}")
            cursor.execute(statement)

    yield run


def test_the_app_role_reads_and_writes_rows(as_app_role: Callable[[str], None]) -> None:
    as_app_role("SELECT count(*) FROM iki_project")
    as_app_role("UPDATE iki_project SET description = description")
    as_app_role("DELETE FROM iki_session WHERE expire_date < now()")
    as_app_role(
        "INSERT INTO iki_audit_event (id, occurred_at, action, actor_label, via, target_type,"
        " target_id, target_label, data, request_id)"
        " VALUES (gen_random_uuid(), now(), 'encryption.rotated', '', 'cli', '', '', '', '{}', '')"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE iki_audit_event SET action = 'x'",
        "DELETE FROM iki_audit_event",
        "TRUNCATE iki_audit_event",
        "TRUNCATE iki_project CASCADE",
        "CREATE TABLE sneaky (id int)",
        "ALTER TABLE iki_project ADD COLUMN sneaky int",
        "DROP TABLE iki_session",
        "ALTER TABLE iki_audit_event DISABLE TRIGGER iki_audit_event_no_change",
        "INSERT INTO django_migrations (app, name, applied) VALUES ('x', 'y', now())",
        "DELETE FROM django_migrations",
    ],
)
def test_the_app_role_cant_change_schema_history_or_the_audit_log(
    as_app_role: Callable[[str], None], statement: str
) -> None:
    with pytest.raises(DatabaseError, match=r"permission denied|must be owner"):
        as_app_role(statement)


def test_a_least_privileged_role_passes_the_readiness_check(
    as_app_role: Callable[[str], None],
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {APP_ROLE}")
        assert db_roles.problems() == []


def test_a_powerful_role_is_named_and_never_ready(settings: object) -> None:
    found = db_roles.problems()  # the test suite connects as a superuser
    assert "is a superuser" in found
    assert "owns tables (or is a member of their owner)" in found
    settings.IKI_DB_REQUIRE_LEAST_PRIVILEGE = True  # type: ignore[attr-defined]
    health._RoleState.checked = False
    assert health.readiness()["database_role"] == "fail"


def test_grants_refuse_to_run_without_a_separate_app_role() -> None:
    with pytest.raises(CommandError, match="both"):
        call_command("grant_app_privileges", "--role", connection.settings_dict["USER"])
    with pytest.raises(CommandError, match="no role"):
        call_command("grant_app_privileges", "--role", "nobody_here", "--timeout", "0")


def test_grants_find_the_role_in_the_app_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.core.management.commands.grant_app_privileges import app_role

    monkeypatch.setenv("IKI_APP_DATABASE_URL", "postgres://iki_app:secret@db:5432/iki")
    assert app_role(None) == "iki_app"
    monkeypatch.setenv("IKI_DB_APP_ROLE", "explicit")
    assert app_role(None) == "explicit"
    assert app_role("flag") == "flag"


# --- The audit page ------------------------------------------------------------------


@pytest.fixture
def world() -> World:
    return build_world()


def test_auditors_read_and_filter_the_log(world: World) -> None:
    from iki_server.org import services

    services.create_team("Genomics", "", by=world.users["org_admin"])
    services.update_project(world.project, "Kinase Atlas", "x", by=world.users["admin"])
    auditor = world.browser("auditor")
    page = auditor.get("/audit/").content.decode()
    assert "Created a team: Genomics" in page
    assert "Changed a project: Kinase Atlas" in page
    assert "org_admin@example.com" in page

    filtered = auditor.get("/audit/", {"action": "team.created"}).content.decode()
    assert "Created a team: Genomics" in filtered
    assert "Changed a project: Kinase Atlas" not in filtered
    by_project = auditor.get("/audit/", {"project": str(world.project.pk)}).content.decode()
    assert "Changed a project: Kinase Atlas" in by_project
    assert "Created a team: Genomics" not in by_project
    by_person = auditor.get("/audit/", {"person": "admin@ex"}).content.decode()
    assert "Changed a project: Kinase Atlas" in by_person


def test_the_log_pages_with_a_stable_cursor(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import html
    import re

    from iki_server.audit import views

    monkeypatch.setattr(views, "PAGE_SIZE", 2)
    auditor = world.browser("auditor")
    for _ in range(4):
        audit.record("encryption.rotated", data={"secrets": 1})
    seen: list[str] = []
    url = "/audit/"
    while url:
        page = auditor.get(url).content.decode()
        ids = re.findall(r"<dt>Event ID</dt><dd><code>([0-9a-f-]{36})</code>", page)
        assert 1 <= len(ids) <= 2
        seen += ids
        older = re.search(r'href="(\?[^"]*before=[^"]+)"', page)
        url = f"/audit/{html.unescape(older.group(1))}" if older else ""
    every = [str(pk) for pk in AuditEvent.objects.values_list("pk", flat=True)]
    assert seen == every  # newest first, each exactly once
    assert auditor.get("/audit/?before=not-a-uuid").status_code == 200


def test_auditor_link_only_for_auditors(world: World) -> None:
    assert 'href="/audit/"' in world.browser("auditor").get("/teams/").content.decode()
    assert 'href="/audit/"' not in world.browser("org_admin").get("/teams/").content.decode()
    assert Api(world.browser("auditor")).get("/me").json()["can"]["read_audit"] is True
    assert path_of(world.browser("anonymous").get("/audit/")).startswith("/login/")

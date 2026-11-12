"""An agent's changes name the person and the agent (ADR 0072): the client, the person's
grant, and what the agent says it is, on every record, in the audit log, and on the
person's account."""

from typing import Any, cast

import pytest
from api_client import Api
from django.db import DatabaseError, connection, transaction
from org_world import World, build_world
from test_oauth import bearer, signed_in_token
from test_webhooks import make

from iki_server.accounts import service_accounts
from iki_server.api.schemas import agent_out
from iki_server.audit import log as audit
from iki_server.audit.models import Agent, AuditEvent
from iki_server.entities.models import Revision
from iki_server.oauth import services
from iki_server.oauth.models import Consent
from iki_server.org.models import ProjectMembership, ProjectRole
from iki_server.webhooks.models import Delivery

pytestmark = pytest.mark.django_db

AMINO = "ACDEFGHIKLMNPQRSTVWY"


@pytest.fixture
def world() -> World:
    return build_world()


def create(world: World, token: str, name: str = "ABL1", **headers: str) -> Any:
    made = bearer(
        token,
        "POST",
        "/entities",
        {
            "type": "protein.Protein",
            "project": str(world.project.pk),
            # Each its own sequence, so none is found already there.
            "values": {"sequence": "MKV" + "".join(AMINO[ord(c) % 20] for c in name), "name": name},
        },
        headers=headers,
    )
    assert made.status_code == 201, made.content
    return made.json()


def describe(world: World, token: str, text: str, **headers: str) -> None:
    """Changes the project's description: audited (an entity's changes are revisions)."""
    changed = bearer(
        token, "PATCH", f"/projects/{world.project.pk}", {"description": text}, headers=headers
    )
    assert changed.status_code == 200, changed.content


def test_an_agents_change_names_the_person_and_the_agent(world: World) -> None:
    client, tokens = signed_in_token(world, "editor")
    consent = Consent.objects.get(user=world.users["editor"], client=client)
    token = tokens["access_token"]
    made = create(world, token, **{"Iki-Agent-Model": "claude-opus-5-5\x00 "})

    revision = Revision.objects.select_related("agent").get(entity__hrid=made["hrid"])
    assert revision.user == world.users["editor"]  # the person
    agent = revision.agent
    assert agent is not None
    assert (agent.client_id, agent.client_name, agent.grant, agent.model) == (
        client.client_id,
        client.display_name(),
        consent.grant,
        "claude-opus-5-5",  # as it said, made printable
    )
    assert agent.session == agent.software == ""  # not through MCP

    revisions = bearer(token, "GET", f"/entities/{made['hrid']}/revisions").json()["items"]
    assert revisions[0]["agent"] == {
        "client_id": client.client_id,
        "name": client.display_name(),
        "grant": str(consent.grant),
        "mcp_session": None,
        "reported": {"software": None, "model": "claude-opus-5-5"},
    }

    # The same agent is stored once; another model is another agent.
    create(world, token, name="ABL2", **{"Iki-Agent-Model": "claude-opus-5-5"})
    create(world, token, name="ABL3", **{"Iki-Agent-Model": "claude-sonnet-5-5"})
    assert sorted(Agent.objects.values_list("model", flat=True)) == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]


def test_an_agents_audited_change_names_the_person_and_the_agent(world: World) -> None:
    client, tokens = signed_in_token(world, "admin")
    describe(world, tokens["access_token"], "Kinases", **{"Iki-Agent-Model": "claude-opus-5-5"})
    event = AuditEvent.objects.select_related("agent").get(action="project.updated")
    assert event.actor == world.users["admin"]
    assert event.agent is not None
    assert (event.agent.client_id, event.agent.model) == (client.client_id, "claude-opus-5-5")
    assert event.oauth_client == f"{client.display_name()} ({client.client_id[:40]})"


def test_changes_in_the_web_app_name_no_agent(world: World) -> None:
    api = Api(world.browser("editor"))
    made = api.post(
        "/entities",
        {
            "type": "protein.Protein",
            "project": str(world.project.pk),
            "values": {"sequence": "MKVLA", "name": "ABL1"},
        },
    )
    assert made.status_code == 201, made.content
    assert Revision.objects.get(entity__hrid=made.json()["hrid"]).agent is None
    revisions = api.get(f"/entities/{made.json()['hrid']}/revisions").json()
    assert revisions["items"][0]["agent"] is None


def test_a_service_accounts_own_credential_has_no_grant(world: World) -> None:
    from test_oauth import basic, form_post

    admin = world.users["org_admin"]
    account = service_accounts.create("LIMS sync", "", by=admin)
    ProjectMembership.objects.create(project=world.project, user=account, role=ProjectRole.EDITOR)
    credential = services.create_credential(account, "LIMS server", ["read", "write"], by=admin)
    tokens = form_post(
        "/oauth/token/",
        {"grant_type": "client_credentials", "scope": "read write"},
        Authorization=basic(credential.client.client_id, credential.secret),
    ).json()
    made = create(world, tokens["access_token"])
    agent = Revision.objects.get(entity__hrid=made["hrid"]).agent
    assert agent is not None
    assert (agent.client_id, agent.grant) == (credential.client.client_id, None)


def test_a_grant_keeps_its_id_until_it_is_revoked(world: World) -> None:
    editor = world.users["editor"]
    client, _ = signed_in_token(world, "editor")
    first = Consent.objects.get(user=editor, client=client).grant
    # Narrowing it is the same grant.
    services.grant(editor, client, services.Grant(["read"], True, []))
    assert Consent.objects.get(user=editor, client=client).grant == first
    granted = AuditEvent.objects.filter(action="oauth.consent_granted").order_by("occurred_at")
    assert [e.data["grant"] for e in granted] == [str(first), str(first)]
    # Given again after it was revoked, it's another.
    services.revoke_consent(Consent.objects.get(user=editor, client=client), by=editor)
    revoked = AuditEvent.objects.get(action="oauth.consent_revoked")
    assert revoked.data["grant"] == str(first)
    services.grant(editor, client, services.Grant(["read"], True, []))
    assert Consent.objects.get(user=editor, client=client).grant != first


def test_agents_cant_be_changed_or_deleted(world: World) -> None:
    _, tokens = signed_in_token(world, "editor")
    create(world, tokens["access_token"])
    agent = Agent.objects.get()
    for statement in (
        "UPDATE iki_audit_agent SET model = 'gpt'",
        "DELETE FROM iki_audit_agent",
    ):
        with (
            pytest.raises(DatabaseError, match="append-only"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement)
    agent.refresh_from_db()
    assert agent.model == ""


def test_older_records_give_the_client_their_label_names() -> None:
    assert agent_out(None, "Claude Code (c-81f3)") == {
        "client_id": "c-81f3",
        "name": "Claude Code",
        "grant": None,
        "mcp_session": None,
        "reported": {"software": None, "model": None},
    }
    assert agent_out(None, "An app") == {
        "client_id": None,
        "name": "An app",
        "grant": None,
        "mcp_session": None,
        "reported": {"software": None, "model": None},
    }
    assert agent_out(None, "") is None


def test_webhooks_say_which_agent_made_a_change(world: World) -> None:
    make(world, "https://siem.example.com/iki", ["entity.created"])
    client, tokens = signed_in_token(world, "editor")
    create(world, tokens["access_token"])
    (delivery,) = Delivery.objects.filter(event="entity.created")
    sent = delivery.payload["data"]
    assert sent["actor"]["email"] == world.users["editor"].email
    assert sent["agent"]["client_id"] == client.client_id
    assert sent["agent"]["grant"] == str(
        Consent.objects.get(user=world.users["editor"], client=client).grant
    )


class TokenApi:
    """Calls the API as an agent does: with its token."""

    def __init__(self, token: str) -> None:
        self.token = token

    def post(self, path: str, body: Any = None) -> Any:
        return bearer(self.token, "POST", path, body)

    def get(self, path: str) -> Any:
        return bearer(self.token, "GET", path)


def test_a_file_an_agent_adds_names_it_once_checked(world: World, s3: Any) -> None:
    from test_files import add

    from iki_server.files.models import File, Upload

    client, tokens = signed_in_token(world, "editor")
    agent = cast("Api", TokenApi(tokens["access_token"]))  # the same calls, with a token
    upload = add(agent, world, b"ATOM      1  N   MET A   1\n")
    # Checked later, by the task runner: it names the agent that uploaded it all the same.
    started = Upload.objects.select_related("agent").get(pk=upload["id"])
    file = File.objects.select_related("agent").get(pk=upload["file"]["id"])
    assert started.agent is not None and started.agent.client_id == client.client_id
    assert file.agent == started.agent
    event = AuditEvent.objects.get(action="file.added")
    assert (event.actor, event.agent) == (world.users["editor"], started.agent)
    assert event.oauth_client == started.agent.label()
    shown = bearer(tokens["access_token"], "GET", f"/files/{file.pk}").json()
    assert shown["agent"]["client_id"] == client.client_id


# --- The audit log ---------------------------------------------------------------------------


def test_the_audit_log_finds_what_an_agent_or_a_grant_did(world: World) -> None:
    admin = world.users["admin"]
    client, tokens = signed_in_token(world, "admin")
    _, others = signed_in_token(world, "admin")  # another app
    describe(world, tokens["access_token"], "Kinases", **{"Iki-Agent-Model": "claude-opus-5-5"})
    describe(world, others["access_token"], "Not this one")
    # Before agents were stored, an event named its client only in its label.
    with audit.using(audit.Context(via="api", oauth_client=f"Old name ({client.client_id})")):
        audit.record("project.archived", actor=admin)
    grant = Consent.objects.get(user=admin, client=client).grant

    auditor = world.browser("auditor")
    page = auditor.get("/audit/", {"agent": client.client_id})
    assert page.status_code == 200, page.content
    shown = sorted((e.action, a and a["name"]) for e, _, a in page.context["events"])
    # Not the other app's, nor the consent given in the web app.
    assert shown == [("project.archived", "Old name"), ("project.updated", client.display_name())]

    by_grant = auditor.get("/audit/", {"grant": str(grant)})
    assert [e.action for e, _, _ in by_grant.context["events"]] == ["project.updated"]
    text = by_grant.content.decode()
    assert f"{admin.email}, through {client.display_name()}" in text
    assert 'claude-opus-5-5 <span class="muted">(as the agent reports it)</span>' in text
    assert f'href="?grant={grant}"' in text


def test_people_see_what_each_of_their_grants_did(world: World) -> None:
    editor = world.users["editor"]
    client, tokens = signed_in_token(world, "editor")
    create(world, tokens["access_token"])
    grant = Consent.objects.get(user=editor, client=client).grant
    # What it did, as the audit log has it, with the editor as actor.
    ProjectMembership.objects.filter(project=world.project, user=editor).update(
        role=ProjectRole.ADMIN
    )
    describe(world, tokens["access_token"], "Kinases")

    browser = world.browser("editor")
    apps = browser.get("/account/apps/").content.decode()
    assert f'href="/account/apps/grants/{grant}/"' in apps
    page = browser.get(f"/account/apps/grants/{grant}/")
    assert page.status_code == 200
    assert "Changed a project: Kinase Atlas" in page.content.decode()

    # Still there once revoked, and nobody else's to read.
    services.revoke_consent(Consent.objects.get(user=editor, client=client), by=editor)
    page = browser.get(f"/account/apps/grants/{grant}/")
    assert "You’ve since revoked it." in page.content.decode()
    assert world.browser("admin").get(f"/account/apps/grants/{grant}/").status_code == 404


def test_a_grant_that_did_nothing_yet_says_so(world: World) -> None:
    client, _ = signed_in_token(world, "editor")
    grant = Consent.objects.get(user=world.users["editor"], client=client).grant
    page = world.browser("editor").get(f"/account/apps/grants/{grant}/")
    assert "It hasn’t done anything yet." in page.content.decode()

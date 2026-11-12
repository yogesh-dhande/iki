"""Unattended agents act for a person by delegation (ADR 0073): a service account's
credential, allowed by an Org Admin, exchanges a person's delegation for a token that acts
for them, within what both may, until the delegation ends."""

import itertools
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from org_world import World, build_world
from test_agents import create
from test_oauth import basic, bearer, form_post

from iki_server.accounts import service_accounts
from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Revision
from iki_server.oauth import exchange, services
from iki_server.oauth.models import Client, Consent
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import catalog

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


_bots = itertools.count(1)


class Bot:
    """A service account with a credential allowed to act for people."""

    def __init__(self, world: World, *, acts_for_people: bool = True, role: str = "editor"):
        admin = world.users["org_admin"]
        self.account: User = service_accounts.create(
            f"Triage bot {next(_bots)}", "Nightly triage", by=admin
        )
        ProjectMembership.objects.create(
            project=world.project, user=self.account, role=ProjectRole[role.upper()]
        )
        made = services.create_credential(
            self.account,
            "Triage server",
            ["read", "write", "run"],
            by=admin,
            acts_for_people=acts_for_people,
        )
        self.client: Client = made.client
        self.secret = made.secret

    def exchange(self, grant: Any, **params: str) -> Any:
        return form_post(
            "/oauth/token/",
            {
                "grant_type": exchange.GRANT_TYPE,
                "subject_token": str(grant),
                "subject_token_type": exchange.DELEGATION,
                **params,
            },
            Authorization=basic(self.client.client_id, self.secret),
        )

    def token(self, grant: Any, **params: str) -> str:
        response = self.exchange(grant, **params)
        assert response.status_code == 200, response.content
        token: str = response.json()["access_token"]
        return token


def delegate(
    world: World,
    bot: Bot,
    principal: str = "editor",
    scopes: tuple[str, ...] = ("read", "write"),
    *,
    days: int = 30,
    projects: list[Project] | None = None,
) -> Consent:
    return services.delegate(
        world.users[principal],
        bot.client,
        services.Grant(list(scopes), projects is None, projects or []),
        expires_at=services.end_of(services.latest_delegation_day() - timedelta(days=90 - days)),
    )


def test_an_agent_acts_for_the_person_who_delegated_to_it(world: World) -> None:
    bot = Bot(world)
    delegation = delegate(world, bot)
    response = bot.exchange(delegation.grant)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["issued_token_type"] == exchange.ACCESS_TOKEN
    assert body["act"] == {"sub": bot.client.client_id}
    assert body["token_type"] == "Bearer"
    assert set(body["scope"].split()) == {"read", "write"}
    assert "refresh_token" not in body
    token = body["access_token"]

    # The change is the person's, through the agent, under the delegation.
    made = create(world, token)
    revision = Revision.objects.select_related("agent").get(entity__hrid=made["hrid"])
    assert revision.user == world.users["editor"]
    assert revision.agent is not None
    assert (revision.agent.client_id, revision.agent.grant) == (
        bot.client.client_id,
        delegation.grant,
    )

    me = bearer(token, "GET", "/me").json()
    assert me["email"] == world.users["editor"].email
    assert me["agent"]["client_id"] == bot.client.client_id
    assert me["agent"]["grant"] == str(delegation.grant)
    assert me["delegation"]["service_account"]["name"] == bot.account.name
    assert datetime.fromisoformat(me["delegation"]["expires_at"]) == delegation.expires_at


def test_the_web_app_and_other_tokens_show_no_delegation(world: World) -> None:
    from api_client import Api

    me = Api(world.browser("editor")).get("/me").json()
    assert (me["agent"], me["delegation"]) == (None, None)


def test_the_token_has_only_the_scopes_both_allow(world: World) -> None:
    bot = Bot(world)
    delegation = delegate(world, bot, scopes=("read",))
    assert bot.exchange(delegation.grant).json()["scope"] == "read"
    refused = bot.exchange(delegation.grant, scope="read write")
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_scope"
    token = bot.token(delegation.grant)
    assert (
        bearer(token, "PATCH", f"/projects/{world.project.pk}", {"description": "x"}).status_code
        == 403
    )


def test_only_credentials_allowed_to_act_for_people_can_exchange(world: World) -> None:
    bot = Bot(world, acts_for_people=False)
    with pytest.raises(Refused, match="can’t act for people"):
        delegate(world, bot)
    # Even with a delegation left from before.
    Consent.objects.create(
        user=world.users["editor"],
        client=bot.client,
        scopes="read",
        all_projects=True,
        expires_at=services.end_of(services.latest_delegation_day()),
    )
    grant = Consent.objects.get(client=bot.client).grant
    refused = bot.exchange(grant)
    assert refused.status_code in (400, 401)
    assert refused.json()["error"] == "unauthorized_client"


def test_an_exchange_needs_a_delegation_to_this_credential(world: World) -> None:
    bot, other = Bot(world), Bot(world)
    theirs = delegate(world, other)
    for grant in (theirs.grant, "not-a-grant", "01900000-0000-7000-8000-000000000000"):
        refused = bot.exchange(grant)
        assert refused.status_code == 400, grant
        assert refused.json()["error"] == "invalid_grant"
    mine = delegate(world, bot)
    for params, error in (
        ({"subject_token_type": exchange.ACCESS_TOKEN}, "invalid_request"),
        ({"actor_token": "x", "actor_token_type": exchange.ACCESS_TOKEN}, "invalid_request"),
    ):
        refused = bot.exchange(mine.grant, **params)
        assert refused.json()["error"] == error, params
    # Without the credential's secret it's nothing.
    stolen = form_post(
        "/oauth/token/",
        {
            "grant_type": exchange.GRANT_TYPE,
            "subject_token": str(mine.grant),
            "subject_token_type": exchange.DELEGATION,
        },
        Authorization=basic(bot.client.client_id, "wrong"),
    )
    assert stolen.status_code == 401


def test_a_delegation_ends_when_it_expires_or_is_revoked(world: World) -> None:
    bot = Bot(world)
    delegation = delegate(world, bot)
    token = bot.token(delegation.grant)
    Consent.objects.filter(pk=delegation.pk).update(
        expires_at=services.end_of(services.latest_delegation_day()) - timedelta(days=200)
    )
    assert bearer(token, "GET", "/me").status_code == 401
    assert bot.exchange(delegation.grant).json()["error"] == "invalid_grant"

    delegation = delegate(world, bot)  # given again
    token = bot.token(delegation.grant)
    services.revoke_consent(delegation, by=world.users["editor"])
    assert bearer(token, "GET", "/me").status_code == 401
    assert bot.exchange(delegation.grant).json()["error"] == "invalid_grant"
    revoked = AuditEvent.objects.get(action="oauth.delegation_revoked")
    assert revoked.data["grant"] == str(delegation.grant)
    assert revoked.data["tokens_revoked"] == 2  # from before it expired, and since


def test_a_delegation_ends_when_either_side_is_deactivated(world: World) -> None:
    from iki_server.org import services as account_services

    for side in ("person", "service account"):
        bot = Bot(world)
        delegation = delegate(world, bot)
        token = bot.token(delegation.grant)
        subject = world.users["editor"] if side == "person" else bot.account
        account_services.set_active(subject, False, by=world.users["org_admin"])
        assert bearer(token, "GET", "/me").status_code == 401, side
        assert bot.exchange(delegation.grant).status_code in (400, 401), side
        account_services.set_active(subject, True, by=world.users["org_admin"])


def test_it_can_do_only_what_both_the_person_and_the_service_account_may(world: World) -> None:
    # The service account only views the project; the person edits it.
    bot = Bot(world, role="viewer")
    token = bot.token(delegate(world, bot).grant)
    assert bearer(token, "GET", f"/projects/{world.project.pk}").status_code == 200
    refused = bearer(
        token, "PATCH", f"/projects/{world.project.pk}", {"description": "Not allowed"}
    )
    assert refused.status_code == 403, refused.content

    # A project only the person is in is out of its reach; one only the service account
    # is in, out of the person's.
    bot = Bot(world)
    mine = Project.objects.create(team=world.team, name="Mine alone")
    ProjectMembership.objects.create(
        project=mine, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    bots = Project.objects.create(team=world.team, name="Bot alone")
    ProjectMembership.objects.create(project=bots, user=bot.account, role=ProjectRole.EDITOR)
    token = bot.token(delegate(world, bot).grant)
    seen = {p["name"] for p in bearer(token, "GET", "/projects").json()["items"]}
    assert world.project.name in seen
    assert {"Mine alone", "Bot alone"}.isdisjoint(seen)
    assert bearer(token, "GET", f"/projects/{mine.pk}").status_code == 404
    assert bearer(token, "GET", f"/projects/{bots.pk}").status_code == 404

    # A powerful person doesn't make the agent an Org Admin, nor its scopes reach admin.
    admin_delegation = delegate(world, bot, "org_admin", ("read",))
    admin_token = bot.token(admin_delegation.grant)
    assert bearer(admin_token, "GET", f"/projects/{mine.pk}").status_code == 404


def test_a_delegation_can_be_held_to_some_projects(world: World) -> None:
    bot = Bot(world)
    other = Project.objects.create(team=world.team, name="Second")
    for user in (world.users["editor"], bot.account):
        ProjectMembership.objects.create(project=other, user=user, role=ProjectRole.EDITOR)
    token = bot.token(delegate(world, bot, projects=[other]).grant)
    seen = {p["name"] for p in bearer(token, "GET", "/projects").json()["items"]}
    assert seen == {"Second"}


def test_delegated_tokens_cant_approve_runs(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_run_approvals import as_agent

    from iki_server.runs import approvals
    from iki_server.runs.models import ApprovalState, RunApproval

    monkeypatch.setattr(approvals, "APPROVAL_TIERS", approvals.APPROVAL_TIERS | {"backend"})

    (run,) = as_agent(world)
    assert run.approval is not None
    bot = Bot(world)
    token = bot.token(delegate(world, bot, scopes=("read", "write", "run")).grant)
    refused = bearer(token, "POST", f"/run-approvals/{run.approval.pk}/approve")
    assert refused.status_code == 403, refused.content
    assert RunApproval.objects.get(pk=run.approval.pk).state == ApprovalState.PENDING


def test_delegating_is_bounded_and_audited(world: World) -> None:
    bot = Bot(world)
    with pytest.raises(Refused, match="at most 90 days"):
        delegate(world, bot, days=91)
    with pytest.raises(Refused, match="today or a later day"):
        services.delegate(
            world.users["editor"],
            bot.client,
            services.Grant(["read"], True, []),
            expires_at=services.end_of(services.latest_delegation_day()) - timedelta(days=100),
        )
    with pytest.raises(Refused, match="only access the credential itself has"):
        delegate(world, bot, scopes=("read", "webhooks"))
    with pytest.raises(Refused, match="Only people"):
        services.delegate(
            bot.account,
            bot.client,
            services.Grant(["read"], True, []),
            expires_at=services.end_of(services.latest_delegation_day()),
        )
    first = delegate(world, bot, days=90)
    changed = delegate(world, bot, scopes=("read",), days=10)
    assert changed.grant == first.grant  # the same delegation, changed
    events = AuditEvent.objects.filter(action="oauth.delegation_granted").order_by("occurred_at")
    assert [(e.data["changed"], e.data["scopes"]) for e in events] == [
        (False, ["read", "write"]),
        (True, ["read"]),
    ]
    assert events[0].data["grant"] == str(first.grant)
    assert events[0].data["service_account"] == bot.account.email


def test_stopping_a_credential_acting_ends_its_delegations(world: World) -> None:
    bot = Bot(world)
    token = bot.token(delegate(world, bot).grant)
    delegate(world, bot, "admin")
    services.set_acts_for_people(bot.client, False, by=world.users["org_admin"])
    assert not Consent.objects.filter(client=bot.client).exists()
    assert bearer(token, "GET", "/me").status_code == 401
    event = AuditEvent.objects.get(action="oauth.credential_updated")
    assert event.data == {
        "client_id": bot.client.client_id,
        "acts_for_people": False,
        "delegations_ended": 2,
    }


def test_people_and_their_own_apps_cant_act_for_others(world: World) -> None:
    from test_oauth import registered

    app = registered(world)
    with pytest.raises(Exception, match="only_credentials_act_for_people"):
        Client.objects.filter(pk=app.pk).update(acts_for_people=True)


def test_its_own_credential_still_acts_as_the_service_account(world: World) -> None:
    bot = Bot(world)
    tokens = form_post(
        "/oauth/token/",
        {"grant_type": "client_credentials", "scope": "read"},
        Authorization=basic(bot.client.client_id, bot.secret),
    ).json()
    me = bearer(tokens["access_token"], "GET", "/me").json()
    assert me["email"] == bot.account.email
    assert me["delegation"] is None


# --- Pages -----------------------------------------------------------------------------------


def test_an_org_admin_lets_a_credential_act_for_people(world: World) -> None:
    from org_world import post

    admin = world.browser("org_admin")
    bot = Bot(world, acts_for_people=False)
    page = f"/service-accounts/{bot.account.pk}/"
    assert "Let it act for people" in admin.get(page).content.decode()
    done = post(admin, page, {"action": "allow_acting", "credential": str(bot.client.pk)})
    assert done.status_code == 302
    bot.client.refresh_from_db()
    assert bot.client.acts_for_people
    assert "acts for people (0 now)" in admin.get(page).content.decode()

    created = post(
        admin,
        page,
        {
            "action": "create_credential",
            "name": "Sync",
            "scopes": ["read"],
            "acts_for_people": "on",
        },
    )
    assert created.status_code == 200
    assert Client.objects.get(name="Sync").acts_for_people


def test_a_person_lets_an_agent_act_for_them(world: World) -> None:
    from org_world import post

    bot = Bot(world)
    browser = world.browser("editor")
    apps = browser.get("/account/apps/").content.decode()
    assert "Let an agent act for you" in apps
    listed = browser.get("/account/apps/delegate/").content.decode()
    assert "Triage server" in listed
    form = browser.get(f"/account/apps/delegate/{bot.client.pk}/")
    assert form.status_code == 200
    latest = form.context["form"].latest

    too_late = post(
        browser,
        f"/account/apps/delegate/{bot.client.pk}/",
        {"granted": ["read"], "access": "all", "until": str(latest + timedelta(days=1))},
    )
    assert too_late.status_code == 200
    assert "at most 90 days" in too_late.content.decode()

    done = post(
        browser,
        f"/account/apps/delegate/{bot.client.pk}/",
        {"granted": ["read", "write"], "access": "all", "until": str(latest)},
    )
    assert done.status_code == 302, done.content
    delegation = Consent.objects.get(user=world.users["editor"], client=bot.client)
    assert delegation.expires_at == services.end_of(latest)
    apps = browser.get("/account/apps/").content.decode()
    assert str(delegation.grant) in apps
    assert "Agents acting for you" in apps
    assert bot.token(delegation.grant)


def test_service_accounts_cant_delegate(world: World) -> None:
    bot = Bot(world)
    browser = world.browser("anonymous")
    browser.force_login(bot.account)
    assert browser.get("/account/apps/delegate/").status_code in (403, 404)


def test_the_server_says_it_exchanges_tokens() -> None:
    from browser import Browser

    meta = Browser().get("/.well-known/oauth-authorization-server").json()
    assert exchange.GRANT_TYPE in meta["grant_types_supported"]

"""A worker on someone's own computer, signed in as them (ADR 0058): ``iki-worker login``
goes through the device flow as the ``iki-worker`` client, for the ``worker`` scope alone,
and registers with the access token that gives it."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from api_client import problem
from django.test import Client
from django.utils import timezone
from oauth2_provider.models import get_access_token_model
from org_world import World, build_world, post
from test_api_entities import api_as
from test_oauth import bearer, form_post, poll, start_device
from test_workers import LOCAL, ROOMY, WorkerClient, queued

from iki_server.accounts import service_accounts
from iki_server.audit.models import AuditEvent
from iki_server.oauth import services
from iki_server.oauth.models import Consent
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import catalog
from iki_server.runs.models import ActionRun, Job, Worker

pytestmark = pytest.mark.django_db

AccessToken = get_access_token_model()


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def sign_in(world: World, principal: str = "editor", projects: list[Project] | None = None) -> str:
    """What ``iki-worker login`` gets: an access token for the ``worker`` scope."""
    started = start_device("worker", client_id="iki-worker")
    browser = world.browser(principal)
    entered = post(browser, "/oauth/device/", {"user_code": started["user_code"]})
    assert entered.status_code == 302, entered.content
    page = browser.get(entered["Location"])
    assert b"Iki worker" in page.content
    choice: dict[str, Any] = {"granted": ["worker"], "access": "all"}
    if projects is not None:
        choice = {**choice, "access": "chosen", "projects": [str(p.pk) for p in projects]}
    assert post(browser, entered["Location"], choice).status_code == 200
    tokens = poll(started["device_code"], client_id="iki-worker")
    assert tokens.status_code == 200, tokens.content
    assert tokens.json()["scope"] == "worker"
    token: str = tokens.json()["access_token"]
    return token


def register(token: str, name: str = "yogi-laptop") -> Any:
    return WorkerClient(token).post(
        "/register", {"name": name, "version": "0.1.0", "capacity": ROOMY, "plugins": LOCAL}
    )


def signed_in(world: World, principal: str = "editor", **kw: Any) -> WorkerClient:
    made = register(sign_in(world, principal, **kw))
    assert made.status_code == 201, made.content
    return WorkerClient(made.json()["token"])


def on_client(run: dict[str, Any]) -> dict[str, Any]:
    """A queued run, moved to the client tier (no test action runs there)."""
    owner = ActionRun.objects.get(pk=run["id"]).requested_by
    ActionRun.objects.filter(pk=run["id"]).update(tier="client")
    Job.objects.filter(run_id=run["id"]).update(tier="client", owner=owner)
    return run


def second_project(world: World) -> Project:
    other = Project.objects.create(team=world.team, name="Screens")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    return other


# --- Signing in -------------------------------------------------------------------------------


def test_a_worker_signs_in_on_someones_computer(world: World) -> None:
    token = sign_in(world)
    made = register(token)
    assert made.status_code == 201, made.content
    assert made.json()["tier"] == "client"
    worker = Worker.objects.get()
    assert (worker.tier, worker.owner, worker.registration) == (
        "client",
        world.users["editor"],
        None,
    )
    me = WorkerClient(made.json()["token"]).call("GET", "/me")
    assert me.status_code == 200 and me.json()["name"] == "yogi-laptop"
    event = AuditEvent.objects.get(action="worker.registered")
    assert event.actor == world.users["editor"] and event.data["tier"] == "client"


def test_registering_spends_the_token_it_signed_in_with(world: World) -> None:
    token = sign_in(world)
    assert register(token).status_code == 201
    problem(register(token), 401, "not_authenticated")
    assert not AccessToken.objects.filter(user=world.users["editor"]).exists()


def test_only_iki_worker_may_ask_for_the_worker_scope(world: World) -> None:
    refused = form_post(
        "/oauth/device-authorization/", {"client_id": "iki-cli", "scope": "read worker"}
    )
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_scope"
    worker_asks_more = form_post(
        "/oauth/device-authorization/", {"client_id": "iki-worker", "scope": "worker read"}
    )
    assert worker_asks_more.status_code == 400
    assert worker_asks_more.json()["error"] == "invalid_scope"
    admin = world.users["org_admin"]
    account = service_accounts.create("Pipelines", "", by=admin)
    with pytest.raises(Refused, match="can’t run workers"):
        services.create_credential(account, "laptop", ["worker"], by=admin)


def test_other_tokens_register_no_worker(world: World) -> None:
    started = start_device("read write")
    browser = world.browser("editor")
    entered = post(browser, "/oauth/device/", {"user_code": started["user_code"]})
    post(browser, entered["Location"], {"granted": ["read", "write"], "access": "all"})
    cli = poll(started["device_code"]).json()["access_token"]
    problem(register(cli), 401, "not_authenticated")
    problem(register("not-a-token"), 401, "not_authenticated")
    assert not Worker.objects.exists()


def test_a_worker_token_is_only_for_registering(world: World) -> None:
    token = sign_in(world)
    problem(bearer(token, "GET", "/me"), 403, "insufficient_scope")
    mcp = Client().post(
        "/mcp",
        "{}",
        content_type="application/json",
        secure=True,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert mcp.status_code == 403


def test_someone_deactivated_since_signing_in_registers_nothing(world: World) -> None:
    token = sign_in(world)
    world.users["editor"].is_active = False
    world.users["editor"].save()
    problem(register(token), 401, "not_authenticated")


def test_expired_tokens_register_nothing(world: World) -> None:
    token = sign_in(world)
    AccessToken.objects.update(expires=timezone.now())
    problem(register(token), 401, "not_authenticated")


# --- What it runs -------------------------------------------------------------------------------


def test_it_runs_only_its_persons_client_tier_runs(world: World) -> None:
    laptop = signed_in(world, "runner")
    queued(world)  # the editor's, on the backend tier
    assert laptop.lease().status_code == 204
    on_client(queued(world))  # the editor's, on the client tier
    assert laptop.lease().status_code == 204
    mine = signed_in(world, "editor")
    leased = mine.lease()
    assert leased.status_code == 200, leased.content


def test_it_runs_only_in_the_projects_it_was_allowed(world: World) -> None:
    other = second_project(world)
    laptop = signed_in(world, projects=[other])
    run = on_client(queued(world))
    assert laptop.lease().status_code == 204
    elsewhere = on_client(queued(world, other))
    leased = laptop.lease()
    assert leased.status_code == 200, leased.content
    assert Job.objects.get(pk=leased.json()["job"]["id"]).run_id != uuid.UUID(run["id"])
    assert str(Job.objects.get(pk=leased.json()["job"]["id"]).run_id) == elsewhere["id"]


def test_revoking_the_app_signs_out_its_workers(world: World) -> None:
    laptop = signed_in(world)
    assert laptop.call("GET", "/me").status_code == 200
    browser = world.browser("editor")
    consent = Consent.objects.get(user=world.users["editor"], client__client_id="iki-worker")
    revoked = post(browser, "/account/apps/", {"action": "revoke", "consent": str(consent.pk)})
    assert revoked.status_code == 302
    problem(laptop.call("GET", "/me"), 401, "not_authenticated")
    event = AuditEvent.objects.get(action="oauth.consent_revoked")
    assert event.data["workers_revoked"] == 1


def test_a_worker_with_no_consent_leases_nothing(world: World) -> None:
    laptop = signed_in(world)
    Consent.objects.all().delete()
    on_client(queued(world))
    assert laptop.lease().status_code == 204


# --- Your workers ---------------------------------------------------------------------------------


def test_people_see_and_revoke_their_workers(world: World) -> None:
    laptop = signed_in(world)
    signed_in(world, "runner")
    browser = world.browser("editor")
    page = browser.get("/account/workers/")
    assert page.status_code == 200
    html = page.content.decode()
    assert html.count("yogi-laptop") == 1 and "Online" in html
    assert "iki-worker login https://testserver" in html
    worker = Worker.objects.get(owner=world.users["editor"])
    other = Worker.objects.get(owner=world.users["runner"])
    refused = post(browser, "/account/workers/", {"action": "revoke", "worker": str(other.pk)})
    assert refused.status_code == 404
    done = post(browser, "/account/workers/", {"action": "revoke", "worker": str(worker.pk)})
    assert done.status_code == 302
    problem(laptop.call("GET", "/me"), 401, "not_authenticated")
    assert (
        "You haven’t signed in a worker yet." in browser.get("/account/workers/").content.decode()
    )
    other.refresh_from_db()
    assert other.revoked_at is None


def test_a_client_tier_run_says_when_it_waits_for_a_worker(world: World) -> None:
    run = on_client(queued(world))
    assert api_as(world, "editor").get(f"/runs/{run['hrid']}").json()["needs_worker"] == "yours"
    assert api_as(world, "viewer").get(f"/runs/{run['hrid']}").json()["needs_worker"] == "theirs"
    signed_in(world)
    assert api_as(world, "editor").get(f"/runs/{run['hrid']}").json()["needs_worker"] is None
    backend = queued(world)
    assert api_as(world, "editor").get(f"/runs/{backend['hrid']}").json()["needs_worker"] is None

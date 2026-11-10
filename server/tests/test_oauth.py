"""The OAuth 2.1 server (ADR 0028), driven the way real clients and people use it.

Clients register themselves (RFC 7591) and wait for an Org Admin's approval;
people choose which scopes and projects a client gets; the CLI uses the
device flow (RFC 8628); service accounts use client credentials. Tokens can
only do what both their scopes and their user's roles allow, and revoking
anything signs the client out at once.
"""

import base64
import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from api_client import problem
from browser import Browser, Response
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_device_grant_model
from org_world import World, build_world, post

from iki_server.accounts import service_accounts
from iki_server.audit.models import AuditEvent
from iki_server.oauth import maintenance, services
from iki_server.oauth.models import Approval, Client, Consent
from iki_server.oauth.views import register_limiter
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused

pytestmark = pytest.mark.django_db

AccessToken = get_access_token_model()
DeviceGrant = get_device_grant_model()

REDIRECT = "https://notebook.example.com/callback"
DEVICE_CODE = "urn:ietf:params:oauth:grant-type:device_code"


@pytest.fixture
def world() -> World:
    return build_world()


# --- Helpers -------------------------------------------------------------------


def form_post(path: str, data: dict[str, str], **headers: str) -> Response:
    return Browser().post(
        path,
        urlencode(data),
        content_type="application/x-www-form-urlencoded",
        headers=headers or None,
    )


def register(body: dict[str, Any] | None = None) -> Response:
    payload = {
        "client_name": "Notebook",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        **(body or {}),
    }
    return Browser().post("/oauth/register/", json.dumps(payload), content_type="application/json")


def registered(world: World, *, approve: bool = True, **body: Any) -> Client:
    response = register(body)
    assert response.status_code == 201, response.content
    client: Client = Client.objects.get(client_id=response.json()["client_id"])
    if approve:
        services.set_approval(client, Approval.APPROVED, by=world.users["org_admin"])
        client.refresh_from_db()
    return client


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_query(client: Client, challenge: str, scope: str = "read write") -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": REDIRECT,
        "scope": scope,
        "state": "s-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }


def consent(
    browser: Browser,
    client: Client,
    *,
    scope: str = "read write",
    granted: list[str] | None = None,
    projects: list[Project] | None = None,
) -> tuple[Response, str]:
    """Goes through the consent page and allows; returns the redirect and the PKCE verifier."""
    verifier, challenge = pkce()
    query = authorize_query(client, challenge, scope)
    page = browser.get("/oauth/authorize/", query)
    assert page.status_code == 200, page.content
    data: dict[str, Any] = {
        **query,
        "allow": "Allow",
        "granted": granted or scope.split(),
        "access": "chosen" if projects is not None else "all",
        "projects": [str(p.pk) for p in projects or []],
    }
    return post(browser, "/oauth/authorize/", data), verifier


def code_from(response: Response) -> str:
    assert response.status_code == 302, response.content
    location = urlsplit(response["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
    params = parse_qs(location.query)
    assert params["state"] == ["s-123"]
    assert params["iss"] == ["https://testserver"]  # RFC 9207 mix-up defence
    return params["code"][0]


def exchange(client: Client, code: str, verifier: str) -> Response:
    return form_post(
        "/oauth/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client.client_id,
            "code_verifier": verifier,
        },
    )


def signed_in_token(
    world: World, principal: str = "editor", **choice: Any
) -> tuple[Client, dict[str, Any]]:
    client = registered(world)
    response, verifier = consent(world.browser(principal), client, **choice)
    tokens = exchange(client, code_from(response), verifier)
    assert tokens.status_code == 200, tokens.content
    return client, tokens.json()


def bearer(token: str, method: str, path: str, body: Any = None) -> Response:
    browser = Browser()
    browser.raise_request_exception = False
    return browser.generic(
        method,
        "/api/v1" + path,
        "" if body is None else json.dumps(body),
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}"},
    )


def basic(client_id: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()


# --- Metadata ------------------------------------------------------------------


def test_clients_discover_the_server_from_metadata() -> None:
    server = Browser().get("/.well-known/oauth-authorization-server")
    assert server.status_code == 200
    meta = server.json()
    assert meta["issuer"] == "https://testserver"
    assert meta["authorization_endpoint"] == "https://testserver/oauth/authorize/"
    assert meta["token_endpoint"] == "https://testserver/oauth/token/"
    assert meta["registration_endpoint"] == "https://testserver/oauth/register/"
    assert meta["device_authorization_endpoint"] == "https://testserver/oauth/device-authorization/"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["response_types_supported"] == ["code"]
    assert "implicit" not in meta["grant_types_supported"]
    assert "password" not in meta["grant_types_supported"]
    assert set(meta["scopes_supported"]) == {"read", "write", "run", "apps", "admin"}

    resource = Browser().get("/.well-known/oauth-protected-resource").json()
    assert resource["authorization_servers"] == ["https://testserver"]
    assert resource["bearer_methods_supported"] == ["header"]


def test_an_api_call_without_a_token_says_where_to_get_one() -> None:
    response = bearer("not-a-token", "GET", "/me")
    problem(response, 401, "invalid_token")
    challenge = response["WWW-Authenticate"]
    assert 'error="invalid_token"' in challenge
    assert 'resource_metadata="https://testserver/.well-known/oauth-protected-resource"' in (
        challenge
    )


# --- Registration and approval -------------------------------------------------


def test_a_registered_client_waits_for_an_org_admin(world: World) -> None:
    response = register()
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["token_endpoint_auth_method"] == "none"  # public: PKCE, no secret
    client = Client.objects.get(client_id=body["client_id"])
    assert client.approval == Approval.PENDING
    event = AuditEvent.objects.get(action="oauth.client_registered")
    assert event.target_id == str(client.pk)
    assert event.data["approval"] == "pending"
    assert event.data["redirect_uris"] == [REDIRECT]

    # Someone who tries it sees why it doesn't work yet, and can ask for it.
    browser = world.browser("viewer")
    _, challenge = pkce()
    page = browser.get("/oauth/authorize/", authorize_query(client, challenge))
    assert page.status_code == 403
    assert b"waiting for approval" in page.content.lower() or b"approv" in page.content
    asked = post(browser, f"/oauth/apps/{client.pk}/request-approval/")
    assert asked.status_code == 403
    client.refresh_from_db()
    assert client.requested_by == world.users["viewer"]
    assert AuditEvent.objects.filter(action="oauth.client_approval_requested").count() == 1

    # The Org Admin sees the request and approves it.
    admin = world.browser("org_admin")
    listing = admin.get("/settings/apps/")
    assert listing.status_code == 200 and b"Notebook" in listing.content
    assert post(admin, f"/settings/apps/{client.pk}/", {"action": "approve"}).status_code == 302
    client.refresh_from_db()
    assert client.approval == Approval.APPROVED
    assert browser.get("/oauth/authorize/", authorize_query(client, challenge)).status_code == 200


def test_with_approval_turned_off_clients_work_at_once(world: World) -> None:
    admin = world.browser("org_admin")
    post(admin, "/settings/apps/", {"action": "policy"})  # unchecked: no approval needed
    assert AuditEvent.objects.get(action="oauth.approval_policy_changed").data == {
        "required": False
    }
    client = registered(world, approve=False)
    assert client.approval == Approval.APPROVED


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"grant_types": ["client_credentials"]}, "invalid_client_metadata"),
        ({"grant_types": ["authorization_code", "password"]}, "invalid_client_metadata"),
        ({"response_types": ["token"]}, "invalid_client_metadata"),
        ({"client_name": "x" * 101}, "invalid_client_metadata"),
        ({"redirect_uris": ["http://notebook.example.com/callback"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://10.0.0.5:8080/callback"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["com.example.app:/callback"]}, None),
    ],
)
def test_clients_register_only_for_what_people_can_safely_use(
    body: dict[str, Any], error: str | None
) -> None:
    response = register(body)
    assert response.status_code == 400, response.content
    if error:
        assert response.json()["error"] == error
    assert not Client.objects.filter(registration_source="dcr").exists()


@pytest.mark.parametrize(
    "uri", ["http://127.0.0.1/callback", "http://[::1]:8123/cb", "http://localhost:5173/cb"]
)
def test_apps_on_your_own_computer_may_use_loopback_http(uri: str) -> None:
    assert register({"redirect_uris": [uri]}).status_code == 201


def test_registration_is_rate_limited() -> None:
    for _ in range(register_limiter.limit):
        assert register().status_code == 201
    response = register()
    assert response.status_code == 429
    assert response.json()["error"] == "too_many_requests"
    assert int(response["Retry-After"]) > 0


def test_changing_where_it_sends_people_needs_approval_again(world: World) -> None:
    response = register()
    body = response.json()
    client = Client.objects.get(client_id=body["client_id"])
    services.set_approval(client, Approval.APPROVED, by=world.users["org_admin"])
    updated = Browser().put(
        f"/oauth/register/{body['client_id']}/",
        json.dumps({**body, "redirect_uris": ["https://elsewhere.example.net/cb"]}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {body['registration_access_token']}"},
    )
    assert updated.status_code == 200, updated.content
    client.refresh_from_db()
    assert client.approval == Approval.PENDING


def test_blocking_a_client_signs_it_out_everywhere(world: World) -> None:
    client, tokens = signed_in_token(world)
    services.set_approval(client, Approval.BLOCKED, by=world.users["org_admin"])
    assert not AccessToken.objects.filter(application=client).exists()
    problem(bearer(tokens["access_token"], "GET", "/me"), 401, "invalid_token")
    event = AuditEvent.objects.get(action="oauth.client_blocked")
    # The person's token, and the one the client manages its registration with.
    assert event.data == {"previous": "approved", "tokens_revoked": 2}


def test_abandoned_registrations_are_cleaned_up(world: World) -> None:
    old = registered(world, approve=False)
    asked = registered(world, approve=False)
    approved = registered(world)
    Client.objects.filter(pk__in=[old.pk, asked.pk, approved.pk]).update(
        created=timezone.now() - timedelta(days=8)
    )
    services.request_approval(asked, by=world.users["viewer"])
    fresh = registered(world, approve=False)
    assert maintenance.prune() == 1
    remaining = set(Client.objects.values_list("pk", flat=True))
    assert old.pk not in remaining
    assert {asked.pk, approved.pk, fresh.pk} <= remaining
    assert Client.objects.filter(client_id="iki-cli").exists()


# --- Authorization code with PKCE ----------------------------------------------


def test_people_choose_what_a_client_may_do_and_where(world: World) -> None:
    client = registered(world)
    browser = world.browser("editor")
    verifier, challenge = pkce()
    page = browser.get("/oauth/authorize/", authorize_query(client, challenge))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Notebook" in html and "notebook.example.com" in html
    assert "Kinase Atlas" in html
    assert "Old Screens" not in html  # archived projects aren't offered
    # The browser follows the redirect after the form posts, so CSP must allow it.
    assert "https://notebook.example.com" in page["Content-Security-Policy"]

    response, verifier = consent(
        browser, client, scope="read write", granted=["read"], projects=[world.project]
    )
    tokens = exchange(client, code_from(response), verifier)
    assert tokens.status_code == 200, tokens.content
    body = tokens.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "read"
    assert body["expires_in"] == 3600
    assert body["refresh_token"]

    saved = Consent.objects.get(user=world.users["editor"], client=client)
    assert saved.scope_list == ["read"] and not saved.all_projects
    assert list(saved.projects.all()) == [world.project]
    event = AuditEvent.objects.get(action="oauth.consent_granted")
    assert event.data == {"scopes": ["read"], "projects": [str(world.project.pk)], "changed": False}


def test_the_code_works_only_with_its_verifier_and_only_once(world: World) -> None:
    client = registered(world)
    response, verifier = consent(world.browser("editor"), client)
    code = code_from(response)
    assert exchange(client, code, "wrong-" + verifier).json()["error"] == "invalid_grant"
    response, verifier = consent(world.browser("editor"), client)
    code = code_from(response)
    assert exchange(client, code, verifier).status_code == 200
    assert exchange(client, code, verifier).json()["error"] == "invalid_grant"


def test_pkce_is_required(world: World) -> None:
    client = registered(world)
    query = authorize_query(client, "x")
    del query["code_challenge"], query["code_challenge_method"]
    response = world.browser("editor").get("/oauth/authorize/", query)
    location = response.get("Location", "")
    assert response.status_code in (302, 400), response.status_code
    assert "code" not in parse_qs(urlsplit(location).query)


def test_denying_sends_the_person_back_without_a_code(world: World) -> None:
    client = registered(world)
    browser = world.browser("editor")
    _, challenge = pkce()
    query = authorize_query(client, challenge)
    browser.get("/oauth/authorize/", query)
    response = post(browser, "/oauth/authorize/", query)  # no "allow"
    params = parse_qs(urlsplit(response["Location"]).query)
    assert params["error"] == ["access_denied"]
    assert not Consent.objects.exists()


def test_an_unregistered_redirect_is_never_followed(world: World) -> None:
    client = registered(world)
    _, challenge = pkce()
    query = {**authorize_query(client, challenge), "redirect_uri": "https://evil.example/cb"}
    response = world.browser("editor").get("/oauth/authorize/", query)
    assert response.status_code != 302


# --- Using tokens --------------------------------------------------------------


def test_a_token_reads_only_the_projects_it_was_given(world: World) -> None:
    other = Project.objects.create(team=world.team, name="Second Screen")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    _, tokens = signed_in_token(world, scope="read", projects=[world.project])
    token = tokens["access_token"]
    me = bearer(token, "GET", "/me")
    assert me.status_code == 200 and me.json()["email"] == "editor@example.com"
    names = [p["name"] for p in bearer(token, "GET", "/projects").json()["items"]]
    assert names == ["Kinase Atlas"]
    assert bearer(token, "GET", f"/projects/{other.pk}").status_code == 404


def test_a_read_token_cannot_change_anything(world: World) -> None:
    _, tokens = signed_in_token(world, scope="read")
    response = bearer(
        tokens["access_token"], "PATCH", f"/projects/{world.project.pk}", {"description": "x"}
    )
    body = problem(response, 403, "insufficient_scope")
    assert "write" in body["detail"]
    assert 'error="insufficient_scope"' in response["WWW-Authenticate"]


def test_scopes_never_add_to_what_roles_allow(world: World) -> None:
    _, tokens = signed_in_token(world, "viewer", scope="read write")
    response = bearer(
        tokens["access_token"], "PATCH", f"/projects/{world.project.pk}", {"description": "x"}
    )
    assert response.status_code == 403
    assert response.json()["code"] != "insufficient_scope"


def test_changes_made_with_a_token_name_the_client_in_the_audit_log(world: World) -> None:
    client, tokens = signed_in_token(world, "admin", scope="read write")
    response = bearer(
        tokens["access_token"], "PATCH", f"/projects/{world.project.pk}", {"description": "Hi"}
    )
    assert response.status_code == 200, response.content
    event = AuditEvent.objects.get(action="project.updated")
    assert event.actor == world.users["admin"]
    assert event.via == "api"
    assert event.oauth_client.startswith("Notebook (")
    assert client.client_id[:10] in event.oauth_client


def test_narrowing_consent_applies_to_tokens_already_issued(world: World) -> None:
    client, tokens = signed_in_token(world, "admin", scope="read write")
    token = tokens["access_token"]
    services.grant(
        world.users["admin"], client, services.Grant(["read"], all_projects=True, projects=[])
    )
    response = bearer(token, "PATCH", f"/projects/{world.project.pk}", {"description": "x"})
    problem(response, 403, "insufficient_scope")
    services.grant(
        world.users["admin"],
        client,
        services.Grant(["read"], all_projects=False, projects=[world.archived]),
    )
    assert bearer(token, "GET", f"/projects/{world.project.pk}").status_code == 404


def test_revoking_an_app_signs_it_out(world: World) -> None:
    client, tokens = signed_in_token(world)
    browser = world.browser("editor")
    page = browser.get("/account/apps/")
    assert page.status_code == 200 and b"Notebook" in page.content
    saved = Consent.objects.get(client=client)
    revoked = post(browser, "/account/apps/", {"action": "revoke", "consent": str(saved.pk)})
    assert revoked.status_code == 302
    assert not Consent.objects.exists()
    problem(bearer(tokens["access_token"], "GET", "/me"), 401, "invalid_token")
    refreshed = form_post(
        "/oauth/token/",
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client.client_id,
        },
    )
    assert refreshed.json()["error"] == "invalid_grant"
    assert AuditEvent.objects.get(action="oauth.consent_revoked").data["tokens_revoked"] == 1


def test_nobody_can_revoke_someone_elses_app(world: World) -> None:
    client, _ = signed_in_token(world)
    saved = Consent.objects.get(client=client)
    other = world.browser("viewer")
    response = post(other, "/account/apps/", {"action": "revoke", "consent": str(saved.pk)})
    assert response.status_code == 404
    assert Consent.objects.filter(pk=saved.pk).exists()


def test_deactivating_someone_ends_their_tokens(world: World) -> None:
    _, tokens = signed_in_token(world)
    editor = world.users["editor"]
    editor.is_active = False
    editor.save()
    assert not AccessToken.objects.filter(user=editor).exists()
    problem(bearer(tokens["access_token"], "GET", "/me"), 401, "invalid_token")
    event = AuditEvent.objects.get(action="user.sessions_revoked")
    assert event.data["tokens_revoked"] == 1


def test_signing_someone_out_everywhere_ends_their_tokens(world: World) -> None:
    from iki_server.org import services as org_services

    _, tokens = signed_in_token(world)
    org_services.end_sessions(world.users["editor"], by=world.users["org_admin"])
    problem(bearer(tokens["access_token"], "GET", "/me"), 401, "invalid_token")


def test_refresh_tokens_rotate_and_reuse_ends_the_family(world: World) -> None:
    client, tokens = signed_in_token(world)

    def refresh(token: str) -> Response:
        return form_post(
            "/oauth/token/",
            {"grant_type": "refresh_token", "refresh_token": token, "client_id": client.client_id},
        )

    first = refresh(tokens["refresh_token"])
    assert first.status_code == 200, first.content
    rotated = first.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert bearer(rotated["access_token"], "GET", "/me").status_code == 200

    # Someone replaying the old refresh token (a thief, say) ends every token in the chain.
    assert refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"
    problem(bearer(rotated["access_token"], "GET", "/me"), 401, "invalid_token")
    assert refresh(rotated["refresh_token"]).status_code == 400


def test_expired_tokens_are_refused(world: World) -> None:
    _, tokens = signed_in_token(world)
    AccessToken.objects.update(expires=timezone.now() - timedelta(seconds=1))
    problem(bearer(tokens["access_token"], "GET", "/me"), 401, "invalid_token")


def test_tokens_are_stored_hashed(world: World) -> None:
    _, tokens = signed_in_token(world)
    stored = AccessToken.objects.get(user=world.users["editor"])
    assert stored.token != tokens["access_token"]
    assert not AccessToken.objects.filter(token=tokens["access_token"]).exists()


# --- Device flow (the CLI) -----------------------------------------------------


def start_device(scope: str = "read write") -> dict[str, Any]:
    response = form_post("/oauth/device-authorization/", {"client_id": "iki-cli", "scope": scope})
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def poll(device_code: str, client_id: str = "iki-cli") -> Response:
    """Polls as a well-behaved client would, after waiting the interval."""
    DeviceGrant.objects.filter(device_code=device_code).update(
        last_checked=timezone.now() - timedelta(seconds=6)
    )
    return form_post(
        "/oauth/token/",
        {"grant_type": DEVICE_CODE, "device_code": device_code, "client_id": client_id},
    )


def approve_device(world: World, started: dict[str, Any], principal: str = "editor") -> None:
    browser = world.browser(principal)
    entered = post(browser, "/oauth/device/", {"user_code": started["user_code"]})
    assert entered.status_code == 302, entered.content
    done = post(browser, entered["Location"], {"granted": ["read"], "access": "all"})
    assert done.status_code == 200


def test_the_cli_signs_in_with_a_code_typed_in_the_browser(world: World) -> None:
    started = start_device()
    assert started["verification_uri"].endswith("/oauth/device/")
    assert started["interval"] == 5
    assert poll(started["device_code"]).json()["error"] == "authorization_pending"

    browser = world.browser("editor")
    assert browser.get("/oauth/device/").status_code == 200
    typed = started["user_code"].lower()  # people don't type codes exactly
    entered = post(browser, "/oauth/device/", {"user_code": typed})
    assert entered.status_code == 302
    page = browser.get(entered["Location"])
    assert page.status_code == 200
    assert b"Iki CLI" in page.content and b"Kinase Atlas" in page.content
    done = post(
        browser,
        entered["Location"],
        {"granted": ["read"], "access": "chosen", "projects": [str(world.project.pk)]},
    )
    assert done.status_code == 200

    tokens = poll(started["device_code"])
    assert tokens.status_code == 200, tokens.content
    body = tokens.json()
    assert body["scope"] == "read"
    me = bearer(body["access_token"], "GET", "/me")
    assert me.json()["email"] == "editor@example.com"
    assert AuditEvent.objects.get(action="oauth.device_approved").data == {"scopes": ["read"]}


def test_a_device_code_gets_one_token_response(world: World) -> None:
    started = start_device()
    approve_device(world, started)
    assert poll(started["device_code"]).status_code == 200
    again = poll(started["device_code"])
    assert again.status_code == 400 and again.json()["error"] == "expired_token"


def test_a_device_code_works_only_for_the_client_it_was_issued_to(world: World) -> None:
    started = start_device()
    approve_device(world, started)
    other = registered(world)
    stolen = poll(started["device_code"], client_id=other.client_id)
    assert stolen.status_code in (400, 401)
    assert "access_token" not in stolen.json()
    assert poll(started["device_code"]).status_code == 200


def test_polling_too_fast_is_told_to_slow_down() -> None:
    started = start_device()
    fast = form_post(
        "/oauth/token/",
        {"grant_type": DEVICE_CODE, "device_code": started["device_code"], "client_id": "iki-cli"},
    )
    assert fast.json()["error"] == "slow_down"


def test_expired_and_unknown_device_codes_are_refused(world: World) -> None:
    started = start_device()
    approve_device(world, started)
    DeviceGrant.objects.update(expires=timezone.now() - timedelta(seconds=1))
    assert poll(started["device_code"]).json()["error"] == "expired_token"
    assert poll("not-a-device-code").json()["error"] == "invalid_grant"


def test_a_device_code_approved_by_someone_since_deactivated_gets_nothing(world: World) -> None:
    started = start_device()
    approve_device(world, started)
    editor = world.users["editor"]
    editor.is_active = False
    editor.save()
    assert "access_token" not in poll(started["device_code"]).json()


def test_a_device_code_is_claimed_by_whoever_enters_it_first(world: World) -> None:
    started = start_device()
    first = world.browser("editor")
    entered = post(first, "/oauth/device/", {"user_code": started["user_code"]})
    assert entered.status_code == 302
    second = world.browser("viewer")
    retry = post(second, "/oauth/device/", {"user_code": started["user_code"]})
    assert retry.status_code == 200 and "isn’t valid".encode() in retry.content
    assert second.get(entered["Location"]).status_code == 404


def test_denying_a_device_ends_its_sign_in(world: World) -> None:
    started = start_device()
    browser = world.browser("editor")
    entered = post(browser, "/oauth/device/", {"user_code": started["user_code"]})
    post(browser, entered["Location"], {"action": "deny"})
    assert poll(started["device_code"]).json()["error"] == "access_denied"


def test_wrong_device_codes_are_rate_limited(world: World) -> None:
    browser = world.browser("editor")
    for _ in range(10):
        post(browser, "/oauth/device/", {"user_code": "WRONGCODE"})
    response = post(browser, "/oauth/device/", {"user_code": "WRONGCODE"})
    assert b"Too many tries" in response.content


# --- Service accounts (client credentials) -------------------------------------


def test_service_accounts_use_client_credentials(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("LIMS sync", "", by=admin)
    ProjectMembership.objects.create(project=world.project, user=account, role=ProjectRole.VIEWER)
    credential = services.create_credential(account, "LIMS server", ["read"], by=admin)
    assert credential.client.client_secret != credential.secret  # only the hash is stored

    response = form_post(
        "/oauth/token/",
        {"grant_type": "client_credentials", "scope": "read"},
        Authorization=basic(credential.client.client_id, credential.secret),
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["scope"] == "read" and "refresh_token" not in body
    me = bearer(body["access_token"], "GET", "/me")
    assert me.json()["id"] == str(account.pk)
    listed = bearer(body["access_token"], "GET", "/projects").json()["items"]
    assert [p["name"] for p in listed] == ["Kinase Atlas"]

    # It can't ask for more than it was created with.
    wider = form_post(
        "/oauth/token/",
        {"grant_type": "client_credentials", "scope": "read write"},
        Authorization=basic(credential.client.client_id, credential.secret),
    )
    assert wider.json()["error"] == "invalid_scope"

    # Deactivating the account ends its tokens and stops new ones.
    service_accounts_deactivate(account)
    problem(bearer(body["access_token"], "GET", "/me"), 401, "invalid_token")
    again = form_post(
        "/oauth/token/",
        {"grant_type": "client_credentials"},
        Authorization=basic(credential.client.client_id, credential.secret),
    )
    assert again.status_code in (400, 401)


def service_accounts_deactivate(account: Any) -> None:
    account.is_active = False
    account.save()


def test_revoking_a_credential_ends_its_tokens(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("Pipeline", "", by=admin)
    credential = services.create_credential(account, "", ["read"], by=admin)
    token = form_post(
        "/oauth/token/",
        {"grant_type": "client_credentials"},
        Authorization=basic(credential.client.client_id, credential.secret),
    ).json()["access_token"]
    services.revoke_credential(credential.client, by=admin)
    problem(bearer(token, "GET", "/me"), 401, "invalid_token")
    event = AuditEvent.objects.get(action="oauth.credential_revoked")
    assert event.data["tokens_revoked"] == 1


def test_service_accounts_never_get_admin_scope(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("Pipeline", "", by=admin)
    with pytest.raises(Refused):
        services.create_credential(account, "", ["read", "admin"], by=admin)


def test_only_service_account_credentials_use_client_credentials(world: World) -> None:
    client = registered(world)
    response = form_post(
        "/oauth/token/", {"grant_type": "client_credentials", "client_id": client.client_id}
    )
    assert response.status_code in (400, 401)
    assert not AccessToken.objects.exclude(scope="oauth2_provider:registration").exists()


def test_the_registration_token_is_not_an_api_token() -> None:
    token = register().json()["registration_access_token"]
    problem(bearer(token, "GET", "/me"), 401, "invalid_token")


def test_credentials_are_created_on_the_service_account_page(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("Pipeline", "", by=admin)
    browser = world.browser("org_admin")
    response = post(
        browser,
        f"/service-accounts/{account.pk}/",
        {"action": "create_credential", "name": "CI", "scopes": ["read", "run"]},
    )
    assert response.status_code == 200, response.status_code
    client = Client.objects.get(service_account=account)
    assert client.allowed_scopes == "read run"
    # The secret is shown once, and never again.
    html = response.content.decode()
    assert client.client_id in html
    assert "secret" in html.lower()
    again = browser.get(f"/service-accounts/{account.pk}/").content.decode()
    assert client.client_id in again

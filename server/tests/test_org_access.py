"""The endpoint matrix: every org, audit and sign-in settings page against every principal.

404 means "you can't know this exists", 403 means "you can see it but not
do this", 302 to /login/ means "sign in first". A test fails if a new org URL
isn't listed here, so no endpoint ships without its authorization row.
"""

from collections.abc import Callable

import pytest
from django.urls import get_resolver
from org_world import PRINCIPALS, World, build_world, post

pytestmark = pytest.mark.django_db

LOGIN = "login"  # expected: redirect to the sign-in page
OK = 200
DONE = 302  # a successful POST redirects
FORBIDDEN = 403
HIDDEN = 404

ANON = {"anonymous": LOGIN, "inactive": LOGIN}


def row(default: int, **overrides: int | str) -> dict[str, int | str]:
    return {**dict.fromkeys(PRINCIPALS, default), **ANON, **overrides}


PROJECT_VISIBLE = dict.fromkeys(
    ("org_admin", "viewer", "editor", "runner", "admin", "via_team", "mixed"), OK
)

#: (URL name, method) -> how to build the request -> expected status per principal
Case = tuple[str, str, Callable[[World], tuple[str, dict[str, str]]], dict[str, int | str]]


def project_url(suffix: str = "") -> Callable[[World], tuple[str, dict[str, str]]]:
    return lambda w: (f"/projects/{w.project.slug}/{suffix}", {})


def team_url(suffix: str = "") -> Callable[[World], tuple[str, dict[str, str]]]:
    return lambda w: (f"/teams/{w.team.slug}/{suffix}", {})


def team_member_remove_url(w: World) -> tuple[str, dict[str, str]]:
    return f"/teams/{w.team.slug}/members/{w.users['team_member'].pk}/remove/", {}


def group_url(**data: str) -> Callable[[World], tuple[str, dict[str, str]]]:
    def build(w: World) -> tuple[str, dict[str, str]]:
        from iki_server.scim.models import ScimGroup

        group, _ = ScimGroup.objects.get_or_create(display_name="Chemistry")
        return f"/settings/sign-in/groups/{group.pk}/?team={w.other_team.pk}", {
            "team": str(w.other_team.pk),
            **data,
        }

    return build


def person_url(w: World) -> tuple[str, dict[str, str]]:
    return f"/people/{w.users['viewer'].pk}/", {}


def service_account_url(**data: str) -> Callable[[World], tuple[str, dict[str, str]]]:
    def build(w: World) -> tuple[str, dict[str, str]]:
        from iki_server.accounts import service_accounts

        account = service_accounts.create("Robot", "", by=w.users["org_admin"])
        return f"/service-accounts/{account.pk}/", data

    return build


def app_url(suffix: str = "", **data: str) -> Callable[[World], tuple[str, dict[str, str]]]:
    """A self-registered OAuth app, waiting for approval."""

    def build(w: World) -> tuple[str, dict[str, str]]:
        from iki_server.oauth.models import Client

        client = Client.objects.create(
            name="Notebook",
            client_type=Client.CLIENT_PUBLIC,
            authorization_grant_type=Client.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://notebook.example.com/callback",
            registration_source=Client.RegistrationSource.DCR,
        )
        return suffix.format(pk=client.pk), data

    return build


def device_grant_url(**data: str) -> Callable[[World], tuple[str, dict[str, str]]]:
    """A device code the viewer has entered and not yet approved."""

    def build(w: World) -> tuple[str, dict[str, str]]:
        from datetime import timedelta

        from django.utils import timezone
        from oauth2_provider.models import get_device_grant_model

        grant = get_device_grant_model().objects.create(
            client_id="iki-cli",
            device_code="device-code-for-the-matrix",
            user_code="MATRIX01",
            scope="read",
            expires=timezone.now() + timedelta(minutes=10),
            user=w.users["viewer"],
        )
        return f"/oauth/device/{grant.pk}/", data

    return build


CASES: list[Case] = [
    # The web app's pages: one page for everyone signed in, with no data in it.
    # The app reads everything through the API, whose matrix is in
    # test_api_access.py: that's where outsiders get 404 and members 403.
    ("project_list", "GET", lambda w: ("/projects/", {}), row(OK)),
    ("project_new", "GET", lambda w: ("/projects/new/", {}), row(OK)),
    ("project_detail", "GET", project_url(), row(OK)),
    ("project_members", "GET", project_url("members/"), row(OK)),
    ("project_entities", "GET", project_url("entities/"), row(OK)),
    ("project_entity_new", "GET", project_url("entities/new/"), row(OK)),
    ("project_runs", "GET", project_url("runs/"), row(OK)),
    ("project_run_new", "GET", project_url("runs/new/"), row(OK)),
    ("project_settings", "GET", project_url("settings/"), row(OK)),
    ("project_webhooks", "GET", project_url("webhooks/"), row(OK)),
    (
        "project_webhook",
        "GET",
        project_url("webhooks/01a1c452-a14b-7195-acd0-4b6ba266a652/"),
        row(OK),
    ),
    ("team_list", "GET", lambda w: ("/teams/", {}), row(OK)),
    (
        "team_list",
        "POST",
        lambda w: ("/teams/", {"name": "Genomics"}),
        row(FORBIDDEN, org_admin=DONE),
    ),
    ("team_detail", "GET", team_url(), row(HIDDEN, org_admin=OK, team_member=OK)),
    (
        "team_detail",
        "POST",
        lambda w: (f"/teams/{w.team.slug}/", {"person": "outsider@example.com"}),
        row(HIDDEN, org_admin=DONE, team_member=FORBIDDEN),
    ),
    (
        "team_member_remove",
        "POST",
        team_member_remove_url,
        row(HIDDEN, org_admin=DONE, team_member=FORBIDDEN),
    ),
    (
        "team_settings",
        "GET",
        team_url("settings/"),
        row(HIDDEN, org_admin=OK, team_member=FORBIDDEN),
    ),
    (
        "team_settings",
        "POST",
        lambda w: (f"/teams/{w.team.slug}/settings/", {"action": "archive"}),
        row(HIDDEN, org_admin=DONE, team_member=FORBIDDEN),
    ),
    ("people", "GET", lambda w: ("/people/", {}), row(FORBIDDEN, org_admin=OK)),
    ("audit_log", "GET", lambda w: ("/audit/", {}), row(FORBIDDEN, auditor=OK)),
    ("sign_in_settings", "GET", lambda w: ("/settings/sign-in/", {}), row(FORBIDDEN, org_admin=OK)),
    (
        "sign_in_settings",
        "POST",
        lambda w: ("/settings/sign-in/", {"action": "create_token"}),
        row(FORBIDDEN, org_admin=OK),
    ),
    ("group_link", "GET", group_url(), row(FORBIDDEN, org_admin=OK)),
    ("group_link", "POST", group_url(action="link"), row(FORBIDDEN, org_admin=DONE)),
    ("person", "GET", person_url, row(FORBIDDEN, org_admin=OK)),
    (
        "person",
        "POST",
        lambda w: (f"/people/{w.users['viewer'].pk}/", {"roles": "auditor"}),
        row(FORBIDDEN, org_admin=DONE),
    ),
    (
        "service_accounts",
        "GET",
        lambda w: ("/service-accounts/", {}),
        row(FORBIDDEN, org_admin=OK),
    ),
    (
        "service_accounts",
        "POST",
        lambda w: ("/service-accounts/", {"name": "LIMS sync"}),
        row(FORBIDDEN, org_admin=DONE),
    ),
    ("service_account", "GET", service_account_url(), row(FORBIDDEN, org_admin=OK)),
    # OAuth: apps people sign in to, and the device flow.
    ("org_apps", "GET", lambda w: ("/settings/apps/", {}), row(FORBIDDEN, org_admin=OK)),
    (
        "org_apps",
        "POST",
        lambda w: ("/settings/apps/", {"action": "policy", "require_approval": "on"}),
        row(FORBIDDEN, org_admin=DONE),
    ),
    ("org_app", "GET", app_url("/settings/apps/{pk}/"), row(FORBIDDEN, org_admin=OK)),
    (
        "org_app",
        "POST",
        app_url("/settings/apps/{pk}/", action="approve"),
        row(FORBIDDEN, org_admin=DONE),
    ),
    # Anyone may ask; the answer is the "not approved yet" page.
    (
        "request_approval",
        "POST",
        app_url("/oauth/apps/{pk}/request-approval/"),
        row(FORBIDDEN),
    ),
    ("my_apps", "GET", lambda w: ("/account/apps/", {}), row(OK)),
    ("my_apps", "POST", lambda w: ("/account/apps/", {}), row(OK)),
    ("device", "GET", lambda w: ("/oauth/device/", {}), row(OK)),
    ("device", "POST", lambda w: ("/oauth/device/", {"user_code": "NOPE"}), row(OK)),
    ("device_confirm", "GET", device_grant_url(), row(HIDDEN, viewer=OK)),
    ("device_confirm", "POST", device_grant_url(action="deny"), row(HIDDEN, viewer=OK)),
    (
        "service_account",
        "POST",
        service_account_url(action="deactivate"),
        row(FORBIDDEN, org_admin=DONE),
    ),
]


def test_every_org_audit_and_settings_url_is_in_the_matrix() -> None:
    names = set()
    for namespace in ("org", "audit", "identity", "oauth"):
        _, resolver = get_resolver().namespace_dict[namespace]
        names |= {name for name in resolver.reverse_dict if isinstance(name, str)}
    assert names == {case[0] for case in CASES}


def test_post_only_endpoints_refuse_get() -> None:
    world = build_world()
    browser = world.browser("org_admin")
    for name, method, build, _ in CASES:
        if method == "POST" and not any(c[0] == name and c[1] == "GET" for c in CASES):
            url, _ = build(world)
            assert browser.get(url).status_code == 405, name


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize(
    ("name", "method", "build", "expected"), CASES, ids=[f"{c[1]} {c[0]}" for c in CASES]
)
def test_endpoint_matrix(
    principal: str,
    name: str,
    method: str,
    build: Callable[[World], tuple[str, dict[str, str]]],
    expected: dict[str, int | str],
) -> None:
    world = build_world()
    browser = world.browser(principal)
    url, data = build(world)
    response = browser.get(url) if method == "GET" else post(browser, url, data)
    want = expected[principal]
    if want == LOGIN:
        assert response.status_code == 302, response.status_code
        assert response["Location"].startswith("/login/"), response["Location"]
    else:
        assert response.status_code == want, (response.status_code, response.content[:300])

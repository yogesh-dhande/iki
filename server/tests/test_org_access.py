"""The endpoint matrix: every org and audit page against every principal.

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


def membership_url(w: World) -> tuple[str, dict[str, str]]:
    membership = w.project.memberships.get(user=w.users["viewer"])
    return f"/projects/{w.project.slug}/members/{membership.pk}/", {
        "action": "change",
        "role": "20",
    }


def team_member_remove_url(w: World) -> tuple[str, dict[str, str]]:
    return f"/teams/{w.team.slug}/members/{w.users['team_member'].pk}/remove/", {}


def person_url(w: World) -> tuple[str, dict[str, str]]:
    return f"/people/{w.users['viewer'].pk}/", {}


CASES: list[Case] = [
    ("project_list", "GET", lambda w: ("/projects/", {}), row(OK)),
    ("project_new", "GET", lambda w: ("/projects/new/", {}), row(OK)),
    (
        "project_new",
        "POST",
        lambda w: ("/projects/new/", {"team": str(w.team.pk), "name": "New Work"}),
        # Only team members may pick the team; for everyone else it isn't a valid choice.
        row(OK, team_member=DONE),
    ),
    ("project_detail", "GET", project_url(), row(HIDDEN, **PROJECT_VISIBLE)),
    ("project_members", "GET", project_url("members/"), row(HIDDEN, **PROJECT_VISIBLE)),
    (
        "project_members",
        "POST",
        lambda w: (
            f"/projects/{w.project.slug}/members/",
            {"add": "person", "person-person": "outsider@example.com", "person-role": "10"},
        ),
        row(
            HIDDEN,
            org_admin=DONE,
            admin=DONE,
            viewer=FORBIDDEN,
            editor=FORBIDDEN,
            runner=FORBIDDEN,
            via_team=FORBIDDEN,
            mixed=FORBIDDEN,
        ),
    ),
    (
        "project_member",
        "POST",
        membership_url,
        row(
            HIDDEN,
            org_admin=DONE,
            admin=DONE,
            viewer=FORBIDDEN,
            editor=FORBIDDEN,
            runner=FORBIDDEN,
            via_team=FORBIDDEN,
            mixed=FORBIDDEN,
        ),
    ),
    (
        "project_join",
        "POST",
        project_url("join/"),
        row(
            HIDDEN,
            org_admin=DONE,
            admin=DONE,
            viewer=FORBIDDEN,
            editor=FORBIDDEN,
            runner=FORBIDDEN,
            via_team=FORBIDDEN,
            mixed=FORBIDDEN,
        ),
    ),
    (
        "project_settings",
        "GET",
        project_url("settings/"),
        row(
            HIDDEN,
            admin=OK,
            org_admin=FORBIDDEN,
            viewer=FORBIDDEN,
            editor=FORBIDDEN,
            runner=FORBIDDEN,
            via_team=FORBIDDEN,
            mixed=FORBIDDEN,
        ),
    ),
    (
        "project_settings",
        "POST",
        lambda w: (f"/projects/{w.project.slug}/settings/", {"action": "archive"}),
        row(
            HIDDEN,
            admin=DONE,
            org_admin=FORBIDDEN,
            viewer=FORBIDDEN,
            editor=FORBIDDEN,
            runner=FORBIDDEN,
            via_team=FORBIDDEN,
            mixed=FORBIDDEN,
        ),
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
    ("person", "GET", person_url, row(FORBIDDEN, org_admin=OK)),
    (
        "person",
        "POST",
        lambda w: (f"/people/{w.users['viewer'].pk}/", {"roles": "auditor"}),
        row(FORBIDDEN, org_admin=DONE),
    ),
]


def test_every_org_and_audit_url_is_in_the_matrix() -> None:
    names = set()
    for namespace in ("org", "audit"):
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

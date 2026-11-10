"""The API's authorization matrix: every operation against every principal.

401 means "sign in first", 404 "you can't know this exists", 403 "you can
see it but not do this". A test fails if an operation in the OpenAPI
document isn't listed here, so no endpoint ships without its row.
"""

from collections.abc import Callable
from typing import Any

import pytest
from api_client import Api
from org_world import PRINCIPALS, World, build_world

from iki_server.accounts import service_accounts
from iki_server.api.v1 import api

pytestmark = pytest.mark.django_db

UNAUTHENTICATED = {"anonymous": 401, "inactive": 401}
PROJECT_VISIBLE = ("org_admin", "viewer", "editor", "runner", "admin", "via_team", "mixed")

Build = Callable[[World], tuple[str, Any]]


def row(default: int, **overrides: int) -> dict[str, int]:
    return {**dict.fromkeys(PRINCIPALS, default), **UNAUTHENTICATED, **overrides}


def visible(default_for_visible: int, **overrides: int) -> dict[str, int]:
    """A project operation: 404 unless the caller can see the project."""
    return row(404, **{**dict.fromkeys(PROJECT_VISIBLE, default_for_visible), **overrides})


def fixed(path: str, body: Any = None) -> Build:
    return lambda w: (path, body)


def viewer(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/users/{w.users['viewer'].pk}{suffix}", body)


def robot(body: Any = None) -> Build:
    def build(w: World) -> tuple[str, Any]:
        account = service_accounts.create("Robot", "", by=w.users["org_admin"])
        return f"/service-accounts/{account.pk}", body

    return build


def team(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/teams/{w.team.pk}{suffix}", body)


def project(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/projects/{w.project.pk}{suffix}", body)


def viewer_grant(w: World) -> tuple[str, Any]:
    membership = w.project.memberships.get(user=w.users["viewer"])
    return f"/projects/{w.project.pk}/members/{membership.pk}", None


ORG_ADMIN_ONLY = row(403, org_admin=200)
TEAM_VISIBLE = row(404, org_admin=200, team_member=200)
TEAM_MANAGE = row(404, org_admin=200, team_member=403)
PROJECT_MEMBERS = visible(403, org_admin=200, admin=200)

#: (method, OpenAPI path, how to build the request, expected status per principal)
CASES: list[tuple[str, str, Build, dict[str, int]]] = [
    ("GET", "/me", fixed("/me"), row(200)),
    ("GET", "/users", fixed("/users"), ORG_ADMIN_ONLY),
    ("GET", "/users", fixed("/users?email=editor@example.com"), row(200)),
    ("GET", "/users/{user_id}", viewer(), row(200)),
    ("PATCH", "/users/{user_id}", viewer(body={"active": False}), ORG_ADMIN_ONLY),
    ("DELETE", "/users/{user_id}/sessions", viewer("/sessions"), row(403, org_admin=204)),
    ("GET", "/users/{user_id}/org-roles", viewer("/org-roles"), ORG_ADMIN_ONLY),
    (
        "PUT",
        "/users/{user_id}/org-roles",
        viewer("/org-roles", {"roles": ["auditor"]}),
        ORG_ADMIN_ONLY,
    ),
    ("GET", "/service-accounts", fixed("/service-accounts"), ORG_ADMIN_ONLY),
    (
        "POST",
        "/service-accounts",
        fixed("/service-accounts", {"name": "LIMS sync"}),
        row(403, org_admin=201),
    ),
    ("GET", "/service-accounts/{account_id}", robot(), ORG_ADMIN_ONLY),
    (
        "PATCH",
        "/service-accounts/{account_id}",
        robot({"description": "Plates"}),
        ORG_ADMIN_ONLY,
    ),
    ("GET", "/teams", fixed("/teams"), row(200)),
    ("POST", "/teams", fixed("/teams", {"name": "Genomics"}), row(403, org_admin=201)),
    ("GET", "/teams/{team_id}", team(), TEAM_VISIBLE),
    ("PATCH", "/teams/{team_id}", team(body={"description": "Proteins"}), TEAM_MANAGE),
    ("GET", "/teams/{team_id}/members", team("/members"), TEAM_VISIBLE),
    (
        "PUT",
        "/teams/{team_id}/members/{user_id}",
        lambda w: (f"/teams/{w.team.pk}/members/{w.users['outsider'].pk}", None),
        TEAM_MANAGE,
    ),
    (
        "DELETE",
        "/teams/{team_id}/members/{user_id}",
        lambda w: (f"/teams/{w.team.pk}/members/{w.users['team_member'].pk}", None),
        row(404, org_admin=204, team_member=403),
    ),
    ("GET", "/projects", fixed("/projects"), row(200)),
    (
        "POST",
        "/projects",
        lambda w: ("/projects", {"team": str(w.team.pk), "name": "New Work"}),
        # Org Admins can see the team but only its members create projects in it.
        row(404, team_member=201, org_admin=403),
    ),
    ("GET", "/projects/{project_id}", project(), visible(200)),
    (
        "PATCH",
        "/projects/{project_id}",
        project(body={"description": "Kinases"}),
        visible(403, admin=200),
    ),
    ("GET", "/projects/{project_id}/members", project("/members"), visible(200)),
    (
        "PUT",
        "/projects/{project_id}/members/users/{user_id}",
        lambda w: (
            f"/projects/{w.project.pk}/members/users/{w.users['outsider'].pk}",
            {"role": "viewer"},
        ),
        PROJECT_MEMBERS,
    ),
    (
        "PUT",
        "/projects/{project_id}/members/teams/{team_id}",
        lambda w: (f"/projects/{w.project.pk}/members/teams/{w.team.pk}", {"role": "viewer"}),
        PROJECT_MEMBERS,
    ),
    (
        "DELETE",
        "/projects/{project_id}/members/{membership_id}",
        viewer_grant,
        visible(403, org_admin=204, admin=204),
    ),
]


def test_every_operation_is_in_the_matrix() -> None:
    schema = api.get_openapi_schema()
    operations = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, methods in schema["paths"].items()
        for method in methods
    }
    assert operations == {(case[0], case[1]) for case in CASES}


def test_every_operation_documents_its_summary() -> None:
    for methods in api.get_openapi_schema()["paths"].values():
        for operation in methods.values():
            assert operation.get("summary"), operation["operationId"]


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize(
    ("method", "template", "build", "expected"),
    CASES,
    ids=[f"{i:02d} {c[0]} {c[1]}" for i, c in enumerate(CASES)],
)
def test_operation_matrix(
    principal: str, method: str, template: str, build: Build, expected: dict[str, int]
) -> None:
    world = build_world()
    path, body = build(world)
    client = Api(world.browser(principal))
    response = client.call(method, path, body)
    assert response.status_code == expected[principal], (
        response.status_code,
        response.content[:300],
    )
    if response.status_code >= 400:
        assert response["Content-Type"] == "application/problem+json"

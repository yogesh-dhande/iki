"""The API's conventions: problems, auth and CSRF, rate limits, pagination, audit."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world

from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectRole, Team, TeamMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def api_as(world: World, principal: str) -> Api:
    return Api(world.browser(principal))


# --- Problems ------------------------------------------------------------------


def test_signing_in_is_required_and_said_how() -> None:
    response = Api().get("/me")
    problem(response, 401, "not_authenticated")
    assert response["WWW-Authenticate"] == (
        'Bearer realm="iki", resource_metadata="https://testserver/.well-known/oauth-protected-resource"'
    )


def test_unknown_paths_and_methods_are_problems_too(world: World) -> None:
    api = api_as(world, "viewer")
    missing = api.get("/nothing-here")
    problem(missing, 404, "not_found")
    assert "Content-Security-Policy" in missing and missing["Cache-Control"] == "no-store"
    response = api.delete("/me")
    problem(response, 405, "method_not_allowed")
    assert "GET" in response["Allow"]


def test_invalid_fields_point_at_themselves(world: World) -> None:
    api = api_as(world, "team_member")
    body = problem(api.post("/projects", {"team": str(world.team.pk)}), 422, "validation_failed")
    assert body["errors"] == [{"pointer": "#/name", "detail": "Field required"}]
    body = problem(api.get("/projects", limit=0), 422, "validation_failed")
    assert [e["parameter"] for e in body["errors"]] == ["limit"]
    problem(api.post("/projects", raw="{not json"), 400, "invalid_json")


def test_crashes_are_problems_with_the_request_id(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iki_server.org import permissions

    def broken(user: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(permissions, "visible_teams", broken)
    body = problem(api_as(world, "viewer").get("/teams"), 500, "internal_error")
    assert "boom" not in str(body)
    assert body["request_id"]


def test_refusals_are_conflicts(world: World) -> None:
    api = api_as(world, "admin")
    project = world.project
    # The only other Admin is deactivated, so the caller is the last one.
    mine = project.memberships.get(user=world.users["admin"])
    body = problem(api.delete(f"/projects/{project.pk}/members/{mine.pk}"), 409, "refused")
    assert "at least one Admin" in body["detail"]


def test_openapi_is_published_without_a_bundled_ui() -> None:
    api = Api()
    schema = api.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/projects" in schema.json()["paths"]
    problem(api.get("/docs"), 404, "not_found")


# --- Sessions, CSRF and the audit channel -------------------------------------


def test_unsafe_methods_need_the_csrf_token(world: World) -> None:
    api = api_as(world, "team_member")
    body = {"team": str(world.team.pk), "name": "Fragment Screen"}
    problem(api.post("/projects", body, csrf=False), 403, "csrf_failed")
    assert not Project.objects.filter(name="Fragment Screen").exists()
    assert api.post("/projects", body).status_code == 201


def test_pages_carry_the_csrf_token_for_scripts(world: World) -> None:
    page = world.browser("viewer").get("/projects/")
    assert b'<meta name="csrf-token" content="' in page.content


def test_changes_are_audited_through_the_api_channel(world: World) -> None:
    api = api_as(world, "team_member")
    created = api.post("/projects", {"team": str(world.team.pk), "name": "Fragment Screen"})
    event = AuditEvent.objects.get(action="project.created", target_id=created.json()["id"])
    assert event.via == "api"
    assert event.actor == world.users["team_member"]


def test_deactivated_people_are_not_signed_in(world: World) -> None:
    problem(api_as(world, "inactive").get("/me"), 401, "not_authenticated")


# --- Rate limits ---------------------------------------------------------------


def test_callers_are_rate_limited_per_account(world: World, settings: Any) -> None:
    settings.IKI_API_RATE_LIMIT_PER_MINUTE = 3
    api = api_as(world, "viewer")
    first = api.get("/me")
    assert first.status_code == 200
    assert first["RateLimit-Policy"] == '"api";q=3;w=60'
    assert first["RateLimit"].startswith('"api";r=2;t=')
    api.get("/me")
    api.get("/me")
    refused = api.get("/me")
    problem(refused, 429, "rate_limited")
    assert int(refused["Retry-After"]) >= 1
    assert refused["RateLimit"].startswith('"api";r=0;')
    # Someone else has their own allowance.
    assert api_as(world, "editor").get("/me").status_code == 200


def test_anonymous_callers_are_rate_limited_per_address() -> None:
    api = Api()
    for _ in range(60):
        api.get("/me")
    problem(api.get("/me"), 429, "rate_limited")


# --- Pagination ----------------------------------------------------------------


def test_lists_page_with_opaque_cursors(world: World) -> None:
    for n in range(5):
        Team.objects.create(name=f"Team {n}")
    api = api_as(world, "org_admin")
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        query: dict[str, Any] = {"limit": 3}
        if cursor:
            query["cursor"] = cursor
        page = api.get("/teams", **query).json()
        seen += [team["name"] for team in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert len(seen) == len(set(seen)) == Team.objects.count() == 7


def test_forged_cursors_are_refused(world: World) -> None:
    body = problem(api_as(world, "org_admin").get("/teams", cursor="abc"), 422, "validation_failed")
    assert body["errors"] == [{"parameter": "cursor", "detail": "Not a cursor from this API."}]


# --- Endpoints -----------------------------------------------------------------


def test_me(world: World) -> None:
    body = api_as(world, "org_admin").get("/me").json()
    assert body == {
        "id": str(world.users["org_admin"].pk),
        "email": "org_admin@example.com",
        "name": "Org Admin",
        "kind": "person",
        "active": True,
        "org_roles": ["org_admin"],
    }


def test_projects_show_your_role_and_filter(world: World) -> None:
    api = api_as(world, "mixed")
    items = api.get("/projects").json()["items"]
    assert {p["name"]: p["my_role"] for p in items} == {
        "Kinase Atlas": "editor",
        "Old Screens": "editor",
    }
    assert [p["name"] for p in api.get("/projects", archived=False).json()["items"]] == [
        "Kinase Atlas"
    ]
    admin = api_as(world, "org_admin")
    assert {p["my_role"] for p in admin.get("/projects").json()["items"]} == {None}
    assert admin.get("/projects", member=True).json()["items"] == []


def test_archived_projects_are_restored_before_changes(world: World) -> None:
    api = api_as(world, "admin")
    path = f"/projects/{world.archived.pk}"
    body = problem(api.patch(path, {"name": "New Screens"}), 409, "refused")
    assert "Restore" in body["detail"]
    world.archived.refresh_from_db()
    assert world.archived.is_archived and world.archived.name == "Old Screens"
    changed = api.patch(path, {"name": "New Screens", "archived": False}).json()
    assert changed["name"] == "New Screens" and changed["archived"] is False


def test_failed_changes_change_nothing(world: World) -> None:
    Project.objects.create(team=world.team, name="Taken")
    api = api_as(world, "admin")
    problem(
        api.patch(f"/projects/{world.archived.pk}", {"archived": False, "name": "Taken"}),
        409,
        "refused",
    )
    world.archived.refresh_from_db()
    assert world.archived.is_archived


def test_project_members_can_be_people_and_teams(world: World) -> None:
    api = api_as(world, "admin")
    project = world.project
    outsider = world.users["outsider"]
    granted = api.put(f"/projects/{project.pk}/members/users/{outsider.pk}", {"role": "runner"})
    assert granted.status_code == 200
    assert granted.json()["role"] == "runner" and granted.json()["user"]["id"] == str(outsider.pk)
    team = api.put(f"/projects/{project.pk}/members/teams/{world.team.pk}", {"role": "viewer"})
    assert team.json()["team"]["name"] == "Structural Biology" and team.json()["user"] is None
    members = api.get(f"/projects/{project.pk}/members").json()["items"]
    assert {(m["user"] or m["team"])["id"]: m["role"] for m in members}[
        str(outsider.pk)
    ] == "runner"
    removed = api.delete(f"/projects/{project.pk}/members/{granted.json()['id']}")
    assert removed.status_code == 204
    assert not project.memberships.filter(user=outsider).exists()
    problem(
        api.put(f"/projects/{project.pk}/members/users/{outsider.pk}", {"role": "owner"}),
        422,
        "validation_failed",
    )


def test_teams_linked_to_groups_refuse_hand_changes(world: World) -> None:
    from iki_server.scim.models import ScimGroup

    ScimGroup.objects.create(display_name="SG-Structural", team=world.team)
    api = api_as(world, "org_admin")
    assert api.get(f"/teams/{world.team.pk}").json()["linked_groups"] == ["SG-Structural"]
    body = problem(
        api.put(f"/teams/{world.team.pk}/members/{world.users['outsider'].pk}"), 409, "refused"
    )
    assert "SG-Structural" in body["detail"]


def test_team_membership_by_api(world: World) -> None:
    api = api_as(world, "org_admin")
    outsider = world.users["outsider"]
    added = api.put(f"/teams/{world.other_team.pk}/members/{outsider.pk}")
    assert added.status_code == 200 and added.json()["user"]["email"] == outsider.email
    assert TeamMembership.objects.filter(team=world.other_team, user=outsider).exists()
    assert api.delete(f"/teams/{world.other_team.pk}/members/{outsider.pk}").status_code == 204
    problem(api.delete(f"/teams/{world.other_team.pk}/members/{outsider.pk}"), 404, "not_found")


def test_looking_people_up(world: World) -> None:
    api = api_as(world, "viewer")
    problem(api.get("/users"), 403, "forbidden")
    found = api.get("/users", email="EDITOR@example.com").json()["items"]
    assert [u["email"] for u in found] == ["editor@example.com"]
    assert api_as(world, "org_admin").get("/users", active=False).json()["items"][0]["email"] == (
        "inactive@example.com"
    )


def test_org_roles_by_api(world: World) -> None:
    api = api_as(world, "org_admin")
    viewer = world.users["viewer"]
    response = api.put(f"/users/{viewer.pk}/org-roles", {"roles": ["auditor"]})
    assert response.json() == {"roles": ["auditor"]}
    assert api.get(f"/users/{viewer.pk}/org-roles").json() == {"roles": ["auditor"]}
    me = world.users["org_admin"]
    body = problem(api.put(f"/users/{me.pk}/org-roles", {"roles": []}), 409, "refused")
    assert "at least one Org Admin" in body["detail"]


def test_role_names_cover_every_project_role() -> None:
    from iki_server.api.schemas import ProjectRoleName

    assert set(ProjectRoleName.__args__) == {r.name.lower() for r in ProjectRole}  # type: ignore[attr-defined]

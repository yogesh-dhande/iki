"""A starter crosses two projects. Its setup must be authorized in each (ADR 0198)."""

from typing import Any
from uuid import uuid4

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def choice(world: World, requester: Project) -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "flow": "research",
        "project": str(world.project.pk),
        "requester_project": str(requester.pk),
        "role_assignments": {},
    }


@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_setup_requires_access_to_the_requester(world: World, operation: str) -> None:
    requester = Project.objects.create(team=world.team, name="Assay requester")
    path = f"/starters/dose-response/{operation}"
    api = Api(world.browser("admin"))
    body = choice(world, requester)
    problem(api.post(path, body), 404, "not_found")
    grant = ProjectMembership.objects.create(
        project=requester, user=world.users["admin"], role=ProjectRole.VIEWER
    )
    problem(api.post(path, body), 403, "forbidden")
    grant.role = ProjectRole.EDITOR
    grant.save(update_fields=["role"])
    # The editor now reaches the catalog. The fixture deployment has no dose-response starter.
    problem(api.post(path, body), 404, "not_found")


def test_scope_limited_to_service_project_cannot_apply_elsewhere(world: World) -> None:
    requester = Project.objects.create(team=world.team, name="Assay requester")
    ProjectMembership.objects.create(
        project=requester, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    body = choice(world, requester)
    path = "/starters/dose-response/apply"
    _, read_only = signed_in_token(world, "admin", scope="read")
    problem(bearer(read_only["access_token"], "POST", path, body), 403, "insufficient_scope")
    _, scoped = signed_in_token(world, "admin", scope="read write", projects=[world.project])
    problem(bearer(scoped["access_token"], "POST", path, body), 404, "not_found")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("flow", "release"),
        ("project", "somewhere"),
        ("role_assignments", {"iki/plate_assays.reviewer": ["a name"]}),
        ("undeclared", True),
    ],
)
def test_setup_rejects_unusable_payload_before_application(
    world: World, field: str, value: Any
) -> None:
    api = Api(world.browser("admin"))
    body = {**choice(world, world.project), "requester_project": str(uuid4()), field: value}
    error = problem(api.post("/starters/dose-response/apply", body), 422, "validation_failed")
    assert error["errors"][0]["pointer"].startswith(f"#/{field}")

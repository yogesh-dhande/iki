"""A starter configures the assay project with its administrator’s permission."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def choice(world: World, operation: str = "apply") -> dict[str, Any]:
    """A setup, and for applying it, the plan its preview would have returned."""
    return {
        "version": "2.0.0",
        "flow": "research",
        "project": str(world.project.pk),
        "role_assignments": {},
        **({"plan": "0" * 64} if operation == "apply" else {}),
    }


@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_only_an_assay_admin_can_configure_it(world: World, operation: str) -> None:
    path = f"/starters/dose-response/{operation}"
    body = choice(world, operation)
    problem(Api(world.browser("outsider")).post(path, body), 404, "not_found")
    problem(Api(world.browser("editor")).post(path, body), 403, "forbidden")
    # Only the project Admin reaches the catalog; the fixture deployment has no plate plugin.
    problem(Api(world.browser("admin")).post(path, body), 404, "not_found")


def test_a_token_needs_write_scope_in_the_selected_project(world: World) -> None:
    path = "/starters/dose-response/apply"
    body = choice(world)
    _, read_only = signed_in_token(world, "admin", scope="read")
    problem(bearer(read_only["access_token"], "POST", path, body), 403, "insufficient_scope")
    other = Project.objects.create(team=world.team, name="Second assay project")
    ProjectMembership.objects.create(
        project=other, user=world.users["admin"], role=ProjectRole.ADMIN
    )
    _, scoped = signed_in_token(world, "admin", scope="read write", projects=[world.project])
    problem(
        bearer(scoped["access_token"], "POST", path, {**body, "project": str(other.pk)}),
        404,
        "not_found",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("flow", "release"),
        ("project", "somewhere"),
        ("role_assignments", {"iki/plate_assays.reviewer": ["a name"]}),
        ("requester_project", "removed"),
        ("undeclared", True),
    ],
)
def test_setup_rejects_unusable_payload_before_application(
    world: World, field: str, value: Any
) -> None:
    api = Api(world.browser("admin"))
    body = {**choice(world), field: value}
    error = problem(api.post("/starters/dose-response/apply", body), 422, "validation_failed")
    assert error["errors"][0]["pointer"].startswith(f"#/{field}")

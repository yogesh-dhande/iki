"""A token given only some projects acts in those and nowhere else (ADR 0152).

Its person may be an Org Admin, a Compute Admin and an Auditor, but a token
they gave only some projects takes no action on the organization, its teams
or its registries. Every operation in the API is tried, from the access
matrix, which itself lists every operation in the OpenAPI document, with a
token given another project: one it may still take must be listed in
REACHABLE, or it's refused.
"""

import secrets
from datetime import timedelta
from typing import Any

import pytest
from api_client import problem
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone
from oauth2_provider.models import get_access_token_model
from org_world import World, build_world
from test_api_access import CASES, Build
from test_oauth import bearer

from iki_server.accounts.models import User
from iki_server.oauth import services as oauth
from iki_server.oauth.models import Approval, Client
from iki_server.oauth.scopes import SCOPES
from iki_server.org import permissions
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Registrar,
    Registry,
    TeamMembership,
)
from iki_server.org.permissions import Action

pytestmark = pytest.mark.django_db

AccessToken = get_access_token_model()

#: Operations a token given another project may still take: the person's own, and what
#: every signed-in person reads about the deployment. Lists answer with nothing.
REACHABLE = {
    # The person's own, and the token's projects.
    ("GET", "/me"),
    ("GET", "/projects"),
    ("GET", "/me/signing"),
    ("GET", "/me/notification-settings"),
    ("PATCH", "/me/notification-settings"),
    ("POST", "/notifications/read"),
    ("GET", "/notifications/summary"),
    ("GET", "/signatures/certification"),
    ("POST", "/signatures/certification"),
    # Looking someone up by email to give them a role in a project (not listing everyone:
    # LOOKUPS), and who they are.
    ("GET", "/users"),
    ("GET", "/users/{user_id}"),
    # What the deployment offers, which every signed-in person reads.
    ("GET", "/project-roles"),
    ("GET", "/lab-roles"),
    ("GET", "/entity-types"),
    ("GET", "/entity-types/{name}"),
    ("GET", "/entity-types/{name}/flow"),
    ("GET", "/starters"),
    ("GET", "/actions"),
    ("GET", "/actions/{name}"),
    ("GET", "/workflows"),
    ("GET", "/workflows/{name}"),
    ("GET", "/triggers"),
    ("GET", "/components"),
    ("GET", "/apps/guide"),
    ("POST", "/apps/check"),
    # Reading HELM against the deployment's monomers: nothing is saved.
    ("POST", "/helm/read"),
    ("GET", "/webhook-events"),
    ("GET", "/webhook-policy"),
    ("GET", "/signing-keys"),
    ("GET", "/storage/labels"),
    ("GET", "/registrations/locations"),
    ("GET", "/registries"),
    ("GET", "/registries/{registry_id}"),
    ("GET", "/registries/{registry_id}/registrars"),
}

#: Operations reachable only as these paths: the rest of their uses aren't.
LOOKUPS = {("GET", "/users"): "/users?email="}


def everything(world: World) -> User:
    """The Org Admin, made everything else too: Compute Admin, Auditor, Admin of the
    project, a member of its team and a registrar of its registry."""
    person = world.users["org_admin"]
    for role in (OrgRole.COMPUTE_ADMIN, OrgRole.AUDITOR):
        OrgRoleAssignment.objects.create(user=person, role=role)
    for project in (world.project, world.archived):
        ProjectMembership.objects.create(project=project, user=person, role=ProjectRole.ADMIN)
    TeamMembership.objects.create(team=world.team, user=person)
    Registrar.objects.create(registry=Registry.default(), user=person)
    return person


def elsewhere(world: World, person: User) -> Project:
    """Another project of the team's, which the person is an Admin of."""
    project = Project.objects.create(team=world.team, name="Elsewhere")
    ProjectMembership.objects.create(project=project, user=person, role=ProjectRole.ADMIN)
    return project


def token_for(
    person: User, *, projects: list[Project] | None, scopes: list[str] | None = None
) -> str:
    """A token for an approved app the person allowed every scope, in these projects or
    (None) all of them."""
    client = Client.objects.create(
        name="Notebook",
        client_type=Client.CLIENT_PUBLIC,
        authorization_grant_type=Client.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://notebook.example.com/callback",
        approval=Approval.APPROVED,
    )
    granted = scopes or list(SCOPES)
    oauth.grant(
        person,
        client,
        oauth.Grant(granted, all_projects=projects is None, projects=projects or []),
    )
    token = secrets.token_urlsafe(32)
    AccessToken.objects.create(
        user=person,
        application=client,
        token=token,
        scope=" ".join(granted),
        expires=timezone.now() + timedelta(hours=1),
    )
    return token


def _targets(world: World) -> dict[str, Any]:
    return {
        "org": None,
        "team": world.team,
        "registry": Registry.default(),
        "project": world.project,
    }


def _kind(action: Action) -> str:
    if action is Action.PROJECT_IMPORT:
        return "project"
    if action in permissions.ORG_ACTIONS:
        return "org"
    if action in permissions.TEAM_ACTIONS:
        return "team"
    return "registry"


@pytest.mark.parametrize(
    "action",
    sorted(set(Action) - permissions.PROJECT_ACTIONS | {Action.PROJECT_IMPORT}),
    ids=str,
)
def test_no_action_beyond_its_projects(action: Action) -> None:
    """Nor importing a bundle, which adds people to the organization as an Org Admin."""
    world = build_world()
    person = everything(world)
    target = _targets(world)[_kind(action)]
    everywhere = permissions.TokenLimits(scopes=frozenset(SCOPES))
    chosen = permissions.TokenLimits(
        scopes=frozenset(SCOPES), projects=frozenset({world.project.pk}), only_projects=True
    )
    permissions.limit_to(person, everywhere)
    assert permissions.can(person, action, target)
    permissions.limit_to(person, chosen)
    permissions.forget(person)
    assert not permissions.can(person, action, target)


def test_in_its_projects_it_acts_as_its_person_may() -> None:
    world = build_world()
    person = everything(world)
    permissions.limit_to(
        person,
        permissions.TokenLimits(
            scopes=frozenset(SCOPES), projects=frozenset({world.project.pk}), only_projects=True
        ),
    )
    for action in permissions.PROJECT_ACTIONS - {Action.PROJECT_IMPORT}:
        assert permissions.can(person, action, world.project), action
    assert not permissions.can(person, Action.PROJECT_SEE, world.archived)
    assert list(permissions.visible_projects(person)) == [world.project]
    assert not permissions.visible_teams(person).exists()


#: The organization's: its costs, webhooks, people, teams, compute and settings.
ORGANIZATION = [
    ("GET", "/compute/costs", None),
    ("GET", "/compute/costs/records.csv", None),
    ("GET", "/compute/budgets", None),
    ("GET", "/webhooks", None),
    ("POST", "/webhooks", {"url": "https://hooks.example.com/b", "events": ["team.created"]}),
    ("PATCH", "/webhook-policy", {"project_webhooks": False}),
    ("GET", "/compute/workers", None),
    ("GET", "/service-accounts", None),
    ("GET", "/users", None),
    ("POST", "/teams", {"name": "Genomics"}),
    ("GET", "/teams", None),
    ("GET", "/outside-apis", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ORGANIZATION, ids=lambda v: str(v))
def test_the_organization_is_for_tokens_given_every_project(
    method: str, path: str, body: Any
) -> None:
    world = build_world()
    person = everything(world)
    everywhere = token_for(person, projects=None)
    response = bearer(everywhere, method, path, body)
    assert response.status_code < 300, (response.status_code, response.content[:300])

    chosen = token_for(person, projects=[world.project])
    response = bearer(chosen, method, path, body)
    if path == "/teams" and method == "GET":
        # A list: of no teams.
        assert response.status_code == 200 and response.json()["items"] == []
        return
    found = problem(response, 403, "token_limited_to_projects")
    assert "all your projects" in found["detail"]


def test_it_neither_sees_nor_saves_a_search_of_every_project() -> None:
    """A search of no project is of every project its person reads (ADR 0176): beyond a
    token given some of them. Its searches in its projects it still sees."""
    from iki_server.searches import services as searches

    world = build_world()
    person = world.users["editor"]
    everywhere = searches.create(person, None, name="Every kinase", query={"q": "kinase"})
    mine = searches.create(person, world.project, name="Atlas kinases", query={"q": "kinase"})
    token = token_for(person, projects=[world.project])

    listed = bearer(token, "GET", "/saved-searches", None).json()
    assert [s["id"] for s in listed] == [str(mine.pk)]
    response = bearer(token, "GET", f"/saved-searches/{everywhere.pk}", None)
    assert response.status_code == 404
    found = problem(
        bearer(token, "POST", "/saved-searches", {"name": "All", "query": {}}),
        403,
        "token_limited_to_projects",
    )
    assert "every project" in found["detail"]
    # Given every project, it does both.
    whole = token_for(person, projects=None)
    assert len(bearer(whole, "GET", "/saved-searches", None).json()) == 2


def test_a_project_token_can_read_flow_definitions_and_only_its_projects_starter_choice() -> None:
    world = build_world()
    person = everything(world)
    chosen = elsewhere(world, person)
    token = token_for(person, projects=[chosen])

    # The schema and installed catalog are the same for the deployment. A current
    # starter choice belongs to one project and requires read access to that project.
    flow = bearer(token, "GET", "/entity-types/acme/protein.Batch/flow", None)
    assert flow.status_code == 200
    assert flow.json()["fields"][0]["initial"] == "in_testing"
    assert bearer(token, "GET", "/starters", None).status_code == 200
    assert bearer(token, "GET", f"/starters?project={chosen.pk}", None).status_code == 200
    problem(bearer(token, "GET", f"/starters?project={world.project.pk}", None), 404, "not_found")


def test_its_person_is_told_only_what_their_roles_allow() -> None:
    """Someone who isn't an Org Admin is refused as without a token: the project choice
    isn't why."""
    world = build_world()
    token = token_for(world.users["viewer"], projects=[world.project])
    problem(bearer(token, "POST", "/teams", {"name": "Genomics"}), 403, "forbidden")


@pytest.mark.parametrize(
    ("method", "template", "build"),
    [(c[0], c[1], c[2]) for c in CASES],
    ids=[f"{i:02d} {c[0]} {c[1]}" for i, c in enumerate(CASES)],
)
def test_every_operation_beyond_its_projects_is_refused(
    method: str, template: str, build: Build, request: pytest.FixtureRequest
) -> None:
    if (method, template) in REACHABLE and (method, template) not in LOOKUPS:
        pytest.skip("within the token's projects, or the person's own")
    if template.startswith(
        ("/uploads", "/files", "/registrations", "/bundles", "/imports", "/spreadsheet")
    ) or (template.endswith(("/bundles", "/imports", "/spreadsheet-imports"))):
        request.getfixturevalue("s3")
        settings = request.getfixturevalue("settings")
        settings.IKI_FILES_REGISTER_LOCATIONS = ["s3://lab-data/runs/"]
    if template.startswith("/compute/slurm"):
        from test_slurm import with_cluster

        with_cluster(request)
    world = build_world()
    person = everything(world)
    token = token_for(person, projects=[elsewhere(world, person)])
    path, body, *headers = build(world)
    if path.startswith(LOOKUPS.get((method, template), "\0")):
        pytest.skip("a lookup")
    response = bearer(token, method, path, body, headers[0] if headers else None)
    if response.status_code == 200 and response["Content-Type"].startswith("text/csv"):
        # A download of a search, of nothing it may reach: its heading row alone.
        assert len(response.getvalue().decode().splitlines()) == 1, response.getvalue()
        return
    if response.status_code == 200:
        # A list or a search, of nothing it may reach; or counts (ADR 0176), of nothing.
        found = response.json()
        if isinstance(found, dict) and "groups" in found:
            # Searching everything (ADR 0176): every kind, and nothing of any.
            assert [g["items"] for g in found["groups"]] == [[]] * len(found["groups"]), found
            assert all(g["total"] == 0 for g in found["groups"]), found
            return
        if isinstance(found, dict) and "types" in found:
            assert found == {"types": [], "total": 0}, found
            return
        if isinstance(found, dict) and "more_than_total" in found:
            # How many files match (ADR 0177): none.
            assert found == {"total": 0, "more_than_total": False}, found
            return
        assert (found.get("items") if isinstance(found, dict) else found) == [], found
        return
    assert response.status_code in (403, 404), (response.status_code, response.content[:300])


#: The views under /api/v1 outside the OpenAPI document: event streams.
STREAMS = {
    "api/v1/events": "the person's own",
    "api/v1/runs/<str:ref>/events": "a run in a project",
    "api/v1/batches/<str:ref>/events": "a batch in a project",
    "api/v1/workflow-runs/<str:ref>/events": "a workflow run in a project",
    "api/v1/compute/setups/<str:ref>/events": "the organization's compute",
}


def _api_routes(patterns: list[URLPattern | URLResolver], prefix: str = "") -> set[str]:
    found = set()
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            if route != "api/v1/":  # the OpenAPI document's, tried above
                found |= _api_routes(pattern.url_patterns, route)
        elif route.startswith("api/v1/"):
            found.add(route)
    return found


def test_every_stream_is_accounted_for() -> None:
    """The organization's (a setup build's) refuses such tokens: test_realtime."""
    assert _api_routes(get_resolver().url_patterns) == STREAMS.keys()


def test_what_it_reaches_is_in_the_api() -> None:
    assert {(c[0], c[1]) for c in CASES} >= REACHABLE

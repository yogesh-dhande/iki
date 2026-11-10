"""The authorization matrix: every principal against every action (ADR 0003, ADR 0024).

The expected table is written out here by hand, independently of the
permission service's own tables. Any action the table doesn't grant must be
refused, so a new action is denied to everyone until this table says
otherwise.
"""

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from org_world import PRINCIPALS, World, build_world

from iki_server.org import permissions
from iki_server.org.models import ProjectMembership, ProjectRole, TeamMembership
from iki_server.org.permissions import Action

pytestmark = pytest.mark.django_db

A = Action
VIEWER = {A.PROJECT_SEE, A.PROJECT_READ}
EDITOR = VIEWER | {A.PROJECT_EDIT, A.RUN_CLIENT, A.RUN_BACKEND}
RUNNER = EDITOR | {A.RUN_CONTAINER, A.RUN_VM}
ADMIN = RUNNER | {A.PROJECT_MANAGE, A.PROJECT_MEMBERS_MANAGE, A.PROJECT_WEBHOOKS_MANAGE}
#: Still allowed on an archived project.
ARCHIVED_OK = {A.PROJECT_SEE, A.PROJECT_READ, A.PROJECT_MANAGE, A.PROJECT_MEMBERS_MANAGE}

#: principal -> (organization actions, actions on the owning team, actions on the project)
EXPECTED: dict[str, tuple[set[Action], set[Action], set[Action]]] = {
    "anonymous": (set(), set(), set()),
    "inactive": (set(), set(), set()),
    "outsider": (set(), set(), set()),
    "team_member": (set(), {A.TEAM_VIEW, A.PROJECT_CREATE}, set()),
    "org_admin": (
        {A.ORG_MANAGE, A.TEAM_CREATE, A.ORG_WEBHOOKS_MANAGE},
        {A.TEAM_VIEW, A.TEAM_MANAGE},
        {A.PROJECT_SEE, A.PROJECT_MEMBERS_MANAGE},
    ),
    "compute_admin": ({A.COMPUTE_MANAGE}, set(), set()),
    "auditor": ({A.AUDIT_READ}, set(), set()),
    "viewer": (set(), set(), VIEWER),
    "editor": (set(), set(), EDITOR),
    "runner": (set(), set(), RUNNER),
    "admin": (set(), set(), ADMIN),
    "via_team": (set(), set(), EDITOR),
    "mixed": (set(), set(), EDITOR),  # Viewer directly, Editor through a team: the higher wins
}


@pytest.fixture
def world() -> World:
    return build_world()


def subject(world: World, principal: str) -> object:
    from django.contrib.auth.models import AnonymousUser

    return world.user(principal) or AnonymousUser()


def test_every_principal_is_in_the_matrix() -> None:
    assert set(EXPECTED) == set(PRINCIPALS)


def test_every_action_has_a_scope() -> None:
    scopes = (permissions.ORG_ACTIONS, permissions.TEAM_ACTIONS, permissions.PROJECT_ACTIONS)
    assert sum(len(s) for s in scopes) == len(Action)
    assert set().union(*scopes) == set(Action)


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize("action", sorted(permissions.ORG_ACTIONS))
def test_organization_actions(world: World, principal: str, action: Action) -> None:
    expected = action in EXPECTED[principal][0]
    assert permissions.can(subject(world, principal), action) is expected


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize("action", sorted(permissions.TEAM_ACTIONS))
def test_team_actions(world: World, principal: str, action: Action) -> None:
    expected = action in EXPECTED[principal][1]
    assert permissions.can(subject(world, principal), action, world.team) is expected


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize("action", sorted(permissions.PROJECT_ACTIONS))
def test_project_actions(world: World, principal: str, action: Action) -> None:
    expected = action in EXPECTED[principal][2]
    assert permissions.can(subject(world, principal), action, world.project) is expected


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize("action", sorted(permissions.PROJECT_ACTIONS))
def test_archived_project_actions(world: World, principal: str, action: Action) -> None:
    expected = action in EXPECTED[principal][2] & ARCHIVED_OK
    assert permissions.can(subject(world, principal), action, world.archived) is expected


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_other_teams_grant_nothing_by_membership_alone(world: World, principal: str) -> None:
    """Belonging to a team that holds a project grant doesn't let you manage that team."""
    allowed = {
        action
        for action in permissions.TEAM_ACTIONS
        if permissions.can(subject(world, principal), action, world.other_team)
    }
    expected = {
        "org_admin": {A.TEAM_VIEW, A.TEAM_MANAGE},
        "via_team": {A.TEAM_VIEW, A.PROJECT_CREATE},
        "mixed": {A.TEAM_VIEW, A.PROJECT_CREATE},
    }.get(principal, set())
    assert allowed == expected


def test_archived_teams_get_no_new_projects(world: World) -> None:
    member = world.users["team_member"]
    world.team.archived_at = timezone.now()
    world.team.save()
    assert not permissions.can(member, A.PROJECT_CREATE, world.team)
    assert permissions.can(member, A.TEAM_VIEW, world.team)
    assert world.team not in permissions.teams_for_new_project(member)


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_visible_projects_match_project_see(world: World, principal: str) -> None:
    user = subject(world, principal)
    visible = set(permissions.visible_projects(user))
    for project in (world.project, world.archived):
        assert (project in visible) is permissions.can(user, A.PROJECT_SEE, project)


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_visible_teams_match_team_view(world: World, principal: str) -> None:
    user = subject(world, principal)
    visible = set(permissions.visible_teams(user))
    for team in (world.team, world.other_team):
        assert (team in visible) is permissions.can(user, A.TEAM_VIEW, team)


def test_visible_projects_carry_the_effective_role(world: World) -> None:
    roles = {p.name: p.my_role for p in permissions.visible_projects(world.users["mixed"])}  # type: ignore[attr-defined]
    assert roles == {"Kinase Atlas": ProjectRole.EDITOR, "Old Screens": ProjectRole.EDITOR}
    admin_sees = {p.name: p.my_role for p in permissions.visible_projects(world.users["org_admin"])}  # type: ignore[attr-defined]
    assert admin_sees == {"Kinase Atlas": None, "Old Screens": None}


def test_wrong_targets_are_programming_errors(world: World) -> None:
    user = world.users["admin"]
    with pytest.raises(TypeError):
        permissions.can(user, A.ORG_MANAGE, world.project)
    with pytest.raises(TypeError):
        permissions.can(user, A.PROJECT_READ, world.team)
    with pytest.raises(TypeError):
        permissions.can(user, A.TEAM_VIEW)


def test_require_raises_permission_denied(world: World) -> None:
    permissions.require(world.users["viewer"], A.PROJECT_READ, world.project)
    with pytest.raises(PermissionDenied):
        permissions.require(world.users["viewer"], A.PROJECT_EDIT, world.project)


def test_decisions_are_cached_for_the_request(
    world: World, django_assert_num_queries: object
) -> None:
    user = world.users["editor"]
    for action in permissions.PROJECT_ACTIONS:
        permissions.can(user, action, world.project)
    with django_assert_num_queries(0):  # type: ignore[operator]
        for action in permissions.PROJECT_ACTIONS:
            permissions.can(user, action, world.project)


def test_forget_picks_up_role_changes(world: World) -> None:
    user = world.users["outsider"]
    assert not permissions.can(user, A.PROJECT_READ, world.project)
    ProjectMembership.objects.create(project=world.project, user=user, role=ProjectRole.VIEWER)
    assert not permissions.can(user, A.PROJECT_READ, world.project)  # still cached
    permissions.forget(user)
    assert permissions.can(user, A.PROJECT_READ, world.project)


def test_leaving_a_team_removes_its_grants(world: World) -> None:
    user = world.users["via_team"]
    TeamMembership.objects.filter(user=user).delete()
    permissions.forget(user)
    assert permissions.project_role(user, world.project) is None
    assert not permissions.can(user, A.PROJECT_SEE, world.project)

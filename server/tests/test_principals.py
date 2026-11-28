"""A delegated token does only what both its person and its agent may (ADR 0073).

Every public predicate of the permission service is asked here under the widest delegated
token there is: every scope, every project. So the only thing between a grant one side
holds and the request is how the predicate combines the two. Each grant is given to the
person alone, to the agent alone, and to both, and only both counts.

Grants are written straight to their tables, past what appointing allows today (a service
account can't be an Org Admin, a registrar or a list curator): the predicates mustn't lean
on those rules to stay narrow.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from iki_server.accounts.models import SERVICE_ACCOUNT_DOMAIN, Kind, User
from iki_server.oauth.scopes import SCOPES
from iki_server.org import permissions
from iki_server.org.models import (
    LabRoleGrant,
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Registry,
    RegistryRole,
    Team,
    TeamMembership,
)
from iki_server.org.permissions import Action
from iki_server.setup.models import Installation

pytestmark = pytest.mark.django_db

QA = "acme/protein.qa"
LIST = "iki/lots.LotMethod"
OTHER_LIST = "iki/lots.StorageCondition"


@dataclass
class Lab:
    team: Team
    project: Project
    registry: Registry
    #: Holds an Admin grant on the project; its members hold it through the team.
    group: Team
    person: User
    agent: User

    def request(self) -> User:
        """The person, as a request with a delegated token through the agent makes them."""
        permissions.limit_to(
            self.person,
            permissions.TokenLimits(scopes=frozenset(SCOPES), alongside=self.agent),
        )
        return self.person


@pytest.fixture
def lab() -> Lab:
    Installation.objects.update_or_create(
        pk=1, defaults={"organization_name": "Example Bio", "setup_completed_at": timezone.now()}
    )
    team = Team.objects.create(name="Structural Biology")
    group = Team.objects.create(name="Assay Development")
    project = Project.objects.create(team=team, name="Kinase Atlas")
    ProjectMembership.objects.create(project=project, team=group, role=ProjectRole.ADMIN)
    person = User.objects.create_user("ana@example.com", "Ana Lima")
    agent = User.objects.create(
        kind=Kind.SERVICE, email=f"triage-bot@{SERVICE_ACCOUNT_DOMAIN}", name="Triage bot"
    )
    return Lab(team, project, Registry.default(), group, person, agent)


Grant = Callable[[Lab, User], object]


def _org(role: OrgRole) -> Grant:
    return lambda lab, user: OrgRoleAssignment.objects.create(user=user, role=role)


def _project(role: ProjectRole) -> Grant:
    return lambda lab, user: ProjectMembership.objects.create(
        project=lab.project, user=user, role=role
    )


def _lab_role(lab: Lab, user: User) -> object:
    membership = ProjectMembership.objects.get(project=lab.project, user=user)
    return LabRoleGrant.objects.create(membership=membership, role=QA)


def _allowed(action: Action, target: Callable[[Lab], object] = lambda lab: None) -> "Check":
    def check(lab: Lab, user: User) -> bool:
        try:
            permissions.require(user, action, target(lab))  # type: ignore[arg-type]
        except PermissionDenied:
            return False
        return True

    return Check("require", check, action.value)


@dataclass(frozen=True)
class Check:
    #: The public predicate it asks.
    predicate: str
    #: Whether the request has what the grant gives, by that predicate.
    gives: Callable[[Lab, User], bool]
    detail: str = ""

    @property
    def id(self) -> str:
        return f"{self.predicate}:{self.detail}" if self.detail else self.predicate


def _project_target(lab: Lab) -> Project:
    return lab.project


def _team_target(lab: Lab) -> Team:
    return lab.team


def _registry_target(lab: Lab) -> Registry:
    return lab.registry


def _my_role(lab: Lab, user: User) -> ProjectRole | None:
    found = permissions.with_project_roles(user, Project.objects.filter(pk=lab.project.pk))
    return found.get().my_role  # type: ignore[attr-defined, no-any-return]


def _remembered(predicate: Callable[[Lab, User], bool]) -> Callable[[Lab, User], bool]:
    """`predicate`, after looking up the request's roles in the project in bulk."""

    def check(lab: Lab, user: User) -> bool:
        permissions.remember_roles(user, [lab.project])
        return predicate(lab, user)

    return check


_PROJECT_ROLE = [
    Check("project_role", lambda lab, u: permissions.project_role(u, lab.project) is not None),
    Check("with_project_roles", lambda lab, u: _my_role(lab, u) is not None),
    Check("visible_projects", lambda lab, u: permissions.visible_projects(u).exists()),
    Check("can", lambda lab, u: permissions.can(u, Action.PROJECT_MANAGE, lab.project)),
    Check("roles_allow", lambda lab, u: permissions.roles_allow(u, Action.RUN_VM, lab.project)),
    _allowed(Action.PROJECT_READ, _project_target),
    Check(
        "remember_roles",
        _remembered(lambda lab, u: permissions.can(u, Action.PROJECT_EDIT, lab.project)),
    ),
]

#: Each kind of grant: how it's given to one user, what both sides hold anyway, and what
#: it gives a request by each predicate.
GRANTS: dict[str, tuple[Grant, Grant | None, list[Check]]] = {
    "org_admin": (
        _org(OrgRole.ORG_ADMIN),
        None,
        [
            Check("org_roles", lambda lab, u: OrgRole.ORG_ADMIN in permissions.org_roles(u)),
            Check("can", lambda lab, u: permissions.can(u, Action.ORG_MANAGE)),
            Check("roles_allow", lambda lab, u: permissions.roles_allow(u, Action.ORG_MANAGE)),
            _allowed(Action.REGISTRIES_MANAGE),
            Check("capabilities", lambda lab, u: permissions.capabilities(u)["manage_org"]),
            Check("visible_projects", lambda lab, u: permissions.visible_projects(u).exists()),
            Check("visible_teams", lambda lab, u: permissions.visible_teams(u).exists()),
            _allowed(Action.PROJECT_MEMBERS_MANAGE, _project_target),
            _allowed(Action.TEAM_MANAGE, _team_target),
        ],
    ),
    "compute_admin": (
        _org(OrgRole.COMPUTE_ADMIN),
        None,
        [
            Check("org_roles", lambda lab, u: OrgRole.COMPUTE_ADMIN in permissions.org_roles(u)),
            _allowed(Action.COMPUTE_MANAGE),
            Check("capabilities", lambda lab, u: permissions.capabilities(u)["manage_compute"]),
        ],
    ),
    "project_admin": (_project(ProjectRole.ADMIN), None, _PROJECT_ROLE),
    "project_admin_through_a_team": (
        lambda lab, user: TeamMembership.objects.create(team=lab.group, user=user),
        None,
        _PROJECT_ROLE,
    ),
    "team_member": (
        lambda lab, user: TeamMembership.objects.create(team=lab.team, user=user),
        None,
        [
            Check("can", lambda lab, u: permissions.can(u, Action.TEAM_VIEW, lab.team)),
            _allowed(Action.PROJECT_CREATE, _team_target),
            Check("visible_teams", lambda lab, u: permissions.visible_teams(u).exists()),
            Check(
                "teams_for_new_project",
                lambda lab, u: permissions.teams_for_new_project(u).exists(),
            ),
            Check("capabilities", lambda lab, u: permissions.capabilities(u)["create_projects"]),
        ],
    ),
    "registrar": (
        lambda lab, user: RegistryRole.objects.create(
            registry=lab.registry, user=user, role=RegistryRole.Role.REGISTRAR
        ),
        None,
        [
            _allowed(Action.REGISTRY_CURATE, _registry_target),
            _allowed(Action.LISTS_CURATE, _registry_target),
            Check(
                "can_curate_list",
                lambda lab, u: permissions.can_curate_list(u, lab.registry, LIST),
            ),
        ],
    ),
    "list_curator": (
        lambda lab, user: RegistryRole.objects.create(
            registry=lab.registry, user=user, role=RegistryRole.Role.LIST_CURATOR, list_type=LIST
        ),
        None,
        [
            Check(
                "can",
                lambda lab, u: permissions.can(u, Action.LISTS_CURATE, lab.registry),
            ),
            Check(
                "can_curate_list",
                lambda lab, u: permissions.can_curate_list(u, lab.registry, LIST),
            ),
        ],
    ),
    "lab_role": (
        _lab_role,
        # A lab role counts only while its holder can read the project.
        _project(ProjectRole.VIEWER),
        [
            Check("lab_roles", lambda lab, u: QA in permissions.lab_roles(u, lab.project)),
            Check(
                "remember_roles",
                _remembered(lambda lab, u: QA in permissions.lab_roles(u, lab.project)),
            ),
        ],
    ),
}

CASES = [
    pytest.param(name, check, side, id=f"{name}-{check.id}-{side}")
    for name, (_, _, checks) in GRANTS.items()
    for check in checks
    for side in ("person", "agent", "both")
]


@pytest.mark.parametrize(("grant", "check", "side"), CASES)
def test_a_grant_only_one_side_holds_gives_a_delegated_request_nothing(
    lab: Lab, grant: str, check: Check, side: str
) -> None:
    give, base, _ = GRANTS[grant]
    for each, user in (("person", lab.person), ("agent", lab.agent)):
        if base is not None:
            base(lab, user)
        if side in (each, "both"):
            give(lab, user)
    assert check.gives(lab, lab.request()) is (side == "both")


#: Public functions of the service that take a user but decide nothing about a request.
NOT_PREDICATES = {"limit_to", "token_limits", "principals", "forget"}


def test_every_public_predicate_is_checked_with_one_side_only() -> None:
    takes_a_user = {
        name
        for name, function in inspect.getmembers(permissions, inspect.isfunction)
        if function.__module__ == permissions.__name__
        and not name.startswith("_")
        and next(iter(inspect.signature(function).parameters), None) == "user"
    }
    checked = {check.predicate for _, _, checks in GRANTS.values() for check in checks}
    assert takes_a_user - NOT_PREDICATES == checked


def test_a_registrar_and_a_lists_curator_together_curate_only_that_list(lab: Lab) -> None:
    # Each could curate the list alone, so a token from the two may curate it, and only it:
    # the person's registrarship doesn't stretch the agent's single list.
    RegistryRole.objects.create(
        registry=lab.registry, user=lab.person, role=RegistryRole.Role.REGISTRAR
    )
    RegistryRole.objects.create(
        registry=lab.registry, user=lab.agent, role=RegistryRole.Role.LIST_CURATOR, list_type=LIST
    )
    request = lab.request()
    assert permissions.can(request, Action.LISTS_CURATE, lab.registry)
    assert permissions.can_curate_list(request, lab.registry, LIST)
    assert not permissions.can_curate_list(request, lab.registry, OTHER_LIST)
    assert not permissions.can(request, Action.REGISTRY_CURATE, lab.registry)


def test_two_project_roles_give_the_lower(lab: Lab) -> None:
    _project(ProjectRole.ADMIN)(lab, lab.person)
    _project(ProjectRole.VIEWER)(lab, lab.agent)
    request = lab.request()
    assert permissions.project_role(request, lab.project) == ProjectRole.VIEWER
    assert _my_role(lab, request) == ProjectRole.VIEWER
    assert permissions.can(request, Action.PROJECT_READ, lab.project)
    assert not permissions.can(request, Action.PROJECT_EDIT, lab.project)


def test_an_org_admin_with_an_agent_in_the_project_sees_it_but_cant_manage_its_members(
    lab: Lab,
) -> None:
    _org(OrgRole.ORG_ADMIN)(lab, lab.person)
    _project(ProjectRole.VIEWER)(lab, lab.agent)
    request = lab.request()
    assert permissions.can(request, Action.PROJECT_SEE, lab.project)
    assert not permissions.can(request, Action.PROJECT_MEMBERS_MANAGE, lab.project)
    # The person holds no role in the project, so the request reads none of its data.
    assert _my_role(lab, request) is None
    assert list(permissions.visible_projects(request)) == [lab.project]

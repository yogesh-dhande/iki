"""The permission service: every authorization decision in Iki is made here.

Views, the API, MCP tools and the event stream ask :func:`can` or
:func:`require` before doing anything (ADR 0003). The rules:

- Inactive and anonymous users can do nothing.
- **Org roles** cover the organization: Org Admins manage people, teams and
  settings; Compute Admins manage compute; Auditors read the audit log.
- **Project roles** cover a project's data. Someone's role is the highest of
  their direct grant and their teams' grants. Each role includes the ones
  below it: Viewer reads, Editor edits and runs on the backend and client
  tiers, Runner also runs on the container and VM tiers, Admin manages the
  project and its members.
- **Org Admins see every project's name and members, but not its data.** They
  can manage any project's members, including adding themselves, which is
  how an orphaned project is recovered (ADR 0024).
- **Any member of a team can create a project in it** and becomes its Admin.
- Archived projects are read-only, except for restoring them and managing
  members. Archived teams get no new projects.

Decisions are cached on the user object, which lives for one request. Call
:func:`forget` after changing someone's roles within the same request.

**OAuth tokens only narrow** what their user may do (ADR 0028). A request
made with a token carries a :class:`TokenLimits`: each action needs the
token's scope for it, and a token limited to some projects can't touch the
others.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Exists, Max, OuterRef, Q, QuerySet, Subquery

from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Team,
)


class Action(StrEnum):
    # The organization (no target)
    ORG_MANAGE = "org.manage"
    """Org settings, people's org roles, deactivating people."""
    TEAM_CREATE = "team.create"
    COMPUTE_MANAGE = "compute.manage"
    AUDIT_READ = "audit.read"
    ORG_WEBHOOKS_MANAGE = "org.webhooks.manage"
    """The organization's webhooks, and turning off any project's."""

    # A team
    TEAM_VIEW = "team.view"
    """See the team, its members, and which of its projects you can see."""
    TEAM_MANAGE = "team.manage"
    """Rename, archive, restore, add and remove members."""
    PROJECT_CREATE = "project.create"

    # A project
    PROJECT_SEE = "project.see"
    """See that the project exists: name, team, description, members."""
    PROJECT_READ = "project.read"
    """Read the project's data: entities, runs, views."""
    PROJECT_EDIT = "project.edit"
    RUN_CLIENT = "run.client"
    RUN_BACKEND = "run.backend"
    RUN_CONTAINER = "run.container"
    RUN_VM = "run.vm"
    PROJECT_MANAGE = "project.manage"
    """Rename, describe, archive, restore."""
    PROJECT_MEMBERS_MANAGE = "project.members.manage"
    PROJECT_WEBHOOKS_MANAGE = "project.webhooks.manage"
    """The project's webhooks, which send its changes to other systems."""


ORG_ACTIONS = frozenset(
    {
        Action.ORG_MANAGE,
        Action.TEAM_CREATE,
        Action.COMPUTE_MANAGE,
        Action.AUDIT_READ,
        Action.ORG_WEBHOOKS_MANAGE,
    }
)
TEAM_ACTIONS = frozenset({Action.TEAM_VIEW, Action.TEAM_MANAGE, Action.PROJECT_CREATE})
PROJECT_ACTIONS = frozenset(Action) - ORG_ACTIONS - TEAM_ACTIONS

#: The OAuth scope each action needs, when the request carries a token.
ACTION_SCOPES: dict[Action, str] = {
    Action.TEAM_VIEW: "read",
    Action.PROJECT_SEE: "read",
    Action.PROJECT_READ: "read",
    Action.AUDIT_READ: "read",
    Action.PROJECT_CREATE: "write",
    Action.PROJECT_EDIT: "write",
    Action.PROJECT_MANAGE: "write",
    Action.PROJECT_MEMBERS_MANAGE: "write",
    Action.RUN_CLIENT: "run",
    Action.RUN_BACKEND: "run",
    Action.RUN_CONTAINER: "run",
    Action.RUN_VM: "run",
    Action.ORG_MANAGE: "admin",
    Action.TEAM_CREATE: "admin",
    Action.TEAM_MANAGE: "admin",
    Action.COMPUTE_MANAGE: "admin",
    # Webhooks send data out of Iki: a scope of their own, as GitHub's admin:*_hook.
    Action.ORG_WEBHOOKS_MANAGE: "webhooks",
    Action.PROJECT_WEBHOOKS_MANAGE: "webhooks",
}


@dataclass(frozen=True)
class TokenLimits:
    """What an OAuth token allows, on top of its user's roles."""

    scopes: frozenset[str]
    #: The projects the token may touch; None means every project.
    projects: frozenset[UUID] | None = None

    def allows(self, action: Action, target: "Team | Project | None") -> bool:
        if ACTION_SCOPES[action] not in self.scopes:
            return False
        if self.projects is None:
            return True
        if isinstance(target, Project):
            return target.pk in self.projects
        # A token limited to some projects can't create new ones.
        return action is not Action.PROJECT_CREATE


def limit_to(user: Any, limits: TokenLimits | None) -> None:  # noqa: ANN401
    """Applies a token's limits to every decision about this user in this request."""
    user._iki_token_limits = limits


def token_limits(user: Any) -> TokenLimits | None:  # noqa: ANN401
    return getattr(user, "_iki_token_limits", None)


#: The org role each organization-wide action needs.
_ORG_ACTION_ROLE = {
    Action.ORG_MANAGE: OrgRole.ORG_ADMIN,
    Action.TEAM_CREATE: OrgRole.ORG_ADMIN,
    Action.COMPUTE_MANAGE: OrgRole.COMPUTE_ADMIN,
    Action.AUDIT_READ: OrgRole.AUDITOR,
    Action.ORG_WEBHOOKS_MANAGE: OrgRole.ORG_ADMIN,
}

#: The lowest project role each project action needs.
_PROJECT_ACTION_ROLE = {
    Action.PROJECT_SEE: ProjectRole.VIEWER,
    Action.PROJECT_READ: ProjectRole.VIEWER,
    Action.PROJECT_EDIT: ProjectRole.EDITOR,
    Action.RUN_CLIENT: ProjectRole.EDITOR,
    Action.RUN_BACKEND: ProjectRole.EDITOR,
    Action.RUN_CONTAINER: ProjectRole.RUNNER,
    Action.RUN_VM: ProjectRole.RUNNER,
    Action.PROJECT_MANAGE: ProjectRole.ADMIN,
    Action.PROJECT_MEMBERS_MANAGE: ProjectRole.ADMIN,
    Action.PROJECT_WEBHOOKS_MANAGE: ProjectRole.ADMIN,
}

#: Project actions an Org Admin may take without a project role.
_ORG_ADMIN_PROJECT_ACTIONS = frozenset({Action.PROJECT_SEE, Action.PROJECT_MEMBERS_MANAGE})

#: Project actions still allowed while the project is archived.
_ARCHIVED_PROJECT_ACTIONS = frozenset(
    {
        Action.PROJECT_SEE,
        Action.PROJECT_READ,
        Action.PROJECT_MANAGE,
        Action.PROJECT_MEMBERS_MANAGE,
    }
)


def _active(user: Any) -> bool:  # noqa: ANN401 - User or AnonymousUser
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


def org_roles(user: Any) -> frozenset[OrgRole]:  # noqa: ANN401
    """The org roles an active user holds."""
    if not _active(user):
        return frozenset()
    cached: frozenset[OrgRole] | None = getattr(user, "_iki_org_roles", None)
    if cached is None:
        roles = OrgRoleAssignment.objects.filter(user=user).values_list("role", flat=True)
        cached = frozenset(OrgRole(r) for r in roles)
        user._iki_org_roles = cached
    return cached


def _grants_for(user: Any) -> Q:  # noqa: ANN401
    return Q(user=user) | Q(team__memberships__user=user)


def project_role(user: Any, project: Project) -> ProjectRole | None:  # noqa: ANN401
    """The user's role in the project: the highest of their own and their teams' grants."""
    if not _active(user):
        return None
    cache: dict[Any, ProjectRole | None] = user.__dict__.setdefault("_iki_project_roles", {})
    if project.pk not in cache:
        best = (
            ProjectMembership.objects.filter(project=project)
            .filter(_grants_for(user))
            .aggregate(best=Max("role"))["best"]
        )
        cache[project.pk] = ProjectRole(best) if best is not None else None
    return cache[project.pk]


def is_team_member(user: Any, team: Team) -> bool:  # noqa: ANN401
    if not _active(user):
        return False
    cache: dict[Any, bool] = user.__dict__.setdefault("_iki_team_memberships", {})
    if team.pk not in cache:
        cache[team.pk] = team.memberships.filter(user=user).exists()
    return cache[team.pk]


def forget(user: Any) -> None:  # noqa: ANN401
    """Drops cached decisions after the user's roles changed in this request."""
    for attr in ("_iki_org_roles", "_iki_project_roles", "_iki_team_memberships"):
        user.__dict__.pop(attr, None)


def can(user: Any, action: Action, target: Team | Project | None = None) -> bool:  # noqa: ANN401
    """Whether the user may take the action, on the target if the action needs one."""
    limits = token_limits(user)
    if limits is not None and not limits.allows(action, target):
        return False
    return _roles_allow(user, action, target)


def _roles_allow(user: Any, action: Action, target: Team | Project | None) -> bool:  # noqa: ANN401
    if action in ORG_ACTIONS:
        if target is not None:
            raise TypeError(f"{action} takes no target")
        return _ORG_ACTION_ROLE[action] in org_roles(user)

    if action in TEAM_ACTIONS:
        if not isinstance(target, Team):
            raise TypeError(f"{action} needs a Team")
        org_admin = OrgRole.ORG_ADMIN in org_roles(user)
        if action is Action.TEAM_MANAGE:
            return org_admin
        if action is Action.TEAM_VIEW:
            return org_admin or is_team_member(user, target)
        # PROJECT_CREATE
        return not target.is_archived and is_team_member(user, target)

    if not isinstance(target, Project):
        raise TypeError(f"{action} needs a Project")
    if target.is_archived and action not in _ARCHIVED_PROJECT_ACTIONS:
        return False
    role = project_role(user, target)
    if role is not None and role >= _PROJECT_ACTION_ROLE[action]:
        return True
    return action in _ORG_ADMIN_PROJECT_ACTIONS and OrgRole.ORG_ADMIN in org_roles(user)


def require(user: Any, action: Action, target: Team | Project | None = None) -> None:  # noqa: ANN401
    """Raises ``PermissionDenied`` unless :func:`can` allows the action."""
    if not can(user, action, target):
        raise PermissionDenied(action)


def with_project_roles(user: Any, projects: QuerySet[Project]) -> QuerySet[Project]:  # noqa: ANN401
    """Annotates each project with ``my_role``: the user's role, or None."""
    best = (
        ProjectMembership.objects.filter(project=OuterRef("pk"))
        .filter(_grants_for(user))
        .order_by("-role")
        .values("role")[:1]
    )
    return projects.annotate(my_role=Subquery(best))


def visible_projects(user: Any) -> QuerySet[Project]:  # noqa: ANN401
    """Projects the user can see (:attr:`Action.PROJECT_SEE`), each with ``my_role``."""
    if not _active(user):
        return Project.objects.none()
    projects = with_project_roles(user, Project.objects.select_related("team"))
    limits = token_limits(user)
    if limits is not None:
        if "read" not in limits.scopes:
            return Project.objects.none()
        if limits.projects is not None:
            projects = projects.filter(pk__in=limits.projects)
    if OrgRole.ORG_ADMIN in org_roles(user):
        return projects
    member = ProjectMembership.objects.filter(project=OuterRef("pk")).filter(_grants_for(user))
    return projects.filter(Exists(member))


def visible_teams(user: Any) -> QuerySet[Team]:  # noqa: ANN401
    """Teams the user can see (:attr:`Action.TEAM_VIEW`)."""
    if not _active(user):
        return Team.objects.none()
    limits = token_limits(user)
    if limits is not None and "read" not in limits.scopes:
        return Team.objects.none()
    if OrgRole.ORG_ADMIN in org_roles(user):
        return Team.objects.all()
    return Team.objects.filter(memberships__user=user)


def teams_for_new_project(user: Any) -> QuerySet[Team]:  # noqa: ANN401
    """Teams the user may create a project in (:attr:`Action.PROJECT_CREATE`)."""
    if not _active(user):
        return Team.objects.none()
    limits = token_limits(user)
    if limits is not None and not limits.allows(Action.PROJECT_CREATE, None):
        return Team.objects.none()
    return Team.objects.filter(memberships__user=user, archived_at__isnull=True)

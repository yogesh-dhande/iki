"""The permission service: every authorization decision in Iki is made here.

Views, the API, MCP tools and the event stream ask :func:`can` or
:func:`require` before doing anything (ADR 0003). The rules:

- Inactive and anonymous users can do nothing.
- **Org roles** cover the organization: Org Admins manage people, teams and
  settings; Compute Admins manage compute; Auditors read the audit log.
- **Project roles** cover a project's data. Someone's role is the highest of
  their direct grant and their teams' grants. Each role includes the ones
  below it: Viewer reads, Editor edits and runs on the backend and self-hosted
  tiers, Runner also runs on the container and VM tiers, Admin manages the
  project and its members.
- **Org Admins see every project's name and members, but not its data.** They
  can manage any project's members, including adding themselves, which is
  how an orphaned project is recovered (ADR 0024).
- **Any member of a team can create a project in it** and becomes its Admin.
- Archived projects are read-only, except for restoring them and managing
  members. Archived teams get no new projects.
- **Registries** (ADR 0115): Org Admins add and change them, and appoint each
  one's registrars. A registrar curates their registry: sees its entries
  (never projects' records of them), changes their facts, and supersedes
  mistaken ones. Being an Org Admin doesn't make someone a registrar.
- **Lab lists** (ADR 0270): a registry's registrars curate its lists, and so
  does each list's curators, whom Org Admins appoint a list at a time. Every
  project in the registry picks from them.
- **Lab roles** (ADR 0116), such as QA, are declared by plugins and given by a
  project's Admins with a membership, beside its project role: someone holds
  the lab roles of their own membership and their teams'. A lab role grants
  nothing by itself. What needs one (a workflow step's approval, a record's
  state, ADR 0125) asks :func:`lab_roles`, and counts it only while the person can
  read the project. Being an Admin or an Org Admin holds none.

Decisions are cached on the user object, which lives for one request. Call
:func:`forget` after changing someone's roles within the same request.

**OAuth tokens only narrow** what their user may do (ADR 0028). A request
made with a token carries a :class:`TokenLimits`: each action needs the
token's scope for it, and a token limited to some projects can't touch the
others. A token the person gave only some projects takes no action beyond
them: none on the organization, its teams or its registries (ADR 0152).

**A delegated token does only what both may** (ADR 0073): a token an agent got by
acting for its person carries the agent's service account too. Each grant is looked up
for one user at a time, and every public predicate combines the two with :func:`all_of`,
so a grant only one of them holds gives the request nothing.
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, overload
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import (
    Case,
    Exists,
    Max,
    Model,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Least

from iki_server.org.models import (
    LabRoleGrant,
    ListCurator,
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Registrar,
    Registry,
    Team,
)


class Action(StrEnum):
    # The organization (no target)
    ORG_MANAGE = "org.manage"
    """Org settings, people's org roles, deactivating people."""
    TEAM_CREATE = "team.create"
    COMPUTE_MANAGE = "compute.manage"
    QUARANTINE_MANAGE = "quarantine.manage"
    """Review held file scans and release a false positive (ADR 0217)."""
    AUDIT_READ = "audit.read"
    COSTS_READ = "costs.read"
    """What runs cost across the organization, by project, team, template and action."""
    ORG_WEBHOOKS_MANAGE = "org.webhooks.manage"
    """The organization's webhooks, and turning off any project's."""
    REGISTRIES_MANAGE = "registries.manage"
    """Add, rename and describe registries, choose which teams use each, appoint their
    registrars, and move a project with no entities to another registry (ADR 0115)."""
    OUTSIDE_APIS_MANAGE = "outside_apis.manage"
    """Approve outside APIs for projects, and revoke approvals (ADR 0130): where runs may send
    the lab's data."""

    # A registry
    REGISTRY_CURATE = "registry.curate"
    """See every entry in the registry, change their facts, and supersede them (ADR 0115)."""
    LISTS_CURATE = "lists.curate"
    """Curate some of the registry's lab lists (ADR 0270): its registrars curate them all,
    and each list's curators that one. :func:`can_curate_list` says which."""

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
    APP_EDIT = "app.edit"
    """Build and change apps, saved views and app drafts."""
    RUN_EDIT_APP = "run.edit_app"
    """Change an app or view as a declared effect of running an installed action."""
    DASHBOARD_MANAGE = "dashboard.manage"
    DASHBOARD_CHOICES = "dashboard.choices"
    PROJECT_SET_STATE = "project.set_state"
    """Set one of a record's state fields (ADR 0125): with one of the state's lab roles,
    which this never stands in for."""
    RUN_SELF_HOSTED = "run.self_hosted"
    RUN_BACKEND = "run.backend"
    RUN_CONTAINER = "run.container"
    RUN_VM = "run.vm"
    PROJECT_MANAGE = "project.manage"
    """Rename, describe, archive, restore."""
    PROJECT_MEMBERS_MANAGE = "project.members.manage"
    PROJECT_WEBHOOKS_MANAGE = "project.webhooks.manage"
    """The project's webhooks, which send its changes to other systems."""
    PROJECT_TRIGGERS_MANAGE = "project.triggers.manage"
    """Turn plugins' triggers on and off in the project: their runs are launched as you."""
    PROJECT_EXPORT = "project.export"
    """Export the project's entities, with their history, in a bundle for another deployment
    (ADR 0096)."""
    PROJECT_IMPORT = "project.import"
    """Import a bundle from another deployment into the project (ADR 0096). It can add
    people, as placeholders, so it needs an Org Admin who is also the project's Admin."""


ORG_ACTIONS = frozenset(
    {
        Action.ORG_MANAGE,
        Action.TEAM_CREATE,
        Action.COMPUTE_MANAGE,
        Action.QUARANTINE_MANAGE,
        Action.AUDIT_READ,
        Action.COSTS_READ,
        Action.ORG_WEBHOOKS_MANAGE,
        Action.REGISTRIES_MANAGE,
        Action.OUTSIDE_APIS_MANAGE,
    }
)
TEAM_ACTIONS = frozenset({Action.TEAM_VIEW, Action.TEAM_MANAGE, Action.PROJECT_CREATE})
REGISTRY_ACTIONS = frozenset({Action.REGISTRY_CURATE, Action.LISTS_CURATE})
PROJECT_ACTIONS = frozenset(Action) - ORG_ACTIONS - TEAM_ACTIONS - REGISTRY_ACTIONS

#: The OAuth scope each action needs, when the request carries a token.
ACTION_SCOPES: dict[Action, str] = {
    Action.TEAM_VIEW: "read",
    Action.PROJECT_SEE: "read",
    Action.PROJECT_READ: "read",
    Action.AUDIT_READ: "read",
    Action.PROJECT_CREATE: "write",
    Action.PROJECT_EDIT: "write",
    Action.APP_EDIT: "apps",
    Action.RUN_EDIT_APP: "run",
    Action.DASHBOARD_MANAGE: "apps",
    Action.DASHBOARD_CHOICES: "read",
    Action.PROJECT_SET_STATE: "write",
    Action.PROJECT_MANAGE: "write",
    Action.PROJECT_MEMBERS_MANAGE: "write",
    Action.RUN_SELF_HOSTED: "run",
    Action.RUN_BACKEND: "run",
    Action.RUN_CONTAINER: "run",
    Action.RUN_VM: "run",
    Action.ORG_MANAGE: "admin",
    Action.TEAM_CREATE: "admin",
    Action.TEAM_MANAGE: "admin",
    Action.COMPUTE_MANAGE: "admin",
    Action.QUARANTINE_MANAGE: "admin",
    Action.COSTS_READ: "read",
    # Webhooks send data out of Iki: a scope of their own, as GitHub's admin:*_hook.
    Action.ORG_WEBHOOKS_MANAGE: "webhooks",
    Action.PROJECT_WEBHOOKS_MANAGE: "webhooks",
    Action.PROJECT_TRIGGERS_MANAGE: "write",
    # A bundle takes a project's data out of the deployment: tokens need the admin scope.
    Action.PROJECT_EXPORT: "admin",
    Action.PROJECT_IMPORT: "admin",
    Action.REGISTRIES_MANAGE: "admin",
    # A registrar's changes are every holder's: tokens need the admin scope.
    Action.REGISTRY_CURATE: "admin",
    # ...as a list curator's are every project's in the registry (ADR 0270).
    Action.LISTS_CURATE: "admin",
    # Approving is web-only (ADR 0130); a token with the admin scope may revoke.
    Action.OUTSIDE_APIS_MANAGE: "admin",
}


@dataclass(frozen=True)
class TokenLimits:
    """What an OAuth token allows, on top of its user's roles."""

    scopes: frozenset[str]
    #: The projects the token may touch; None means every project.
    projects: frozenset[UUID] | None = None
    #: For a token an agent got by acting for its user (ADR 0073), the agent's own
    #: service account, whose roles must allow each action too.
    alongside: Any = None
    #: The person chose only some projects for it (ADR 0152): it acts in those and nowhere
    #: else, so it takes no action on the organization, its teams or its registries.
    only_projects: bool = False

    def allows(self, action: Action, target: "Team | Project | Registry | None") -> bool:
        if ACTION_SCOPES[action] not in self.scopes:
            return False
        if isinstance(target, Project):
            if self.only_projects and action in _ALSO_ORG_ADMIN:
                # It adds people to the organization, as an Org Admin.
                return False
            return self.projects is None or target.pk in self.projects
        if self.only_projects:
            return False
        if self.projects is None:
            return True
        # A token held to the projects its agent's service account may go to can't create
        # new ones, nor curate a registry, whose entries are every project's.
        return action is not Action.PROJECT_CREATE and action not in REGISTRY_ACTIONS

    def stored(self) -> dict[str, Any]:
        """As JSON, for work a task does later for the request (ADR 0152)."""
        return {
            "scopes": sorted(self.scopes),
            "projects": None if self.projects is None else sorted(map(str, self.projects)),
            "alongside": None if self.alongside is None else str(self.alongside.pk),
            "only_projects": self.only_projects,
        }

    @classmethod
    def restored(cls, data: dict[str, Any]) -> "TokenLimits | None":
        """The limits :meth:`stored` kept; None when the agent's service account they held
        to is gone or deactivated, so nothing may be done under them."""
        from iki_server.accounts.models import User

        alongside = None
        if data["alongside"] is not None:
            alongside = User.objects.filter(pk=data["alongside"], is_active=True).first()
            if alongside is None:
                return None
        projects = data["projects"]
        return cls(
            scopes=frozenset(data["scopes"]),
            projects=None if projects is None else frozenset(UUID(p) for p in projects),
            alongside=alongside,
            only_projects=data["only_projects"],
        )

    def beyond_projects(self, action: Action) -> bool:
        """Whether it's refused `action` for acting beyond the projects it was given,
        rather than for its scopes."""
        return self.only_projects and (action not in PROJECT_ACTIONS or action in _ALSO_ORG_ADMIN)


def limit_to(user: Any, limits: TokenLimits | None) -> None:  # noqa: ANN401
    """Applies a token's limits to every decision about this user in this request."""
    user._iki_token_limits = limits


def token_limits(user: Any) -> TokenLimits | None:  # noqa: ANN401
    return getattr(user, "_iki_token_limits", None)


#: The org roles that may take each organization-wide action: any one of them.
_ORG_ACTION_ROLES = {
    Action.ORG_MANAGE: {OrgRole.ORG_ADMIN},
    Action.TEAM_CREATE: {OrgRole.ORG_ADMIN},
    Action.COMPUTE_MANAGE: {OrgRole.COMPUTE_ADMIN},
    Action.QUARANTINE_MANAGE: {OrgRole.QUARANTINE_ADMIN},
    Action.AUDIT_READ: {OrgRole.AUDITOR},
    # Who answers for what's spent: those who run compute, audit it, and run the org.
    Action.COSTS_READ: {OrgRole.COMPUTE_ADMIN, OrgRole.AUDITOR, OrgRole.ORG_ADMIN},
    Action.ORG_WEBHOOKS_MANAGE: {OrgRole.ORG_ADMIN},
    Action.REGISTRIES_MANAGE: {OrgRole.ORG_ADMIN},
    Action.OUTSIDE_APIS_MANAGE: {OrgRole.ORG_ADMIN},
}

#: The lowest project role each project action needs.
_PROJECT_ACTION_ROLE = {
    Action.PROJECT_SEE: ProjectRole.VIEWER,
    Action.PROJECT_READ: ProjectRole.VIEWER,
    Action.PROJECT_EDIT: ProjectRole.EDITOR,
    Action.APP_EDIT: ProjectRole.EDITOR,
    Action.RUN_EDIT_APP: ProjectRole.EDITOR,
    Action.DASHBOARD_MANAGE: ProjectRole.ADMIN,
    Action.DASHBOARD_CHOICES: ProjectRole.ADMIN,
    # Any member: what decides it is the state's lab role (ADR 0125).
    Action.PROJECT_SET_STATE: ProjectRole.VIEWER,
    Action.RUN_SELF_HOSTED: ProjectRole.EDITOR,
    Action.RUN_BACKEND: ProjectRole.EDITOR,
    Action.RUN_CONTAINER: ProjectRole.RUNNER,
    Action.RUN_VM: ProjectRole.RUNNER,
    Action.PROJECT_MANAGE: ProjectRole.ADMIN,
    Action.PROJECT_MEMBERS_MANAGE: ProjectRole.ADMIN,
    Action.PROJECT_WEBHOOKS_MANAGE: ProjectRole.ADMIN,
    Action.PROJECT_TRIGGERS_MANAGE: ProjectRole.ADMIN,
    Action.PROJECT_EXPORT: ProjectRole.ADMIN,
    Action.PROJECT_IMPORT: ProjectRole.ADMIN,
}

#: Project actions that also need the Org Admin role, beside the project role.
_ALSO_ORG_ADMIN = frozenset({Action.PROJECT_IMPORT})

#: Project actions an Org Admin may take without a project role.
_ORG_ADMIN_PROJECT_ACTIONS = frozenset({Action.PROJECT_SEE, Action.PROJECT_MEMBERS_MANAGE})

#: Project actions still allowed while the project is archived.
_ARCHIVED_PROJECT_ACTIONS = frozenset(
    {
        Action.PROJECT_SEE,
        Action.PROJECT_READ,
        Action.PROJECT_MANAGE,
        Action.DASHBOARD_MANAGE,
        Action.DASHBOARD_CHOICES,
        Action.PROJECT_MEMBERS_MANAGE,
        # Exporting only reads.
        Action.PROJECT_EXPORT,
    }
)


def _active(user: Any) -> bool:  # noqa: ANN401 - User or AnonymousUser
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


# --- Whose grants a request acts with (ADR 0073) ----------------------------------


@dataclass(frozen=True)
class Principals:
    """Whose grants a request acts with: its person, and, for a token an agent got by acting
    for them (ADR 0073), the agent's own service account. The request may do only what each
    of them may, so every public predicate here asks each one with :func:`all_of`."""

    person: Any
    agent: Any = None

    def __iter__(self) -> Iterator[Any]:
        yield self.person
        if self.agent is not None:
            yield self.agent


def principals(user: Any) -> Principals:  # noqa: ANN401
    """Whose grants a request made as `user` acts with."""
    limits = token_limits(user)
    return Principals(user, limits.alongside if limits is not None else None)


@overload
def all_of(who: Principals, held: Callable[[Any], bool]) -> bool: ...
@overload
def all_of[T](who: Principals, held: Callable[[Any], frozenset[T]]) -> frozenset[T]: ...
@overload
def all_of[M: Model](who: Principals, held: Callable[[Any], QuerySet[M]]) -> QuerySet[M]: ...
@overload
def all_of(who: Principals, held: Callable[[Any], ProjectRole | None]) -> ProjectRole | None: ...
def all_of(who: Principals, held: Callable[[Any], Any]) -> Any:
    """What each of the principals holds, asked of each with `held`, a lookup for one user:
    both must be true, the names both hold, the rows both may reach, the lower of two
    project roles (none if either has none). The agent isn't asked when the person's answer
    already refuses."""
    mine = held(who.person)
    if who.agent is None:
        return mine
    if isinstance(mine, bool):
        return mine and bool(held(who.agent))
    if isinstance(mine, frozenset):
        return mine & held(who.agent) if mine else mine
    if isinstance(mine, QuerySet):
        return mine.filter(pk__in=held(who.agent).values("pk"))
    if mine is None:
        return None
    if isinstance(mine, ProjectRole):
        theirs = held(who.agent)
        return None if theirs is None else min(mine, theirs)
    raise TypeError(f"all_of can't combine {type(mine).__name__}")


# --- What one user holds ------------------------------------------------------------
#
# Each lookup is written once, for one user, and cached on that user's object for the
# request. Nothing outside this section asks them directly: the public predicates below
# combine them over the request's principals.


def _held_org_roles(user: Any) -> frozenset[OrgRole]:  # noqa: ANN401
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


def _held_project_role(user: Any, project: Project) -> ProjectRole | None:  # noqa: ANN401
    """The highest of the user's own and their teams' grants in the project."""
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


def _held_team_membership(user: Any, team: Team) -> bool:  # noqa: ANN401
    if not _active(user):
        return False
    cache: dict[Any, bool] = user.__dict__.setdefault("_iki_team_memberships", {})
    if team.pk not in cache:
        cache[team.pk] = team.memberships.filter(user=user).exists()
    return cache[team.pk]


def _held_registrar(user: Any, registry: Registry) -> bool:  # noqa: ANN401
    """One of the registry's registrars (ADR 0115)."""
    if not _active(user):
        return False
    cache: dict[Any, bool] = user.__dict__.setdefault("_iki_registrars", {})
    if registry.pk not in cache:
        cache[registry.pk] = Registrar.objects.filter(registry=registry, user=user).exists()
    return cache[registry.pk]


def _held_list_curations(user: Any, registry: Registry) -> frozenset[str]:  # noqa: ANN401
    """The registry's lab lists the user was appointed to curate (ADR 0270), by type."""
    if not _active(user):
        return frozenset()
    cache: dict[Any, frozenset[str]] = user.__dict__.setdefault("_iki_list_curators", {})
    if registry.pk not in cache:
        cache[registry.pk] = frozenset(
            ListCurator.objects.filter(registry=registry, user=user).values_list(
                "list_type", flat=True
            )
        )
    return cache[registry.pk]


def _curates(user: Any, registry: Registry, list_type: str | None) -> bool:  # noqa: ANN401
    """Whether the user curates this lab list of the registry, or with None, any of its
    lists: as one of its registrars, who curate them all, or as the list's curator."""
    if _held_registrar(user, registry):
        return True
    lists = _held_list_curations(user, registry)
    return bool(lists) if list_type is None else list_type in lists


def _held_lab_roles(user: Any, project: Project) -> frozenset[str]:  # noqa: ANN401
    """The lab roles an active user holds in the project (ADR 0116), by qualified name: their
    own membership's and their teams', while they have a role there to read it with. Only
    roles an installed plugin declares count."""
    if not _active(user) or _held_project_role(user, project) is None:
        return frozenset()
    cache: dict[Any, frozenset[str]] = user.__dict__.setdefault("_iki_lab_roles", {})
    if project.pk not in cache:
        from iki_server.org import lab_roles as catalog

        held = (
            LabRoleGrant.objects.filter(membership__project=project)
            .filter(Q(membership__user=user) | Q(membership__team__memberships__user=user))
            .values_list("role", flat=True)
        )
        cache[project.pk] = frozenset(held).intersection(catalog.declared())
    return cache[project.pk]


def _held_role_allows(user: Any, action: Action, target: Team | Project | Registry | None) -> bool:  # noqa: ANN401
    if action in ORG_ACTIONS:
        if target is not None:
            raise TypeError(f"{action} takes no target")
        return bool(_ORG_ACTION_ROLES[action] & _held_org_roles(user))

    if action in REGISTRY_ACTIONS:
        if not isinstance(target, Registry):
            raise TypeError(f"{action} needs a Registry")
        if action is Action.LISTS_CURATE:
            return _curates(user, target, None)
        return _held_registrar(user, target)

    if action in TEAM_ACTIONS:
        if not isinstance(target, Team):
            raise TypeError(f"{action} needs a Team")
        org_admin = OrgRole.ORG_ADMIN in _held_org_roles(user)
        if action is Action.TEAM_MANAGE:
            return org_admin
        if action is Action.TEAM_VIEW:
            return org_admin or _held_team_membership(user, target)
        # PROJECT_CREATE
        return not target.is_archived and _held_team_membership(user, target)

    if not isinstance(target, Project):
        raise TypeError(f"{action} needs a Project")
    if target.is_archived and action not in _ARCHIVED_PROJECT_ACTIONS:
        return False
    role = _held_project_role(user, target)
    if action in _ALSO_ORG_ADMIN and OrgRole.ORG_ADMIN not in _held_org_roles(user):
        return False
    if role is not None and role >= _PROJECT_ACTION_ROLE[action]:
        return True
    return action in _ORG_ADMIN_PROJECT_ACTIONS and OrgRole.ORG_ADMIN in _held_org_roles(user)


def _role_in_each_project(user: Any) -> Subquery:  # noqa: ANN401
    """The user's role in the outer query's project, or NULL."""
    return Subquery(
        ProjectMembership.objects.filter(project=OuterRef("pk"))
        .filter(_grants_for(user))
        .order_by("-role")
        .values("role")[:1]
    )


def _held_visible_projects(user: Any) -> QuerySet[Project]:  # noqa: ANN401
    if not _active(user):
        return Project.objects.none()
    if OrgRole.ORG_ADMIN in _held_org_roles(user):
        return Project.objects.all()
    member = ProjectMembership.objects.filter(project=OuterRef("pk")).filter(_grants_for(user))
    return Project.objects.filter(Exists(member))


def _held_visible_teams(user: Any) -> QuerySet[Team]:  # noqa: ANN401
    if not _active(user):
        return Team.objects.none()
    if OrgRole.ORG_ADMIN in _held_org_roles(user):
        return Team.objects.all()
    return Team.objects.filter(memberships__user=user)


def _held_teams_for_new_project(user: Any) -> QuerySet[Team]:  # noqa: ANN401
    if not _active(user):
        return Team.objects.none()
    return Team.objects.filter(memberships__user=user, archived_at__isnull=True)


# --- What a request may do ----------------------------------------------------------
#
# Each public predicate takes the request's user, and answers for its principals, combined
# with :func:`all_of` (ADR 0073): a grant only one of them holds counts for nothing.
# ``test_principals`` checks every one of them.


def org_roles(user: Any) -> frozenset[OrgRole]:  # noqa: ANN401
    """The org roles the request holds: an active user's, and its agent's too."""
    return all_of(principals(user), _held_org_roles)


def project_role(user: Any, project: Project) -> ProjectRole | None:  # noqa: ANN401
    """The request's role in the project: the highest of the user's own and their teams'
    grants, and no higher than its agent's."""
    return all_of(principals(user), lambda u: _held_project_role(u, project))


def least_role(action: Action) -> ProjectRole:
    """The least project role that may take a project action."""
    return _PROJECT_ACTION_ROLE[action]


def members_with(project: Project, at_least: ProjectRole) -> QuerySet[Any]:
    """Active people whose role in the project is at least `at_least`: by their own grant,
    or a team's they belong to."""
    from iki_server.accounts.models import User

    grants = ProjectMembership.objects.filter(project=project, role__gte=at_least)
    return User.objects.filter(
        Q(pk__in=grants.filter(user__isnull=False).values("user"))
        | Q(team_memberships__team__in=grants.filter(team__isnull=False).values("team")),
        is_active=True,
    ).distinct()


def can_curate_list(user: Any, registry: Registry, list_type: str) -> bool:  # noqa: ANN401
    """Whether the user may curate this lab list of the registry (ADR 0270): add, relabel,
    reorder, archive and supersede its options. Under a delegated token, the person and the
    agent must each curate this list, as a registrar or as its curator."""
    limits = token_limits(user)
    if limits is not None and not limits.allows(Action.LISTS_CURATE, registry):
        return False
    return all_of(principals(user), lambda u: _curates(u, registry, list_type))


def lab_roles(user: Any, project: Project) -> frozenset[str]:  # noqa: ANN401
    """The lab roles this request can use in the project (ADRs 0116 and 0210)."""
    limits = token_limits(user)
    if limits is not None and limits.projects is not None and project.pk not in limits.projects:
        return frozenset()
    return all_of(principals(user), lambda u: _held_lab_roles(u, project))


def remember_roles(user: Any, projects: Iterable[Project]) -> None:  # noqa: ANN401
    """Looks up the request's project roles and lab roles in all these projects at once, so
    deciding what it may do in each makes no more queries: two queries for each principal
    however many projects there are, where asking project by project makes two for each."""
    ids = {p.pk for p in projects}
    for person in principals(user):
        if not _active(person) or not ids:
            continue
        roles = person.__dict__.setdefault("_iki_project_roles", {})
        unknown = ids - roles.keys()
        if unknown:
            best = dict(
                ProjectMembership.objects.filter(project_id__in=unknown)
                .filter(_grants_for(person))
                .order_by()
                .values("project_id")
                .annotate(best=Max("role"))
                .values_list("project_id", "best")
            )
            for pk in unknown:
                roles[pk] = ProjectRole(best[pk]) if pk in best else None
        held = person.__dict__.setdefault("_iki_lab_roles", {})
        unknown = {pk for pk in ids if roles[pk] is not None} - held.keys()
        if unknown:
            from iki_server.org import lab_roles as catalog

            found: dict[Any, set[str]] = {pk: set() for pk in unknown}
            for pk, role in (
                LabRoleGrant.objects.filter(membership__project_id__in=unknown)
                .filter(Q(membership__user=person) | Q(membership__team__memberships__user=person))
                .values_list("membership__project_id", "role")
            ):
                found[pk].add(role)
            declared = catalog.declared().keys()
            for pk, names in found.items():
                held[pk] = frozenset(names).intersection(declared)


def holders_of(project: Project, role: str) -> QuerySet[Any]:
    """Active people who hold a lab role in the project: by their own membership, or a
    team's they belong to (ADR 0116)."""
    from iki_server.accounts.models import User

    grants = LabRoleGrant.objects.filter(membership__project=project, role=role)
    return User.objects.filter(
        Q(pk__in=grants.filter(membership__user__isnull=False).values("membership__user"))
        | Q(
            team_memberships__team__in=grants.filter(membership__team__isnull=False).values(
                "membership__team"
            )
        ),
        is_active=True,
    ).distinct()


def forget(user: Any) -> None:  # noqa: ANN401
    """Drops cached decisions after the request's roles changed: its user's, and its
    agent's."""
    for each in principals(user):
        for attr in (
            "_iki_org_roles",
            "_iki_project_roles",
            "_iki_team_memberships",
            "_iki_registrars",
            "_iki_list_curators",
            "_iki_lab_roles",
        ):
            each.__dict__.pop(attr, None)


def can(user: Any, action: Action, target: Team | Project | Registry | None = None) -> bool:  # noqa: ANN401
    """Whether the user may take the action, on the target if the action needs one."""
    limits = token_limits(user)
    if limits is not None and not limits.allows(action, target):
        return False
    return roles_allow(user, action, target)


def roles_allow(user: Any, action: Action, target: Team | Project | Registry | None = None) -> bool:  # noqa: ANN401
    """Whether the request's roles allow the action, whatever its token's scopes and
    projects: for saying why a token was refused."""
    return all_of(principals(user), lambda u: _held_role_allows(u, action, target))


def require(user: Any, action: Action, target: Team | Project | Registry | None = None) -> None:  # noqa: ANN401
    """Raises ``PermissionDenied`` unless :func:`can` allows the action."""
    if not can(user, action, target):
        raise PermissionDenied(action)


def with_project_roles(user: Any, projects: QuerySet[Project]) -> QuerySet[Project]:  # noqa: ANN401
    """Annotates each project with ``my_role``: the request's role there, as
    :func:`project_role` gives it, or None."""
    who = principals(user)
    if who.agent is None:
        return projects.annotate(my_role=_role_in_each_project(who.person))
    # all_of's rule for two roles, in SQL: the lower, and none if either has none.
    return projects.alias(
        _person_role=_role_in_each_project(who.person),
        _agent_role=_role_in_each_project(who.agent),
    ).annotate(
        my_role=Case(
            When(Q(_person_role__isnull=True) | Q(_agent_role__isnull=True), then=Value(None)),
            default=Least("_person_role", "_agent_role"),
        )
    )


def visible_projects(user: Any) -> QuerySet[Project]:  # noqa: ANN401
    """Projects the user can see (:attr:`Action.PROJECT_SEE`), each with ``my_role``."""
    limits = token_limits(user)
    if not _active(user) or (limits is not None and "read" not in limits.scopes):
        return Project.objects.none()
    projects = all_of(principals(user), _held_visible_projects)
    if limits is not None and limits.projects is not None:
        projects = projects.filter(pk__in=limits.projects)
    return with_project_roles(user, projects.select_related("team", "registry"))


def visible_teams(user: Any) -> QuerySet[Team]:  # noqa: ANN401
    """Teams the user can see (:attr:`Action.TEAM_VIEW`)."""
    limits = token_limits(user)
    if limits is not None and ("read" not in limits.scopes or limits.only_projects):
        return Team.objects.none()
    return all_of(principals(user), _held_visible_teams)


def teams_for_new_project(user: Any) -> QuerySet[Team]:  # noqa: ANN401
    """Teams the user may create a project in (:attr:`Action.PROJECT_CREATE`)."""
    limits = token_limits(user)
    if limits is not None and not limits.allows(Action.PROJECT_CREATE, None):
        return Team.objects.none()
    return all_of(principals(user), _held_teams_for_new_project)


#: ``/me``'s ``can`` that are an organization-wide action, which org roles grant.
_CAPABILITY_ACTIONS = {
    "manage_org": Action.ORG_MANAGE,
    "read_audit": Action.AUDIT_READ,
    "manage_webhooks": Action.ORG_WEBHOOKS_MANAGE,
    "read_costs": Action.COSTS_READ,
    "manage_compute": Action.COMPUTE_MANAGE,
    "manage_quarantine": Action.QUARANTINE_MANAGE,
}


def capabilities(user: Any) -> dict[str, bool]:  # noqa: ANN401
    """What the person may do across the organization: ``/me``'s ``can``, and what the
    header's navigation offers them (:mod:`iki_server.web.navigation`)."""
    can_do = {name: can(user, action) for name, action in _CAPABILITY_ACTIONS.items()}
    return {**can_do, "create_projects": teams_for_new_project(user).exists()}


def roles_granting(capability: str) -> frozenset[OrgRole]:
    """The org roles that give a person this one of :func:`capabilities` (none for one
    that isn't an org role's, such as ``create_projects``)."""
    action = _CAPABILITY_ACTIONS.get(capability)
    return frozenset(_ORG_ACTION_ROLES[action]) if action is not None else frozenset()

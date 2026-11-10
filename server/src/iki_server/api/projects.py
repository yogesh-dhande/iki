"""Projects and who has access to them."""

from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Count, QuerySet
from django.http import HttpRequest
from ninja import Field, Query, Router, Status

from iki_server.api.common import caller, person, project, team
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import (
    Page,
    ProjectIn,
    ProjectMemberOut,
    ProjectOut,
    ProjectPatch,
    ProjectRoleOut,
    RoleIn,
    TeamRef,
    parse_role,
)
from iki_server.org import permissions, services
from iki_server.org.models import (
    PROJECT_ROLE_DESCRIPTIONS,
    Project,
    ProjectMembership,
    ProjectRole,
    Team,
)
from iki_server.org.permissions import Action

router = Router(tags=["projects"])


class ProjectFilters(PageParams):
    team: UUID | None = None
    slug: str | None = Field(None, description="The project's address, as in its page's URL.")
    archived: bool | None = None
    member: bool | None = Field(
        None, description="true: only projects you have a role in. Org Admins see all names."
    )


def _reload(user: Any, found: Project) -> Project:  # noqa: ANN401
    permissions.forget(user)
    return project(user, found.pk)


@router.get(
    "/project-roles", response=list[ProjectRoleOut], summary="The project roles, lowest first"
)
def project_roles(request: HttpRequest) -> list[dict[str, str]]:
    """Each role includes everything the roles before it can do."""
    caller(request)
    return [
        {
            "name": role.name.lower(),
            "label": role.label,
            "description": PROJECT_ROLE_DESCRIPTIONS[role],
        }
        for role in ProjectRole
    ]


@router.get("/projects", response=Page[ProjectOut], summary="Projects you can see")
def list_projects(request: HttpRequest, filters: Query[ProjectFilters]) -> dict[str, Any]:
    projects: QuerySet[Project] = permissions.visible_projects(caller(request))
    if filters.team:
        projects = projects.filter(team_id=filters.team)
    if filters.slug:
        projects = projects.filter(slug=filters.slug)
    if filters.archived is not None:
        projects = projects.filter(archived_at__isnull=not filters.archived)
    if filters.member is not None:
        projects = projects.filter(my_role__isnull=not filters.member)  # type: ignore[misc]
    return page(projects, filters)


@router.post("/projects", response={201: ProjectOut}, summary="Create a project")
def create_project(request: HttpRequest, payload: ProjectIn) -> Status[Project]:
    """Any member of the team can create a project in it, and becomes its Admin."""
    me = caller(request)
    owner = team(me, payload.team, Action.PROJECT_CREATE)
    created = services.create_project(owner, payload.name.strip(), payload.description, by=me)
    return Status(201, _reload(me, created))


@router.get("/projects/{project_id}", response=ProjectOut, summary="A project")
def get_project(request: HttpRequest, project_id: UUID) -> Project:
    return project(caller(request), project_id)


@router.patch(
    "/projects/{project_id}", response=ProjectOut, summary="Rename, describe, archive or restore"
)
def patch_project(request: HttpRequest, project_id: UUID, payload: ProjectPatch) -> Project:
    """Archived projects are read-only: restore one (``archived: false``) to change it,
    in the same request if you like."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_MANAGE)
    edits = payload.name is not None or payload.description is not None
    with transaction.atomic():
        if payload.archived is False and found.is_archived:
            services.set_project_archived(found, False, by=me)
        if edits:
            if found.is_archived:
                raise services.Refused("Restore the project before changing it.")
            services.update_project(
                found,
                payload.name.strip() if payload.name is not None else found.name,
                payload.description if payload.description is not None else found.description,
                by=me,
            )
        if payload.archived is True and not found.is_archived:
            services.set_project_archived(found, True, by=me)
    return _reload(me, found)


def _memberships(found: Project) -> QuerySet[ProjectMembership]:
    return found.memberships.select_related("user", "team").annotate(
        team_size=Count("team__memberships")
    )


@router.get(
    "/projects/{project_id}/members", response=Page[ProjectMemberOut], summary="Who has access"
)
def list_members(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    """Each grant goes to one person (or service account) or to a whole team."""
    return page(_memberships(project(caller(request), project_id)), params)


@router.put(
    "/projects/{project_id}/members/users/{user_id}",
    response=ProjectMemberOut,
    summary="Give someone a role",
)
def grant_user(
    request: HttpRequest, project_id: UUID, user_id: UUID, payload: RoleIn
) -> ProjectMembership:
    """Replaces their direct grant, if any. Their teams' grants still apply."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_MEMBERS_MANAGE)
    membership = services.grant_project_role(
        found, parse_role(payload.role), user=person(user_id), by=me
    )
    return _memberships(found).get(pk=membership.pk)


@router.put(
    "/projects/{project_id}/members/teams/{team_id}",
    response=ProjectMemberOut,
    summary="Give a whole team a role",
)
def grant_team(
    request: HttpRequest, project_id: UUID, team_id: UUID, payload: RoleIn
) -> ProjectMembership:
    """Everyone in the team gets the role, including people who join it later."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_MEMBERS_MANAGE)
    grantee = _team_for_grant(team_id)
    membership = services.grant_project_role(found, parse_role(payload.role), team=grantee, by=me)
    return _memberships(found).get(pk=membership.pk)


@router.get(
    "/projects/{project_id}/grantable-teams",
    response=Page[TeamRef],
    summary="Teams you could give access",
)
def grantable_teams(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    """Any active team without a grant yet, whether or not you belong to it."""
    found = project(caller(request), project_id, Action.PROJECT_MEMBERS_MANAGE)
    granted = found.memberships.filter(team__isnull=False).values("team")
    return page(Team.objects.filter(archived_at__isnull=True).exclude(pk__in=granted), params)


def _team_for_grant(team_id: UUID) -> Team:
    # Like the members page: any team can be granted access, whether or not
    # the caller belongs to it, except archived ones.
    grantee = Team.objects.filter(pk=team_id).first()
    if grantee is None:
        raise Problem(404, "No such team.")
    if grantee.is_archived:
        raise services.Refused(f"{grantee.name} is archived.")
    return grantee


@router.delete(
    "/projects/{project_id}/members/{membership_id}",
    response={204: None},
    summary="Remove a grant",
)
def remove_member(request: HttpRequest, project_id: UUID, membership_id: UUID) -> Status[None]:
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_MEMBERS_MANAGE)
    membership = _memberships(found).filter(pk=membership_id).first()
    if membership is None:
        raise Problem(404, "No such grant in this project.")
    services.remove_project_membership(membership, by=me)
    return Status(204, None)

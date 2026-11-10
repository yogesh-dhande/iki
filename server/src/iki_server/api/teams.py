"""Teams and their members."""

from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpRequest
from ninja import Field, Query, Router, Status

from iki_server.api.common import caller, person, require, team
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, TeamIn, TeamMemberOut, TeamOut, TeamPatch
from iki_server.org import permissions, services
from iki_server.org.models import Team
from iki_server.org.permissions import Action

router = Router(tags=["teams"])


class TeamFilters(PageParams):
    archived: bool | None = None
    can_create_projects: bool | None = Field(
        None, description="true: only the teams you may create projects in."
    )


@router.get("/teams", response=Page[TeamOut], summary="Teams you can see")
def list_teams(request: HttpRequest, filters: Query[TeamFilters]) -> dict[str, Any]:
    """Org Admins see every team; others see the teams they belong to."""
    teams = permissions.visible_teams(caller(request)).prefetch_related(
        Prefetch("scim_groups", to_attr="linked_group_list")
    )
    if filters.archived is not None:
        teams = teams.filter(archived_at__isnull=not filters.archived)
    if filters.can_create_projects is not None:
        creatable = permissions.teams_for_new_project(caller(request)).values("pk")
        teams = (
            teams.filter(pk__in=creatable)
            if filters.can_create_projects
            else teams.exclude(pk__in=creatable)
        )
    result = page(teams, filters)
    for item in result["items"]:
        item.linked_group_names = [g.display_name for g in item.linked_group_list]
    return result


@router.post("/teams", response={201: TeamOut}, summary="Create a team")
def create_team(request: HttpRequest, payload: TeamIn) -> Status[Team]:
    me = caller(request)
    require(me, Action.TEAM_CREATE)
    return Status(201, services.create_team(payload.name.strip(), payload.description, by=me))


@router.get("/teams/{team_id}", response=TeamOut, summary="A team")
def get_team(request: HttpRequest, team_id: UUID) -> Team:
    return team(caller(request), team_id)


@router.patch("/teams/{team_id}", response=TeamOut, summary="Rename, describe, archive or restore")
def patch_team(request: HttpRequest, team_id: UUID, payload: TeamPatch) -> Team:
    me = caller(request)
    found = team(me, team_id, Action.TEAM_MANAGE)
    with transaction.atomic():
        if payload.name is not None or payload.description is not None:
            services.update_team(
                found,
                payload.name.strip() if payload.name is not None else found.name,
                payload.description if payload.description is not None else found.description,
                by=me,
            )
        if payload.archived is not None and payload.archived != found.is_archived:
            services.set_team_archived(found, payload.archived, by=me)
    return found


@router.get("/teams/{team_id}/members", response=Page[TeamMemberOut], summary="A team's members")
def list_members(request: HttpRequest, team_id: UUID, params: Query[PageParams]) -> dict[str, Any]:
    found = team(caller(request), team_id)
    return page(found.memberships.select_related("user"), params)


@router.put(
    "/teams/{team_id}/members/{user_id}", response=TeamMemberOut, summary="Add someone to a team"
)
def add_member(request: HttpRequest, team_id: UUID, user_id: UUID) -> Any:  # noqa: ANN401
    """Refused while identity provider groups decide the team's members."""
    me = caller(request)
    found = team(me, team_id, Action.TEAM_MANAGE)
    subject = person(user_id)
    services.add_team_member(found, subject, by=me)
    return found.memberships.select_related("user").get(user=subject)


@router.delete(
    "/teams/{team_id}/members/{user_id}", response={204: None}, summary="Remove someone from a team"
)
def remove_member(request: HttpRequest, team_id: UUID, user_id: UUID) -> Status[None]:
    me = caller(request)
    found = team(me, team_id, Action.TEAM_MANAGE)
    subject = person(user_id)
    if not found.memberships.filter(user=subject).exists():
        raise Problem(404, "That person isn’t in the team.")
    services.remove_team_member(found, subject, by=me)
    return Status(204, None)

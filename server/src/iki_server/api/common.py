"""Helpers the endpoints share: who is calling, and finding what they asked for."""

from typing import Any
from uuid import UUID

from django.db.models import Case, F, Model, QuerySet, UUIDField, Value, When
from django.db.models.functions import Coalesce
from django.http import HttpRequest

from iki_server.accounts.models import User
from iki_server.api.problems import Problem
from iki_server.org import permissions
from iki_server.org.models import Project, Registry, Team
from iki_server.org.permissions import Action


def caller(request: HttpRequest) -> User:
    user: Any = request.auth  # type: ignore[attr-defined]
    if not isinstance(user, User):
        raise Problem(401)
    return user


def require_scope(user: User, scope: str) -> None:
    """For endpoints about the caller themselves, which no role governs: a token needs the scope."""
    limits = permissions.token_limits(user)
    if limits is not None and scope not in limits.scopes:
        raise Problem(
            403,
            f"This needs the {scope} scope, which your token doesn’t have.",
            code="insufficient_scope",
            headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
        )


def require(user: User, action: Action, target: Team | Project | Registry | None = None) -> None:
    if permissions.can(user, action, target):
        return
    limits = permissions.token_limits(user)
    needed = permissions.ACTION_SCOPES[action]
    if limits is not None and needed not in limits.scopes:
        raise Problem(
            403,
            f"This needs the {needed} scope, which your token doesn’t have.",
            code="insufficient_scope",
            headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{needed}"'},
        )
    if (
        limits is not None
        and limits.beyond_projects(action)
        and permissions.roles_allow(user, action, target)
    ):
        raise Problem(
            403,
            "This token was given only some of your projects, so it can’t act on the "
            "organization, its teams or its registries. To do this, sign in to the app "
            "again and allow it all your projects.",
            code="token_limited_to_projects",
        )
    raise Problem(403)


def project(user: User, project_id: UUID, action: Action = Action.PROJECT_SEE) -> Project:
    """The project, if the caller can see it (else 404) and take the action (else 403)."""
    found = (
        permissions.with_project_roles(user, Project.objects.select_related("team", "registry"))
        .filter(pk=project_id)
        .first()
    )
    if found is None or not permissions.can(user, Action.PROJECT_SEE, found):
        raise Problem(404, "No such project, or you can’t see it.")
    require(user, action, found)
    return found


def team(user: User, team_id: UUID, action: Action = Action.TEAM_VIEW) -> Team:
    found = Team.objects.filter(pk=team_id).first()
    if found is None or not permissions.can(user, Action.TEAM_VIEW, found):
        raise Problem(404, "No such team, or you can’t see it.")
    require(user, action, found)
    return found


def person(user_id: UUID) -> User:
    found = User.objects.filter(pk=user_id).first()
    if found is None:
        raise Problem(404, "No such person or service account.")
    return found


#: The sort fields :func:`by_scope` adds, for :func:`~iki_server.api.pagination.page_by`.
SCOPE_ORDER = ("scope_rank", "scope_id")


def by_scope[M: Model](queryset: QuerySet[M, M]) -> QuerySet[M, M]:
    """A budget's or quota's queryset, able to be sorted the organization's first, then each
    team's, then each project's, by ID: :data:`SCOPE_ORDER`, never null, so it pages."""
    return queryset.annotate(  # type: ignore[return-value] # annotate widens the row type
        scope_rank=Case(
            When(project__isnull=False, then=Value(2)),
            When(team__isnull=False, then=Value(1)),
            default=Value(0),
        ),
        scope_id=Coalesce(
            F("project_id"), F("team_id"), Value(UUID(int=0)), output_field=UUIDField()
        ),
    )

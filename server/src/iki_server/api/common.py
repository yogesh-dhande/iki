"""Helpers the endpoints share: who is calling, and finding what they asked for."""

from typing import Any
from uuid import UUID

from django.http import HttpRequest

from iki_server.accounts.models import User
from iki_server.api.problems import Problem
from iki_server.org import permissions
from iki_server.org.models import Project, Team
from iki_server.org.permissions import Action


def caller(request: HttpRequest) -> User:
    user: Any = request.auth  # type: ignore[attr-defined]
    if not isinstance(user, User):
        raise Problem(401)
    return user


def require(user: User, action: Action, target: Team | Project | None = None) -> None:
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
    raise Problem(403)


def project(user: User, project_id: UUID, action: Action = Action.PROJECT_SEE) -> Project:
    """The project, if the caller can see it (else 404) and take the action (else 403)."""
    found = (
        permissions.with_project_roles(user, Project.objects.select_related("team"))
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

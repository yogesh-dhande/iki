"""People and service accounts, and their org roles."""

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Field, Query, Router, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, person, require
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import (
    KindName,
    OrgRolesIn,
    OrgRolesOut,
    Page,
    UserOut,
    UserPatch,
    org_role_names,
    org_roles_from,
)
from iki_server.org import permissions, services
from iki_server.org.permissions import Action

router = Router(tags=["users"])


class UserFilters(PageParams):
    email: str | None = Field(None, description="Exact address, compared case-insensitively.")
    kind: KindName | None = None
    active: bool | None = None


@router.get("/users", response=Page[UserOut], summary="List or look up people")
def list_users(request: HttpRequest, filters: Query[UserFilters]) -> dict[str, Any]:
    """Org Admins can list everyone. Anyone signed in can look someone up by email."""
    user = caller(request)
    users: QuerySet[User] = User.objects.all()
    if not filters.email and not permissions.can(user, Action.ORG_MANAGE):
        raise Problem(403, "Only Org Admins can list everyone. Look someone up by email.")
    if filters.email:
        users = users.filter(email=filters.email.strip())
    if filters.kind:
        users = users.filter(kind=filters.kind)
    if filters.active is not None:
        users = users.filter(is_active=filters.active)
    return page(users, filters)


@router.get("/users/{user_id}", response=UserOut, summary="A person or service account")
def get_user(request: HttpRequest, user_id: UUID) -> User:
    caller(request)
    return person(user_id)


@router.patch("/users/{user_id}", response=UserOut, summary="Deactivate or reactivate someone")
def patch_user(request: HttpRequest, user_id: UUID, payload: UserPatch) -> User:
    """Deactivating someone ends their sessions at once. People provisioned over SCIM
    are activated and deactivated in the identity provider instead."""
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    subject = person(user_id)
    if payload.active is not None:
        services.set_active(subject, payload.active, by=me)
    return subject


@router.delete(
    "/users/{user_id}/sessions", response={204: None}, summary="Sign someone out everywhere"
)
def end_sessions(request: HttpRequest, user_id: UUID) -> Status[None]:
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    services.end_sessions(person(user_id), by=me)
    return Status(204, None)


@router.get("/users/{user_id}/org-roles", response=OrgRolesOut, summary="Someone's org roles")
def get_org_roles(request: HttpRequest, user_id: UUID) -> dict[str, Any]:
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    return {"roles": org_role_names(person(user_id))}


@router.put("/users/{user_id}/org-roles", response=OrgRolesOut, summary="Set someone's org roles")
def put_org_roles(request: HttpRequest, user_id: UUID, payload: OrgRolesIn) -> dict[str, Any]:
    """Replaces the org roles. Service accounts can't be Org Admins."""
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    subject = person(user_id)
    services.set_org_roles(subject, org_roles_from(payload.roles), by=me)
    permissions.forget(subject)
    return {"roles": org_role_names(subject)}

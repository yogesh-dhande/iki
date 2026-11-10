"""Service accounts: non-person users for scripts, pipelines and instruments."""

from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Query, Router, Status

from iki_server.accounts import service_accounts
from iki_server.accounts.models import Kind, User
from iki_server.api.common import caller, require
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, ServiceAccountIn, ServiceAccountOut, ServiceAccountPatch
from iki_server.org import services
from iki_server.org.permissions import Action

router = Router(tags=["service accounts"])


def _accounts() -> QuerySet[User]:
    return User.objects.filter(kind=Kind.SERVICE).select_related(
        "service_account", "service_account__created_by"
    )


def _account(account_id: UUID) -> User:
    found = _accounts().filter(pk=account_id).first()
    if found is None:
        raise Problem(404, "No such service account.")
    return found


@router.get("/service-accounts", response=Page[ServiceAccountOut], summary="List service accounts")
def list_accounts(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    require(caller(request), Action.ORG_MANAGE)
    return page(_accounts(), params)


@router.post(
    "/service-accounts", response={201: ServiceAccountOut}, summary="Create a service account"
)
def create_account(request: HttpRequest, payload: ServiceAccountIn) -> Status[User]:
    """Grant it project roles afterwards, like a person. It gets an address under
    service-accounts.iki.invalid that project Admins use to add it."""
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    account = service_accounts.create(payload.name, payload.description, by=me)
    return Status(201, _account(account.pk))


@router.get(
    "/service-accounts/{account_id}", response=ServiceAccountOut, summary="A service account"
)
def get_account(request: HttpRequest, account_id: UUID) -> User:
    require(caller(request), Action.ORG_MANAGE)
    return _account(account_id)


@router.patch(
    "/service-accounts/{account_id}",
    response=ServiceAccountOut,
    summary="Rename, describe, deactivate or reactivate a service account",
)
def patch_account(request: HttpRequest, account_id: UUID, payload: ServiceAccountPatch) -> User:
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    account = _account(account_id)
    with transaction.atomic():
        if payload.name is not None or payload.description is not None:
            service_accounts.update(
                account,
                name=payload.name if payload.name is not None else account.name,
                description=(
                    payload.description
                    if payload.description is not None
                    else account.service_account.description
                ),
                by=me,
            )
        if payload.active is not None:
            services.set_active(account, payload.active, by=me)
    return _account(account_id)

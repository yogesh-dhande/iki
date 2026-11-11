"""Saved searches of a project's entities: your own, and those shared with the project.

Run one by sending its ``query`` to ``POST /entities/search`` with its project.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project, require_scope
from iki_server.api.entities import Condition, SortIn
from iki_server.api.problems import Problem
from iki_server.org.permissions import Action
from iki_server.searches import services
from iki_server.searches.models import SavedSearch

router = Router(tags=["saved searches"])


class QueryIn(Schema):
    """A search, as ``POST /entities/search`` takes it, without its project or paging."""

    type: str | None = None
    exact_type: bool = False
    archived: bool | None = None
    q: str | None = Field(None, max_length=200)
    where: Condition | None = None
    sort: SortIn | None = None


class SavedSearchIn(Schema):
    project: UUID
    name: str = Field(max_length=100)
    query: QueryIn
    shared: bool = Field(False, description="Show it to everyone who can read the project.")


class SavedSearchPatch(Schema):
    name: str | None = Field(None, max_length=100)
    query: QueryIn | None = None
    shared: bool | None = None


class PersonRef(Schema):
    id: UUID
    name: str


class SavedSearchCan(Schema):
    change: bool
    delete: bool


class SavedSearchOut(Schema):
    id: UUID
    project: UUID
    name: str
    query: dict[str, Any] = Field(description="The search, ready for POST /entities/search.")
    shared: bool
    created_by: PersonRef
    created_at: datetime
    updated_at: datetime
    can: SavedSearchCan


class SavedSearchFilters(Schema):
    project: UUID


def _query(query: QueryIn) -> dict[str, Any]:
    out = query.model_dump(by_alias=True, exclude_unset=True, exclude={"sort", "where"})
    if query.where is not None:
        out["where"] = query.where.model_dump(by_alias=True, exclude_unset=True)
    if query.sort is not None:
        out["sort"] = query.sort.field
        out["descending"] = query.sort.descending
    return out


def _out(user: User, saved: SavedSearch) -> dict[str, Any]:
    query = dict(saved.query)
    sort = query.pop("sort", None)
    descending = query.pop("descending", False)
    if sort:
        query["sort"] = {"field": sort, "descending": descending}
    return {
        "id": saved.pk,
        "project": saved.project_id,
        "name": saved.name,
        "query": query,
        "shared": saved.shared,
        "created_by": {"id": saved.created_by.pk, "name": saved.created_by.get_full_name()},
        "created_at": saved.created_at,
        "updated_at": saved.updated_at,
        "can": {
            "change": services.can_change(user, saved),
            "delete": services.can_delete(user, saved),
        },
    }


def _refused(error: services.Refused) -> Problem:
    code = {403: "forbidden", 409: "conflict", 422: "validation_failed"}[error.status]
    return Problem(
        error.status,
        error.detail,
        code=code,
        errors=[{"pointer": p, "detail": d} for p, d in error.errors] or None,
    )


def _find(user: User, saved_id: UUID) -> SavedSearch:
    saved = (
        SavedSearch.objects.select_related("project__team", "created_by")
        .filter(pk=saved_id)
        .first()
    )
    if saved is not None:
        try:
            project(user, saved.project_id, Action.PROJECT_READ)
        except Problem:
            saved = None
    if saved is None or not services.visible(user, saved.project).filter(pk=saved.pk).exists():
        raise Problem(404, "No such saved search, or you can’t see it.")
    return saved


@router.get("/saved-searches", response=list[SavedSearchOut], summary="A project's saved searches")
def list_saved_searches(request: HttpRequest, filters: Query[SavedSearchFilters]) -> Any:  # noqa: ANN401
    """Yours, and those shared with the project, by name."""
    me = caller(request)
    found = project(me, filters.project, Action.PROJECT_READ)
    searches = services.visible(me, found).select_related("project__team", "created_by")
    return [_out(me, s) for s in searches.order_by("name", "pk")]


@router.post("/saved-searches", response={201: SavedSearchOut}, summary="Save a search")
def create_saved_search(request: HttpRequest, payload: SavedSearchIn) -> Any:  # noqa: ANN401
    """Checked as you, now. Sharing it with the project takes Editor there."""
    me = caller(request)
    require_scope(me, "write")
    found = project(me, payload.project, Action.PROJECT_READ)
    try:
        saved = services.create(
            me, found, name=payload.name, query=_query(payload.query), shared=payload.shared
        )
    except services.Refused as error:
        raise _refused(error) from error
    return Status(201, _out(me, saved))


@router.get("/saved-searches/{saved_id}", response=SavedSearchOut, summary="A saved search")
def get_saved_search(request: HttpRequest, saved_id: UUID) -> Any:  # noqa: ANN401
    me = caller(request)
    return _out(me, _find(me, saved_id))


@router.patch(
    "/saved-searches/{saved_id}", response=SavedSearchOut, summary="Change a saved search"
)
def update_saved_search(request: HttpRequest, saved_id: UUID, payload: SavedSearchPatch) -> Any:  # noqa: ANN401
    """Only its owner can change it."""
    me = caller(request)
    require_scope(me, "write")
    saved = _find(me, saved_id)
    try:
        saved = services.update(
            me,
            saved,
            name=payload.name,
            query=_query(payload.query) if payload.query is not None else None,
            shared=payload.shared,
        )
    except services.Refused as error:
        raise _refused(error) from error
    return _out(me, saved)


@router.delete("/saved-searches/{saved_id}", response={204: None}, summary="Delete a saved search")
def delete_saved_search(request: HttpRequest, saved_id: UUID) -> Any:  # noqa: ANN401
    """Its owner can, and the project's Admins can remove a shared one."""
    me = caller(request)
    require_scope(me, "write")
    saved = _find(me, saved_id)
    try:
        services.delete(me, saved)
    except services.Refused as error:
        raise _refused(error) from error
    return Status(204, None)

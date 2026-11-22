"""Saved searches of entities: your own, and those shared with a project.

Run one by sending its ``query`` to ``POST /entities/search`` with its project, or with
none for a search of every project you can read (ADR 0176).
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
from iki_server.api.schemas import PersonRef
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
    project: UUID | None = Field(
        None,
        description="The project it searches. Without one, it searches every project you can "
        "read, and is yours alone: it can't be shared.",
    )
    name: str = Field(max_length=100)
    query: QueryIn
    shared: bool = Field(False, description="Show it to everyone who can read the project.")


class SavedSearchPatch(Schema):
    name: str | None = Field(None, max_length=100)
    query: QueryIn | None = None
    shared: bool | None = None


class SavedSearchCan(Schema):
    change: bool
    delete: bool


class SavedSearchOut(Schema):
    id: UUID
    project: UUID | None = Field(
        description="The project it searches; null for every project you can read."
    )
    name: str
    query: dict[str, Any] = Field(description="The search, ready for POST /entities/search.")
    shared: bool
    created_by: PersonRef
    created_at: datetime
    updated_at: datetime
    can: SavedSearchCan


class SavedSearchFilters(Schema):
    project: UUID | None = Field(
        None,
        description="Only this project's. Without one, every search you see: your own, and "
        "those shared with projects you can read.",
    )
    mine: bool = Field(False, description="Only your own: the sidebar's saved views.")
    limit: int | None = Field(None, ge=1, le=100, description="At most this many.")


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
        "created_by": saved.created_by,
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
    if saved is not None and saved.project_id is not None:
        try:
            project(user, saved.project_id, Action.PROJECT_READ)
        except Problem:
            saved = None
    if saved is None or not services.visible(user, saved.project).filter(pk=saved.pk).exists():
        raise Problem(404, "No such saved search, or you can’t see it.")
    return saved


@router.get("/saved-searches", response=list[SavedSearchOut], summary="Saved searches")
def list_saved_searches(request: HttpRequest, filters: Query[SavedSearchFilters]) -> Any:  # noqa: ANN401
    """Yours, and those shared with the project, by name. Without a project: every one you
    see, your searches of every project among them. Only your own with ``mine`` (the
    sidebar's saved views, ADR 0176)."""
    me = caller(request)
    if filters.project is None:
        searches = services.mine(me) if filters.mine else services.everywhere(me)
    else:
        searches = services.visible(me, project(me, filters.project, Action.PROJECT_READ))
        if filters.mine:
            searches = searches.filter(created_by=me)
    searches = searches.select_related("project__team", "created_by").order_by("name", "pk")
    if filters.limit is not None:
        searches = searches[: filters.limit]
    return [_out(me, s) for s in searches]


@router.post("/saved-searches", response={201: SavedSearchOut}, summary="Save a search")
def create_saved_search(request: HttpRequest, payload: SavedSearchIn) -> Any:  # noqa: ANN401
    """Checked as you, now. Sharing it with the project takes Editor there."""
    me = caller(request)
    require_scope(me, "write")
    found = (
        project(me, payload.project, Action.PROJECT_READ) if payload.project is not None else None
    )
    if found is None and not services.reaches_every_project(me):
        raise Problem(
            403,
            "This token was given only some of your projects, so it can’t save a search of "
            "every project. Save it in one of its projects, or sign in to the app again and "
            "allow it all your projects.",
            code="token_limited_to_projects",
        )
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

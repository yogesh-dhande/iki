"""Collections from searches (ADR 0038): keep the search, or the members it finds now.

A collection is an entity (``core.Collection``): read, edit and archive it at
``/entities/{ref}``, and list its members with ``collection`` on
``POST /entities/search``. These two endpoints run a search on the server, as
the caller, so a fixed list of up to 10,000 entities is made in one step.
"""

from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from ninja import Field, HeaderEx, P, Router, Schema, Status

from iki_server.api.common import caller, project, require
from iki_server.api.entities import (
    Condition,
    CreatedOut,
    EntityOut,
    _expected,
    _find,
    _one,
    _searching,
)
from iki_server.api.problems import Problem
from iki_server.entities import collections, services
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.org.models import Project
from iki_server.org.permissions import Action

router = Router(tags=["collections"])


class CollectionSearch(Schema):
    """A search, as ``POST /entities/search`` takes it, without paging."""

    project: UUID | None = Field(None, description="Only entities in this project.")
    type: str | None = None
    exact_type: bool = False
    archived: bool | None = None
    q: str | None = Field(None, max_length=200)
    where: Condition | None = None
    sort: str | None = Field(None, description="The field it's in order of.")
    descending: bool = False


class CollectionIn(Schema):
    project: UUID = Field(description="The project it belongs to. It needs you as an Editor.")
    name: str = Field(max_length=200)
    description: str | None = Field(None, max_length=10000)
    search: CollectionSearch
    keep: Literal["search", "members"] = Field(
        "search",
        description="`search`: a dynamic collection, holding whatever its search finds when "
        "it's read. `members`: a fixed list of what it finds for you now (at most 10,000).",
    )
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class FreezeIn(Schema):
    reason: str = Field("", max_length=2000, description="Why, for the record.")


def _stored(search: CollectionSearch) -> dict[str, Any]:
    out = search.model_dump(exclude_unset=True, exclude={"where", "project"})
    if search.project is not None:
        out["project"] = str(search.project)
    if search.where is not None:
        out["where"] = search.where.model_dump(by_alias=True, exclude_unset=True)
    return {k: v for k, v in out.items() if v is not None or k == "archived"}


def _invalid(error: Invalid) -> Problem:
    return Problem(
        422,
        "Some values are invalid.",
        code="validation_failed",
        errors=[
            {"pointer": f"#/{name}", "detail": " ".join(messages)}
            for name, messages in error.errors.items()
        ],
    )


def _too_many(error: collections.TooMany) -> Problem:
    return Problem(409, str(error), code="too_many")


@router.post("/collections", response={201: CreatedOut}, summary="Make a collection from a search")
def create_collection(request: HttpRequest, payload: CollectionIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """Keep the search (a dynamic collection), or the members it finds for you now, in its
    order (a fixed list). Either way it's checked as you."""
    me = caller(request)
    target = project(me, payload.project, Action.PROJECT_EDIT)
    try:
        with _searching():
            saved = collections.create_from_search(
                me,
                target,
                name=payload.name,
                description=payload.description,
                data=_stored(payload.search),
                keep=payload.keep,
                reason=payload.reason,
            )
    except Invalid as error:
        raise _invalid(error) from None
    except collections.TooMany as error:
        raise _too_many(error) from None
    entity = Entity.objects.select_related("created_by").get(pk=saved.entity.pk)
    response["Location"] = f"/api/v1/entities/{entity.pk}"
    return Status(201, {**_one(me, entity, response), "reused": False, "linked": False})


@router.post("/collections/{ref}/freeze", response=EntityOut, summary="Freeze a dynamic collection")
def freeze_collection(
    request: HttpRequest,
    ref: str,
    payload: FreezeIn,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Replace its search with the members it finds for you now, in its order, as one
    revision: a fixed list a run or a paper can cite. Send its ETag as If-Match."""
    me = caller(request)
    entity = _find(me, ref)
    owner = Project.objects.select_related("team").get(pk=entity.owner_project_id)
    require(me, Action.PROJECT_EDIT, owner)
    expected = _expected(if_match)
    try:
        with _searching():
            collections.freeze(me, entity, expected_revision=expected, reason=payload.reason)
    except collections.NotDynamic as error:
        raise Problem(409, str(error), code="conflict") from None
    except collections.TooMany as error:
        raise _too_many(error) from None
    except services.Stale as stale:
        raise Problem(
            412,
            f"Someone changed it since you read it: it’s at revision {stale.current} now. "
            "Read it again, then freeze it.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    return _one(me, _find(me, str(entity.pk)), response)

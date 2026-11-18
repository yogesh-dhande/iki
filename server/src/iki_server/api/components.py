"""Components: the Web Components installed plugins add to Iki's pages (ADR 0084, 0117).

``GET /components`` lists them, with where each appears and what it reads. Their code
runs only in a sandboxed frame the web app makes, never in its page, and gets data only
from the page: ``POST /components/{name}/entities`` is what the page gives a component
that shows many, found as the grid's search finds them, as the caller may read them.
"""

from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest
from django.urls import reverse
from ninja import Field, Router, Schema
from pydantic import ConfigDict

from iki_server.api.common import caller, project, require_scope
from iki_server.api.entities import Condition, RefOut, SortIn, _ids_in, _searching
from iki_server.api.entities import _out as entities_out
from iki_server.api.problems import Problem
from iki_server.api.schemas import ProjectRef
from iki_server.components import places
from iki_server.components.installed import components
from iki_server.entities import search
from iki_server.entities.values import shape
from iki_server.org.permissions import Action
from iki_spec.model import Component

router = Router(tags=["components"])

Slot = Literal["entity.tab", "search.results", "project.page", "person.home"]


class SlotSearchOut(Schema):
    where: dict[str, Any] | None = Field(
        description="Its conditions, as the search API takes them. A value can be "
        "`{{ entity }}`, the entity whose page it is, or `{{ me }}`, the person viewing it."
    )
    sort: str | None
    descending: bool


class SlotOut(Schema):
    slot: Slot = Field(
        description="`entity.tab`: a tab of its own on the pages of entities of `type`. "
        "`search.results`: the grid's results of `type`, shown another way. `project.page`: "
        "a section on projects' pages, of what `search` finds in each. `person.home`: a "
        "section on people's homes, of what `search` finds in the projects they read."
    )
    type: str = Field(
        description="For a tab, the entity type whose pages show it, with its subtypes; "
        "otherwise the type it shows."
    )
    label: str = Field(description="What its tab, section or view is called.")
    search: SlotSearchOut | None = Field(
        description="What fills it, of the component's `entities`; null for a tab that "
        "shows only its entity, and for search results, which are the grid's."
    )


class EntitiesOut(Schema):
    type: str = Field(description="The type of the entities it shows, with its subtypes.")
    fields: list[str] = Field(description="The fields it reads of each: all it's given.")
    limit: int = Field(description="The most it holds at once.")


class ComponentOut(Schema):
    name: str = Field(description="Its qualified name, such as `protein.plate_map`.")
    plugin: str
    label: str
    description: str | None
    element: str = Field(description="The custom element its module defines.")
    slots: list[SlotOut]
    entities: EntitiesOut | None = Field(
        description="The many entities it shows (ADR 0117), or null if it shows only the "
        "entity whose page it's on."
    )
    selects: str | None = Field(
        description="The type of the entities a person can select in it, or null."
    )
    sha256: str = Field(description="Its module's SHA-256: the code its frames run.")
    frame: str = Field(description='The page to show it in, in `<iframe sandbox="allow-scripts">`.')


def _out(component: Component) -> dict[str, Any]:
    many = component.entities
    return {
        "name": component.qualified_name,
        "plugin": component.plugin,
        "label": component.label,
        "description": component.description,
        "element": component.element,
        "slots": [
            {
                "slot": s.slot,
                "type": s.type,
                "label": s.label,
                "search": {
                    "where": s.search.where,
                    "sort": s.search.sort or None,
                    "descending": s.search.descending,
                }
                if s.search is not None
                else None,
            }
            for s in component.slots
        ],
        "entities": {"type": many.type, "fields": list(many.fields), "limit": many.limit}
        if many is not None
        else None,
        "selects": component.selects or None,
        "sha256": component.sha256,
        "frame": reverse("component_frame", args=[component.qualified_name, component.sha256]),
    }


@router.get("/components", response=list[ComponentOut], summary="Installed components")
def list_components(request: HttpRequest) -> list[dict[str, Any]]:
    caller(request)
    return [_out(c) for c in components().values()]


class PlaceSearchIn(Schema):
    """A search, as ``POST /entities/search`` takes it, without its paging."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="The component's type, or one that extends it.")
    exact_type: bool = False
    project: UUID | None = Field(None, description="Entities that belong to this project.")
    archived: bool | None = None
    q: str | None = Field(None, max_length=200)
    collection: str | None = Field(None, max_length=100)
    where: Condition | None = None
    sort: SortIn | None = None


class RefineIn(Schema):
    """What the component asked for: it can only narrow the place's search."""

    model_config = ConfigDict(extra="forbid")

    where: Condition | None = Field(
        None,
        description="Conditions on the fields it reads (and `hrid`), added to the place's "
        "with `all`. At most 20, nested at most 4 deep.",
    )
    sort: SortIn | None = Field(None, description="One of the fields it reads, or `hrid`.")
    q: str | None = Field(
        None, max_length=200, description="Text in HRIDs and titles, if the place has none."
    )


class ComponentEntitiesIn(Schema):
    model_config = ConfigDict(extra="forbid")

    entity: UUID | None = Field(
        None, description="An entity's tab: the record whose page it's on, by ID."
    )
    search: PlaceSearchIn | None = Field(None, description="A search's results, as the grid's.")
    project: UUID | None = Field(None, description="A project's page.")
    home: bool = Field(False, description="The caller's home.")
    refine: RefineIn | None = None
    cursor: str | None = Field(None, description="From the previous page's next_cursor.")
    limit: int = Field(100, ge=1, le=100, description="Items per page.")


class ComponentEntityOut(Schema):
    id: UUID
    hrid: str
    title: str
    type: str
    revision: int
    project: ProjectRef | None = Field(description="The project it belongs to, if you can see it.")
    values: dict[str, Any] = Field(description="The values of the fields the component reads.")
    refs: dict[str, RefOut] = Field(
        description="The entities those fields refer to that you can read, by ID."
    )


class ComponentEntitiesOut(Schema):
    items: list[ComponentEntityOut]
    next_cursor: str | None


def _place(me: Any, payload: ComponentEntitiesIn) -> places.Place:  # noqa: ANN401 - User
    given = [
        name
        for name, value in (
            ("entity", payload.entity),
            ("search", payload.search),
            ("project", payload.project),
            ("home", payload.home or None),
        )
        if value is not None
    ]
    if len(given) != 1:
        raise Problem(
            422,
            "Name one place: an entity's tab (`entity`), a search's results (`search`), a "
            "project's page (`project`) or your home (`home`).",
            code="validation_failed",
            errors=[{"pointer": f"#/{name}", "detail": "Give only one place."} for name in given]
            or [{"pointer": "#", "detail": "Give a place."}],
        )
    if payload.entity is not None:
        return places.Place(entity=payload.entity)
    if payload.project is not None:
        return places.Place(project=project(me, payload.project, Action.PROJECT_READ))
    if payload.search is not None:
        wanted = payload.search
        if wanted.project is not None:
            project(me, wanted.project, Action.PROJECT_READ)
        return places.Place(
            search=search.Search(
                type=wanted.type,
                exact_type=wanted.exact_type,
                project=wanted.project,
                archived=wanted.archived,
                q=wanted.q,
                collection=wanted.collection,
                where=wanted.where.model_dump(by_alias=True, exclude_unset=True)
                if wanted.where
                else None,
                sort=wanted.sort.field if wanted.sort else None,
                descending=wanted.sort.descending if wanted.sort else False,
            )
        )
    return places.Place(home=True)


def _refinement(refine: RefineIn | None) -> places.Refinement | None:
    if refine is None:
        return None
    return places.Refinement(
        where=refine.where.model_dump(by_alias=True, exclude_unset=True) if refine.where else None,
        sort=refine.sort.field if refine.sort else None,
        descending=refine.sort.descending if refine.sort else False,
        q=refine.q.strip() if refine.q and refine.q.strip() else None,
    )


def _projected(component: Component, full: dict[str, Any]) -> dict[str, Any]:
    """An entity as a component may have it: only the fields it reads, and what they
    refer to."""
    assert component.entities is not None  # noqa: S101 - only components with entities
    fields = shape(full["type"]).fields()
    values = {
        name: full["values"][name]
        for name in component.entities.fields
        if name in fields and name in full["values"]
    }
    ids = {str(i) for i in _ids_in({n: fields[n] for n in values}, values)["ref"]}
    return {
        "id": full["id"],
        "hrid": full["hrid"],
        "title": full["title"],
        "type": full["type"],
        "revision": full["revision"],
        "project": full["owner_project"],
        "values": values,
        "refs": {i: ref for i, ref in full["refs"].items() if i in ids},
    }


@router.post(
    "/components/{qualified:name}/entities",
    response=ComponentEntitiesOut,
    summary="What a component shows in a place",
)
def component_entities(
    request: HttpRequest, name: str, payload: ComponentEntitiesIn
) -> dict[str, Any]:
    """The entities that fill a component's place, a page at a time, as you may read them
    (ADR 0117): an entity's tab (the tab's search, about that entity, in its project), a
    search's results (as ``POST /entities/search`` finds them), a project's page or your
    home (the slot's search there). ``refine`` narrows it as the component asked, naming
    only the fields it reads. Each entity comes with only those fields' values."""
    me = caller(request)
    require_scope(me, "read")
    component = components().get(name)
    if component is None:
        raise Problem(404, "No such component is installed.")
    place = _place(me, payload)
    limit = min(payload.limit, component.entities.limit if component.entities else 100)
    with _searching():
        try:
            wanted, found = places.found(me, component, place, _refinement(payload.refine))
        except places.NotFound:
            raise Problem(404, "No such entity, or you can’t read it.") from None
        except places.Refused as refused:
            raise Problem(
                422,
                refused.detail,
                code=refused.code,
                errors=[{"pointer": p, "detail": d} for p, d in refused.errors] or None,
            ) from None
        except places.Misdeclared:
            raise Problem(
                422,
                f"{component.label}’s search doesn’t work here: tell whoever looks after the "
                f"{component.plugin} plugin.",
                code="component_misdeclared",
            ) from None
        rows, cursor = search.page(found, wanted, payload.cursor, limit)
        return {
            "items": [_projected(component, e) for e in entities_out(me, rows)],
            "next_cursor": cursor,
        }

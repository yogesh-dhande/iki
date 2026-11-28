"""What fills a component's place (ADR 0117): the entities it shows, found as the grid's
search finds them, as the person viewing it may read them.

A place is one of:

- an entity's tab: the tab's declared search, its ``{{ entity }}`` that entity, in the
  entity's project;
- a search's results: the search the page gives, as the grid runs it;
- a project's page: the slot's declared search, in that project;
- a person's home: the slot's declared search, in every project they read, its
  ``{{ me }}`` them.

``{{ me }}`` is the person viewing it wherever it's used: today it's compared with
``created_by``, and step 57's fields that name a person take it the same way.

What the component asks for (a refinement) is added to the place's search with ``all``,
so it can only narrow it; it names only the fields the component reads, and ``hrid``.
Every search runs with :func:`search.queryset`, as the caller, so the component sees
nothing the grid wouldn't show them.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from django.db.models import QuerySet

from iki_server.entities import access, registry, search
from iki_server.entities.models import Entity
from iki_server.entities.values import subtypes
from iki_spec import conditions
from iki_spec.components import EVERY_RECORD, context_of
from iki_spec.model import Component, ComponentSearch, ComponentSlot

logger = structlog.get_logger("iki.components")

# The most a refinement may say, by the names @ikisci/component gives them (ADR 0286),
# which a test holds equal: conditions, how deeply they nest, and text.
MAX_CONDITIONS = conditions.MAX_CONDITIONS
MAX_DEPTH = conditions.MAX_DEPTH
MAX_QUERY_LENGTH = 200
#: What a refinement can name besides the component's fields.
REFINABLE_COLUMNS = ("hrid",)


class NotFound(Exception):  # noqa: N818 - reads as "the place isn't found"
    """The place is nothing the caller can read: 404, as anything they can't read is."""


class Refused(Exception):  # noqa: N818 - reads as "the request is refused"
    """The component doesn't show in that place, or what it asked for can't be done."""

    def __init__(self, code: str, detail: str, errors: list[tuple[str, str]] | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors = errors or []


class Misdeclared(Exception):  # noqa: N818 - reads as "the component is misdeclared"
    """A slot's declared search doesn't run here: the plugin's mistake, not the caller's."""


@dataclass(frozen=True)
class Refinement:
    """What the component asked for: conditions to add, a sort, and text."""

    where: Mapping[str, Any] | None = None
    sort: str | None = None
    descending: bool = False
    q: str | None = None


@dataclass(frozen=True)
class Place:
    """Exactly one of these: an entity's tab, a search's results, a project's page, or a
    person's home."""

    entity: uuid.UUID | None = None
    search: search.Search | None = None
    project: Any = None  # a Project the caller can read, as the API found it
    home: bool = False


def tab_for(component: Component, type_name: str) -> ComponentSlot | None:
    """The component's tab on pages of ``type_name``: the one for the nearest type of its
    lineage, or else its tab on every record (ADR 0237)."""
    lineage = (
        list(registry.effective(type_name).lineage)
        if type_name in registry.types()
        else [type_name, EVERY_RECORD]
    )
    tabs = {s.type: s for s in component.slots if s.slot == "entity.tab"}
    return next((tabs[t] for t in lineage if t in tabs), None)


def _slot(component: Component, kind: str) -> ComponentSlot | None:
    return next((s for s in component.slots if s.slot == kind), None)


def _filled(node: Any, context: Mapping[str, str]) -> Any:  # noqa: ANN401 - JSON
    """A declared condition with its context worked out: ``{{ entity }}`` and ``{{ me }}``
    as their IDs."""
    if isinstance(node, Mapping):
        return {k: _filled(v, context) for k, v in node.items()}
    if isinstance(node, list):
        return [_filled(v, context) for v in node]
    named = context_of(node)
    return context[named] if named else node


def _declared(
    component: Component,
    slot: ComponentSlot,
    context: Mapping[str, str],
    project: uuid.UUID | None = None,
) -> search.Search:
    declared = slot.search or ComponentSearch()
    assert component.entities is not None  # noqa: S101 - checked before
    return search.Search(
        type=component.entities.type,
        archived=False,
        where=_filled(declared.where, context) if declared.where is not None else None,
        sort=declared.sort or None,
        descending=declared.descending,
        project=project,
    )


def base(user: Any, component: Component, place: Place) -> search.Search:  # noqa: ANN401 - User
    """The search that fills the place, before what the component asks for. Raises
    :class:`NotFound` for a place the caller can't read, :class:`Refused` for one the
    component doesn't show in."""
    entities = component.entities
    me = str(user.pk)
    if place.entity is not None:
        entity = access.visible(user).filter(pk=place.entity).first()
        if entity is None:
            raise NotFound
        slot = tab_for(component, entity.type)
        if slot is None:
            raise Refused(
                "not_shown_here",
                f"{component.qualified_name} isn't a tab on {entity.type}’s pages.",
            )
        if entities is None or slot.search is None:
            raise Refused(
                "not_shown_here",
                f"{component.qualified_name} shows only the entity whose page it’s on.",
            )
        # The tab's page is its project's record, so it shows what's in that project.
        return _declared(
            component,
            slot,
            {"entity": str(entity.pk), "me": me},
            project=entity.owner_project_id,
        )
    if entities is None:
        raise Refused(
            "not_shown_here",
            f"{component.qualified_name} shows only the entity whose page it’s on.",
        )
    if place.search is not None:
        wanted = place.search
        if wanted.type is None or wanted.type not in subtypes(entities.type):
            raise Refused(
                "not_shown_here",
                f"{component.qualified_name} shows {entities.type}, so it shows a search of "
                "that type, or a type that extends it.",
                [("#/search/type", f"must be {entities.type}, or a type that extends it.")],
            )
        return wanted
    if place.project is not None:
        slot = _slot(component, "project.page")
        if slot is None:
            raise Refused(
                "not_shown_here", f"{component.qualified_name} isn't shown on projects’ pages."
            )
        return _declared(component, slot, {"me": me}, project=place.project.pk)
    slot = _slot(component, "person.home")
    if slot is None:
        raise Refused(
            "not_shown_here", f"{component.qualified_name} isn't shown on people’s homes."
        )
    return _declared(component, slot, {"me": me})


def _leaves(node: Any, pointer: str, depth: int = 1) -> Iterator[tuple[str, Any, int]]:  # noqa: ANN401
    """Each condition's field (with where it is, and how deep): groups' too, so shapes the
    search would refuse are refused there, with their pointers."""
    if not isinstance(node, Mapping):
        yield pointer, None, depth
        return
    for key in ("all", "any"):
        if isinstance(node.get(key), list):
            for index, child in enumerate(node[key]):
                yield from _leaves(child, f"{pointer}/{key}/{index}", depth + 1)
            return
    if "not" in node:
        yield from _leaves(node["not"], f"{pointer}/not", depth + 1)
        return
    yield pointer, node.get("field"), depth


def refined(
    component: Component, place: search.Search, refinement: Refinement | None
) -> tuple[search.Search, int | None]:
    """The place's search, narrowed by what the component asked for, and where in its
    conditions the refinement's are (``#/where/all/<index>``). Raises :class:`Refused`."""
    if refinement is None:
        return place, None
    assert component.entities is not None  # noqa: S101 - only places with entities
    readable = {*component.entities.fields, *REFINABLE_COLUMNS}
    errors: list[tuple[str, str]] = []
    if refinement.where is not None:
        leaves = list(_leaves(refinement.where, "#/refine/where"))
        if len(leaves) > MAX_CONDITIONS:
            errors.append(
                (
                    "#/refine/where",
                    f"has {len(leaves)} conditions: at most {MAX_CONDITIONS}.",
                )
            )
        for pointer, field, depth in leaves:
            if depth > MAX_DEPTH:
                errors.append((pointer, f"nests too deeply: at most {MAX_DEPTH} levels."))
            elif isinstance(field, str) and field not in readable:
                errors.append(
                    (f"{pointer}/field", f"`{field}` isn't a field {component.label} reads.")
                )
    if refinement.sort is not None and refinement.sort not in readable:
        errors.append(
            ("#/refine/sort/field", f"`{refinement.sort}` isn't a field {component.label} reads.")
        )
    if refinement.q and place.q:
        errors.append(("#/refine/q", "The search it shows has text already."))
    if errors:
        raise Refused("validation_failed", "What the component asked for is refused.", errors)
    where: Mapping[str, Any] | None = place.where
    index = None
    if refinement.where is not None:
        parts = [place.where] if place.where is not None else []
        index = len(parts)
        # Added with `all`, so it can only narrow what the place shows.
        where = {"all": [*parts, refinement.where]}
    sort = place.sort
    descending = place.descending
    if refinement.sort is not None:
        sort, descending = refinement.sort, refinement.descending
    return (
        dataclasses.replace(
            place,
            where=where,
            sort=sort,
            descending=descending,
            q=refinement.q if refinement.q else place.q,
        ),
        index,
    )


def found(
    user: Any,  # noqa: ANN401 - User
    component: Component,
    place: Place,
    refinement: Refinement | None,
) -> tuple[search.Search, QuerySet[Entity]]:
    """The search that fills the place as the component asked, and what it finds. Raises
    :class:`NotFound`, :class:`Refused`, :class:`Misdeclared`, or the search's errors."""
    wanted = base(user, component, place)
    try:
        search.queryset(user, wanted)
    except search.SearchError as error:
        if place.search is not None:
            raise Refused(
                "validation_failed",
                "This search is invalid.",
                [(p.replace("#/", "#/search/", 1), d) for p, d in error.errors],
            ) from error
        # The plugin's declaration, which its checks couldn't see working.
        logger.error(
            "component_search_invalid", component=component.qualified_name, errors=error.errors
        )
        raise Misdeclared(component.qualified_name) from error
    narrowed, index = refined(component, wanted, refinement)
    try:
        return narrowed, search.queryset(user, narrowed)
    except search.SearchError as error:
        mine = f"#/where/all/{index}"
        errors = [
            (
                p.replace(mine, "#/refine/where", 1)
                if index is not None and p.startswith(mine)
                else p.replace("#/sort", "#/refine/sort/field", 1),
                d,
            )
            for p, d in error.errors
        ]
        raise Refused(
            "validation_failed", "What the component asked for is refused.", errors
        ) from error

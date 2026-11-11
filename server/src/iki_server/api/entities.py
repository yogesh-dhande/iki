"""Entities of every type, through one typed API (ADR 0035).

- ``/entity-types`` lists the installed types, each with a JSON Schema of
  its values.
- ``/entities`` lists, creates, reads and changes entities. An entity is
  addressed by its ID or its HRID (``DNA-000042``).
- Every read carries an ``ETag``, the entity's revision. A change must send
  it back as ``If-Match``. If someone else changed the entity since, the
  change is refused (412) rather than overwriting theirs. A change without
  ``If-Match`` is refused too (428).
- Entities you can't read answer 404, as if they didn't exist.
"""

import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from ninja import Field, HeaderEx, P, Query, Router, Schema, Status
from pydantic import ConfigDict

from iki_server.accounts.models import User
from iki_server.api.common import caller, project, require
from iki_server.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, ProjectRef, UserOut
from iki_server.entities import access, registry, schema, search, services
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.entities.values import Invalid, shape, subtypes, to_json
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.runs.models import Lineage, RunEffect
from iki_server.runs.services import visible_runs
from iki_spec import titles, units
from iki_spec.model import EntityType
from iki_spec.model import Field as SpecField

router = Router(tags=["entities"])

DuplicatePolicy = Literal["reuse", "warn", "allow"]


# --- Schemas -------------------------------------------------------------------------------------


class UnitOption(Schema):
    code: str = Field(description="UCUM, as the API takes it.")
    symbol: str = Field(description="As people write it.")


class FieldOut(Schema):
    name: str
    label: str
    description: str | None
    type: str
    plugin: str = Field(description="The plugin that defines the field.")
    required: bool
    immutable: bool = Field(description="Can be set once, and can't change after.")
    computed: bool = Field(description="Set only by actions.")
    unit: str | None = Field(description="Quantities: the UCUM unit values are in.")
    unit_symbol: str | None = Field(description="The unit as people write it, such as Å for Ao.")
    display_unit: str | None
    display_unit_symbol: str | None
    display_scale: float | None = Field(
        description="With a display unit: shown as value * display_scale + display_offset."
    )
    display_offset: float | None
    input_units: list[UnitOption] = Field(
        description="Quantities: common units values can be given in. Any convertible UCUM "
        "unit works."
    )
    to: str | None = Field(description="References: the type they refer to.")
    values: list[str] = Field(description="Enums: the allowed values.")
    items: str | None = Field(description="Lists: the type of each item.")


class EntityTypeOut(Schema):
    name: str = Field(description="Qualified: plugin.Type.")
    plugin: str
    label: str
    label_plural: str
    description: str | None
    extends: str | None
    abstract: bool = Field(description="Abstract types have no entities of their own.")
    subtypes: list[str] = Field(description="The types that extend it, at any depth.")
    hrid_prefix: str | None
    identity: list[str] = Field(description="The fields that tell two entities apart.")
    on_duplicate: DuplicatePolicy | None = Field(
        description="What creating an entity with the same identity as another does."
    )
    fields: list[FieldOut]
    values_schema: dict[str, Any] = Field(
        description="A JSON Schema (2020-12) of the entity's values."
    )


class EntityCan(Schema):
    edit: bool = Field(description="Change it, archive or restore it.")


class RefOut(Schema):
    hrid: str
    title: str = Field(description="What it's called: its type's display title, or its HRID.")
    type: str
    archived: bool


class FileRef(Schema):
    name: str
    media_type: str
    size: int = Field(description="Bytes.")


class EntityOut(Schema):
    id: UUID
    hrid: str
    title: str = Field(description="What it's called: its type's display title, or its HRID.")
    type: str
    revision: int = Field(description="Its latest revision. Send it back as If-Match to change it.")
    owner_project: ProjectRef | None = Field(
        description="The project that owns it, and decides its changes. Null if you can't see it."
    )
    created_by: UserOut | None
    created_at: datetime
    updated_at: datetime
    archived: bool
    archived_at: datetime | None
    values: dict[str, Any] = Field(description="Its fields' values, as its type's schema says.")
    refs: dict[str, RefOut] = Field(
        description="The entities its values refer to that you can read, by ID."
    )
    files: dict[str, FileRef] = Field(
        description="The files its values refer to that you can read, by ID."
    )
    can: EntityCan


class CreatedOut(EntityOut):
    reused: bool = Field(
        description="An entity with the same identity existed, and is returned instead."
    )
    linked: bool = Field(description="The existing entity was added to the project.")


class EntityIn(Schema):
    type: str = Field(description="Qualified, such as dna.DNASequence.")
    project: UUID = Field(description="The project it belongs to. It needs you as an Editor.")
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field("", max_length=2000, description="Why, for the record.")
    allow_duplicate: bool = Field(
        False, description="Create it even if its type warns about a duplicate."
    )


class EntityPatch(Schema):
    values: dict[str, Any] | None = Field(None, description="Only the fields to change.")
    archived: bool | None = Field(
        None, description="Archived entities can't change: restore one to change it."
    )
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class RunRefOut(Schema):
    id: UUID
    hrid: str
    action: str


class RevisionOut(Schema):
    number: int
    kind: Literal["created", "edited", "archived", "restored"]
    changed_fields: list[str]
    user: UserOut | None = Field(description="Who, or for a run, who launched it.")
    via: str = Field(description="How: web, api, mcp, run, …")
    client: str = Field(
        description="The OAuth app it came through, such as an AI agent's MCP client, if any."
    )
    run: RunRefOut | None = Field(
        None,
        description="The run that made it, when `via` is `run` and you can read the run's project.",
    )
    reason: str
    created_at: datetime


class ChangeOut(Schema):
    before: Any = None
    after: Any = None


class RevisionDetailOut(RevisionOut):
    values: dict[str, Any] = Field(description="Every field's value as it was at this revision.")
    changes: dict[str, ChangeOut] = Field(description="What this revision changed.")
    refs: dict[str, RefOut] = Field(
        description="The entities its values and changes refer to that you can read, by ID."
    )
    files: dict[str, FileRef] = Field(
        description="The files its values and changes refer to that you can read, by ID."
    )


class EntityProjectOut(Schema):
    project: ProjectRef
    owner: bool
    linked_by: UserOut | None
    linked_at: datetime


class EntityFilters(PageParams):
    type: str | None = Field(None, description="This type and its subtypes.")
    exact_type: bool = Field(False, description="Only the type itself, not its subtypes.")
    project: UUID | None = Field(None, description="Entities that belong to this project.")
    archived: bool | None = None
    q: str | None = Field(
        None,
        max_length=200,
        description="Entities whose HRID, or a text field in their title, contains this.",
    )
    collection: str | None = Field(
        None,
        max_length=100,
        description="Members of this collection (its ID or HRID): those it lists, or those its "
        "search finds, as you can see them.",
    )


OPERATOR = Literal[
    "is_set",
    "eq",
    "ne",
    "in",
    "lt",
    "lte",
    "gt",
    "gte",
    "contains",
    "starts_with",
    "matches",
    "has",
]


class Condition(Schema):
    """A condition on a field, or a group of them: exactly one of ``field`` (with ``op`` and
    ``value``), ``all``, ``any`` or ``not``."""

    model_config = ConfigDict(extra="forbid")

    field: str | None = Field(None, description="A field of the type, or of every entity.")
    op: OPERATOR | None = None
    value: Any = Field(None, description="What to compare with, as the field takes it.")
    all: list["Condition"] | None = Field(None, description="Every one of these.")
    any: list["Condition"] | None = Field(None, description="At least one of these.")
    not_: "Condition | None" = Field(None, alias="not", description="Not this.")


class SortIn(Schema):
    field: str = Field(description="A field to sort by: text, numbers, dates or yes/no.")
    descending: bool = False


class SearchIn(Schema):
    type: str | None = Field(None, description="This type and its subtypes.")
    exact_type: bool = Field(False, description="Only the type itself, not its subtypes.")
    project: UUID | None = Field(None, description="Entities that belong to this project.")
    archived: bool | None = None
    q: str | None = Field(
        None, max_length=200, description="Text in their HRID, or a text field in their title."
    )
    collection: str | None = Field(
        None,
        max_length=100,
        description="Members of this collection (its ID or HRID): those it lists, or those its "
        "search finds, as you can see them.",
    )
    where: Condition | None = None
    sort: SortIn | None = None
    cursor: str | None = Field(None, description="From the previous page's next_cursor.")
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Items per page.")


# --- Entity types --------------------------------------------------------------------------------


def _types() -> dict[str, EntityType]:
    return {n: t for n, t in registry.types().items() if n != "core.Entity"}


def field_out(name: str, f: SpecField, plugin: str) -> dict[str, Any]:
    """A field as `FieldOut` describes it: an entity type's, or an action's param."""
    return {
        "name": name,
        "label": f.label,
        "description": f.description,
        "type": f.type,
        "plugin": plugin,
        "required": f.required,
        "immutable": f.immutable,
        "computed": f.computed,
        "unit": f.unit,
        "unit_symbol": units.symbol(f.unit) if f.unit else None,
        "display_unit": f.display_unit,
        "display_unit_symbol": units.symbol(f.display_unit) if f.display_unit else None,
        **_display(f.unit, f.display_unit),
        "input_units": _input_units(f.unit, f.display_unit),
        "to": f.to,
        "values": list(f.values),
        "items": f.items.type if f.items is not None else None,
    }


def _type_out(entity_type: EntityType) -> dict[str, Any]:
    form = shape(entity_type.qualified_name)
    owner = registry.identity_owner(entity_type)
    fields = []
    for part in form.parts:
        for name, f in part.fields.items():
            if f.removed:
                continue
            fields.append(field_out(name, f, part.plugin))
    name = entity_type.qualified_name
    return {
        "name": name,
        "plugin": entity_type.plugin,
        "label": entity_type.label or entity_type.name,
        "label_plural": entity_type.label_plural or f"{entity_type.label or entity_type.name}s",
        "description": entity_type.description,
        "extends": entity_type.extends if entity_type.extends != "core.Entity" else None,
        "abstract": entity_type.abstract,
        "subtypes": sorted(subtypes(name) - {name}),
        "hrid_prefix": entity_type.hrid_prefix,
        "identity": list(owner.identity) if owner is not None else [],
        "on_duplicate": registry.duplicate_policy(entity_type),
        "fields": fields,
        "values_schema": schema.for_type(name),
    }


def _input_units(unit: str | None, display_unit: str | None) -> list[dict[str, str]]:
    if unit is None:
        return []
    """The display unit first (what people read values in), then the field's own."""
    codes = list(units.common_alternatives(unit))
    if display_unit:
        codes = [display_unit, *(c for c in codes if c != display_unit)]
    return [{"code": c, "symbol": units.symbol(c)} for c in codes]


def _display(unit: str | None, display_unit: str | None) -> dict[str, float | None]:
    """How to show a quantity in its display unit, when that's a scale and an offset.

    Pages convert without a units table of their own. Units that don't convert
    linearly (degrees of slope, say) are shown in the field's own unit.
    """
    none: dict[str, float | None] = {"display_scale": None, "display_offset": None}
    if unit is None or display_unit is None or display_unit == unit:
        return none
    try:
        at = [units.convert(x, unit, display_unit) for x in (0.0, 1.0, 2.0)]
    except units.UnitError:
        return none
    scale, offset = at[1] - at[0], at[0]
    if not math.isclose(at[2], 2 * scale + offset, rel_tol=1e-9, abs_tol=1e-12):
        return none
    return {"display_scale": scale, "display_offset": offset}


@router.get("/entity-types", response=list[EntityTypeOut], summary="The installed entity types")
def list_types(request: HttpRequest) -> list[dict[str, Any]]:
    """Every type the deployment's plugins define, abstract ones included, by name."""
    caller(request)
    return [_type_out(t) for _, t in sorted(_types().items())]


@router.get("/entity-types/{name}", response=EntityTypeOut, summary="An entity type")
def get_type(request: HttpRequest, name: str) -> dict[str, Any]:
    caller(request)
    found = _types().get(name)
    if found is None:
        raise Problem(404, f"There’s no entity type {name}.")
    return _type_out(found)


# --- Reading -------------------------------------------------------------------------------------


def _find(user: User, ref: str) -> Entity:
    """The entity, by ID or HRID, if the user can read it. Otherwise 404."""
    readable = access.visible(user).select_related("created_by")
    try:
        found = readable.filter(pk=UUID(ref)).first()
    except ValueError:
        found = readable.filter(hrid=ref.upper()).first()
    if found is None:
        raise Problem(404, "No such entity, or you can’t read it.")
    return found


def _out(user: User, entities: list[Entity]) -> list[dict[str, Any]]:
    values = services.current_values_of(entities)
    projects = {
        p.pk: p
        for p in permissions.visible_projects(user).filter(
            pk__in={e.owner_project_id for e in entities}
        )
    }
    editable = {p.pk for p in projects.values() if permissions.can(user, Action.PROJECT_EDIT, p)}
    refs, files = _referenced(user, entities, values)
    out = []
    for entity in entities:
        fields = shape(entity.type).fields()
        mine = _ids_in(fields, values[entity.pk])
        shown = {name: to_json(fields[name], value) for name, value in values[entity.pk].items()}
        out.append(
            {
                "id": entity.pk,
                "hrid": entity.hrid,
                "title": _title(entity, shown),
                "type": entity.type,
                "revision": entity.revision,
                "owner_project": projects.get(entity.owner_project_id),
                "created_by": entity.created_by,
                "created_at": entity.created_at,
                "updated_at": entity.updated_at,
                "archived": entity.archived_at is not None,
                "archived_at": entity.archived_at,
                "values": shown,
                "refs": {str(i): refs[i] for i in mine["ref"] if i in refs},
                "files": {str(i): files[i] for i in mine["file"] if i in files},
                "can": {"edit": entity.owner_project_id in editable},
            }
        )
    return out


def _ids_in(fields: dict[str, Any], values: dict[str, Any]) -> dict[str, set[Any]]:
    """The entity and file IDs these values refer to."""
    found: dict[str, set[Any]] = {"ref": set(), "file": set()}
    for name, value in values.items():
        f = fields[name]
        kind = f.items.type if f.type == "list" and f.items is not None else f.type
        if kind in found and value is not None:
            found[kind].update(value if isinstance(value, list) else [value])
    return found


def _referenced(
    user: User, entities: list[Entity], values: dict[Any, dict[str, Any]]
) -> tuple[dict[Any, dict[str, Any]], dict[Any, dict[str, Any]]]:
    """What a page of entities refers to that the user can read, in a few queries."""
    wanted: dict[str, set[Any]] = {"ref": set(), "file": set()}
    for entity in entities:
        for kind, ids in _ids_in(shape(entity.type).fields(), values[entity.pk]).items():
            wanted[kind] |= ids
    return _lookup(user, wanted)


def _lookup(
    user: User, wanted: dict[str, set[Any]]
) -> tuple[dict[Any, dict[str, Any]], dict[Any, dict[str, Any]]]:
    """The entities and files with these IDs that the user can read, as ``refs`` and ``files``."""
    found = list(access.visible(user).filter(pk__in=wanted["ref"])) if wanted["ref"] else []
    titled = [e for e in found if _template(e.type) is not None]
    titled_values = services.current_values_of(titled) if titled else {}
    refs = {}
    for e in found:
        fields = shape(e.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in titled_values.get(e.pk, {}).items()}
        refs[e.pk] = {
            "hrid": e.hrid,
            "title": _title(e, shown),
            "type": e.type,
            "archived": e.archived_at is not None,
        }
    files = (
        {
            pk: {"name": name, "media_type": media_type, "size": size}
            for pk, name, media_type, size in access.readable_files(user)
            .filter(pk__in=wanted["file"])
            .values_list("pk", "name", "media_type", "blob__size")
        }
        if wanted["file"]
        else {}
    )
    return refs, files


def _template(type_name: str) -> str | None:
    entity_type = registry.types().get(type_name)
    return registry.display_title(entity_type) if entity_type is not None else None


def _title(entity: Entity, values: dict[str, Any]) -> str:
    """Its display title, from its values as JSON."""
    return titles.render(_template(entity.type), values, entity.hrid, shape(entity.type).fields())


def _etag(entity: Entity) -> str:
    return f'"{entity.revision}"'


def _one(user: User, entity: Entity, response: HttpResponse) -> dict[str, Any]:
    response["ETag"] = _etag(entity)
    return _out(user, [entity])[0]


@router.get("/entities", response=Page[EntityOut], summary="Entities you can read")
def list_entities(request: HttpRequest, filters: Query[EntityFilters]) -> dict[str, Any]:
    """Oldest first. Filter by type to get one type and its subtypes. For conditions on
    fields, and sorting, use ``POST /entities/search``."""
    me = caller(request)
    if filters.project:
        project(me, filters.project)
    wanted = search.Search(
        type=filters.type,
        exact_type=filters.exact_type,
        project=filters.project,
        archived=filters.archived,
        q=filters.q,
        collection=filters.collection,
    )
    with _searching(parameters=True):
        result = page(search.queryset(me, wanted), filters)
        result["items"] = _out(me, result["items"])
    return result


@router.post("/entities/search", response=Page[EntityOut], summary="Search entities")
def search_entities(request: HttpRequest, payload: SearchIn) -> dict[str, Any]:
    """Entities you can read that match every filter given: a type (with its subtypes), a
    project, archived or not, text in their HRID or title (``q``), membership of a
    ``collection``, and ``where``, conditions on the type's fields. Sorted by ``sort``
    (then by ID), oldest first without one.

    Each kind of field takes its own operators, and every field takes ``is_set``:

    - string: ``eq``, ``ne``, ``in``, ``contains``, ``starts_with`` (any case)
    - text: ``contains``, ``matches`` (its words: ``"kinase -fragment"``, ``"\"active site\""``)
    - enum, reference: ``eq``, ``ne``, ``in`` (references by ID or HRID)
    - int, float, quantity, date, datetime: ``eq``, ``ne``, ``in``, ``lt``, ``lte``, ``gt``,
      ``gte`` (quantities in the field's unit, or ``{"value": 50, "unit": "kDa"}``)
    - bool: ``eq``; file: ``eq``, ``ne``; list: ``has``

    ``ne`` matches entities without a value too. Every entity also has ``hrid``, ``type``,
    ``owner_project``, ``created_by``, ``created_at``, ``updated_at`` and ``archived_at``.
    A search may run for 15 seconds: conditions on indexed fields keep it fast.
    """
    me = caller(request)
    if payload.project:
        project(me, payload.project)
    wanted = search.Search(
        type=payload.type,
        exact_type=payload.exact_type,
        project=payload.project,
        archived=payload.archived,
        q=payload.q,
        collection=payload.collection,
        where=payload.where.model_dump(by_alias=True, exclude_unset=True)
        if payload.where
        else None,
        sort=payload.sort.field if payload.sort else None,
        descending=payload.sort.descending if payload.sort else False,
    )
    with _searching():
        rows, cursor = search.page(
            search.queryset(me, wanted), wanted, payload.cursor, payload.limit
        )
        return {"items": _out(me, rows), "next_cursor": cursor}


@contextmanager
def _searching(*, parameters: bool = False) -> Iterator[None]:
    """Runs a search with its time limit, answering for its mistakes."""
    try:
        with search.time_limit():
            yield
    except search.SearchError as error:
        raise Problem(
            422,
            "Some fields are invalid." if parameters else "This search is invalid.",
            code="validation_failed",
            errors=[
                {"parameter": pointer.removeprefix("#/"), "detail": detail}
                if parameters
                else {"pointer": pointer, "detail": detail}
                for pointer, detail in error.errors
            ],
        ) from error
    except search.TooSlow as error:
        raise Problem(
            422,
            "This search took too long. Narrow it with conditions on indexed fields, or sort "
            "by an indexed field.",
            code="search_too_slow",
        ) from error


@router.get(
    "/entities/{ref}",
    response={200: EntityOut, 304: None},
    summary="An entity, by ID or HRID",
)
def get_entity(
    request: HttpRequest,
    ref: str,
    response: HttpResponse,
    if_none_match: HeaderEx[str | None, P(alias="If-None-Match")] = None,
) -> Any:  # noqa: ANN401 - the entity, or 304
    """Send ``If-None-Match`` with the ETag you have to get 304 if it hasn't changed."""
    me = caller(request)
    entity = _find(me, ref)
    if if_none_match is not None and _etag(entity) in _etags(if_none_match):
        unchanged = HttpResponse(status=304)  # no body, as RFC 9110 says
        unchanged["ETag"] = _etag(entity)
        return unchanged
    return _one(me, entity, response)


def _etags(header: str) -> set[str]:
    return {tag.strip().removeprefix("W/") for tag in header.split(",")}


# --- Changing ------------------------------------------------------------------------------------


def _invalid(error: Invalid) -> Problem:
    return Problem(
        422,
        "Some values are invalid.",
        errors=[
            {"pointer": f"#/values/{name}", "detail": " ".join(messages)}
            if name != "type"
            else {"pointer": "#/type", "detail": " ".join(messages)}
            for name, messages in error.errors.items()
        ],
    )


@router.post(
    "/entities",
    response={201: CreatedOut, 200: CreatedOut},
    summary="Create an entity",
)
def create_entity(request: HttpRequest, payload: EntityIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """If its type reuses duplicates and you can read one with the same identity, that one
    is returned (200, ``reused``) and added to the project, instead of a copy. If its type
    warns about duplicates, the request is refused (409, ``duplicate``) unless
    ``allow_duplicate`` is set."""
    me = caller(request)
    target = project(me, payload.project, Action.PROJECT_EDIT)
    try:
        saved = services.create(
            me,
            payload.type,
            target,
            payload.values,
            reason=payload.reason,
            allow_duplicate=payload.allow_duplicate,
        )
    except Invalid as error:
        raise _invalid(error) from None
    except services.Duplicate as error:
        raise Problem(
            409,
            str(error),
            code="duplicate",
            errors=[
                {"pointer": "#/values", "detail": f"The same as {e.hrid}.", "entity": str(e.pk)}
                for e in error.existing
            ],
        ) from None
    entity = Entity.objects.select_related("created_by").get(pk=saved.entity.pk)
    out = {**_one(me, entity, response), "reused": saved.reused, "linked": saved.linked}
    response["Location"] = f"/api/v1/entities/{entity.pk}"
    return Status(200 if saved.reused else 201, out)


def _expected(if_match: str | None) -> int:
    if if_match is None:
        raise Problem(
            428,
            "Send If-Match with the ETag you read, so you can’t overwrite someone else’s change.",
            code="precondition_required",
        )
    tags = _etags(if_match)
    numbers = [int(t.strip('"')) for t in tags if t.strip('"').isdigit() and t.startswith('"')]
    if len(numbers) != 1 or len(tags) != 1:
        raise Problem(
            412,
            'If-Match must be the one ETag you read, such as "3".',
            code="precondition_failed",
        )
    return numbers[0]


@router.patch("/entities/{ref}", response=EntityOut, summary="Change, archive or restore")
def patch_entity(
    request: HttpRequest,
    ref: str,
    payload: EntityPatch,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Send only the values to change. Each change is a revision, with your reason.
    To restore an archived entity and change it, send both: it's restored first."""
    me = caller(request)
    entity = _find(me, ref)
    owner = Project.objects.select_related("team").get(pk=entity.owner_project_id)
    require(me, Action.PROJECT_EDIT, owner)
    expected = _expected(if_match)
    try:
        with transaction.atomic():
            # Locked for the whole request, so its steps can't interleave with another's.
            locked = Entity.objects.select_for_update().get(pk=entity.pk)
            if locked.revision != expected:
                raise services.Stale(locked.revision)
            if payload.archived is False:
                services.set_archived(me, locked, False, reason=payload.reason)
            if payload.values:
                services.update(
                    me,
                    locked,
                    payload.values,
                    expected_revision=_current(locked),
                    reason=payload.reason,
                )
            if payload.archived is True:
                services.set_archived(me, locked, True, reason=payload.reason)
    except services.Stale as stale:
        raise Problem(
            412,
            f"Someone changed it since you read it: it’s at revision {stale.current} now. "
            "Read it again, then make your change.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    return _one(me, _find(me, str(entity.pk)), response)


def _current(entity: Entity) -> int:
    return Entity.objects.values_list("revision", flat=True).get(pk=entity.pk)


# --- History -------------------------------------------------------------------------------------


@router.get(
    "/entities/{ref}/revisions", response=Page[RevisionOut], summary="An entity's revisions"
)
def list_revisions(request: HttpRequest, ref: str, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first: who changed what, how, when and why."""
    me = caller(request)
    entity = _find(me, ref)
    revisions = Revision.objects.filter(entity=entity).select_related("user")
    result = page(revisions, params, key="number", newest_first=True)
    made = _runs_of(me, entity, result["items"])
    result["items"] = [_revision(r, made) for r in result["items"]]
    return result


@router.get(
    "/entities/{ref}/revisions/{number}",
    response=RevisionDetailOut,
    summary="An entity as it was at a revision",
)
def get_revision(request: HttpRequest, ref: str, number: int) -> dict[str, Any]:
    me = caller(request)
    entity = _find(me, ref)
    revision = Revision.objects.filter(entity=entity, number=number).select_related("user").first()
    if revision is None:
        raise Problem(404, f"{entity.hrid} has no revision {number}.")
    fields = shape(entity.type).fields()
    values = {n: v for n, v in services.values_at(entity, number).items() if n in fields}
    changes = {n: c for n, c in services.changes(entity, revision).items() if n in fields}
    wanted = _ids_in(fields, values)
    for name, (before, after) in changes.items():
        for kind, ids in _ids_in(fields, {name: before}).items():
            wanted[kind] |= ids
        for kind, ids in _ids_in(fields, {name: after}).items():
            wanted[kind] |= ids
    refs, files = _lookup(me, wanted)
    return {
        **_revision(revision, _runs_of(me, entity, [revision])),
        "values": {n: to_json(fields[n], v) for n, v in values.items()},
        "changes": {
            name: {"before": to_json(fields[name], before), "after": to_json(fields[name], after)}
            for name, (before, after) in changes.items()
        },
        "refs": {str(i): r for i, r in refs.items()},
        "files": {str(i): f for i, f in files.items()},
    }


def _runs_of(me: User, entity: Entity, revisions: list[Revision]) -> dict[int, dict[str, Any]]:
    """The runs that made these revisions, by revision number, if the caller can read them."""
    numbers = [r.number for r in revisions if r.via == "run"]
    if not numbers:
        return {}
    effects = RunEffect.objects.filter(
        entity=entity, revision__in=numbers, run__in=visible_runs(me)
    ).select_related("run")
    return {
        e.revision: {"id": e.run.pk, "hrid": e.run.hrid, "action": e.run.action}
        for e in effects
        if e.revision is not None
    }


def _revision(revision: Revision, runs: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "number": revision.number,
        "kind": revision.kind,
        "changed_fields": revision.changed_fields,
        "user": revision.user,
        "via": revision.via,
        "client": revision.client,
        "run": runs.get(revision.number),
        "reason": revision.reason,
        "created_at": revision.created_at,
    }


@router.get(
    "/entities/{ref}/provenance",
    response=dict[str, RevisionOut],
    summary="Where each value came from",
)
def get_provenance(request: HttpRequest, ref: str) -> dict[str, dict[str, Any]]:
    """For each field, the revision that set its current value: who or what, how and when."""
    me = caller(request)
    entity = _find(me, ref)
    fields = shape(entity.type).fields()
    found = {n: r for n, r in services.provenance(entity).items() if n in fields}
    made = _runs_of(me, entity, list(found.values()))
    return {name: _revision(revision, made) for name, revision in found.items()}


# --- Lineage -------------------------------------------------------------------------------------


class LineageEntityOut(Schema):
    id: UUID
    hrid: str
    type: str
    archived: bool


class LineageLinkOut(Schema):
    entity: LineageEntityOut
    revision: int = Field(description="The parent's revision the run read.")
    input: str = Field(description="The run's input it was given as.")
    run: RunRefOut | None = Field(description="The run, if you can read its project.")


class LineageOut(Schema):
    made_from: list[LineageLinkOut] = Field(description="What runs made it from.")
    used_to_make: list[LineageLinkOut] = Field(
        description="What runs made from it: the newest 100."
    )
    more: bool = Field(description="Runs made more from it than are listed.")


@router.get("/entities/{ref}/lineage", response=LineageOut, summary="What it was made from")
def get_lineage(request: HttpRequest, ref: str) -> dict[str, Any]:
    """One step each way: the entities runs made it from (at the revisions they read), and
    those they made from it. Only entities you can read are listed."""
    me = caller(request)
    entity = _find(me, ref)
    readable = access.visible(me).values("pk")
    runs = set(visible_runs(me).values_list("pk", flat=True))

    def link(row: Lineage, other: Entity) -> dict[str, Any]:
        return {
            "entity": {
                "id": other.pk,
                "hrid": other.hrid,
                "type": other.type,
                "archived": other.archived_at is not None,
            },
            "revision": row.parent_revision,
            "input": row.input,
            "run": (
                {"id": row.run.pk, "hrid": row.run.hrid, "action": row.run.action}
                if row.run_id in runs
                else None
            ),
        }

    parents = (
        Lineage.objects.filter(entity=entity, parent__in=readable)
        .select_related("parent", "run")
        .order_by("id")
    )
    children = (
        Lineage.objects.filter(parent=entity, entity__in=readable)
        .select_related("entity", "run")
        .order_by("-id")[: MAX_LIMIT + 1]
    )
    made = list(children)
    return {
        "made_from": [link(row, row.parent) for row in parents],
        "used_to_make": [link(row, row.entity) for row in made[:MAX_LIMIT]],
        "more": len(made) > MAX_LIMIT,
    }


#: How many generations a lineage graph goes each way, at most.
MAX_GENERATIONS = 10
#: How big a lineage graph gets before it stops and says it's cut short.
MAX_GRAPH_ENTITIES = 200
MAX_GRAPH_LINKS = 500


class LineageGraphParams(Schema):
    up: int = Field(3, ge=0, le=MAX_GENERATIONS, description="How many generations back.")
    down: int = Field(3, ge=0, le=MAX_GENERATIONS, description="How many generations on.")


class LineageNodeOut(RefOut):
    id: UUID


class LineageRunOut(Schema):
    key: str = Field(description="How links refer to it: its HRID, or a key for this response.")
    run: RunRefOut | None = Field(description="The run, if you can read its project.")


class LineageGraphLinkOut(Schema):
    parent: UUID = Field(description="The entity it was made from.")
    entity: UUID = Field(description="The entity the run made.")
    run: str = Field(description="The run's key, in runs.")
    input: str = Field(description="The run's input the parent was given as.")
    revision: int = Field(description="The parent's revision the run read.")


class LineageGraphOut(Schema):
    root: UUID = Field(description="The entity it's the lineage of.")
    entities: list[LineageNodeOut] = Field(description="The entities in it that you can read.")
    runs: list[LineageRunOut]
    links: list[LineageGraphLinkOut]
    earlier: bool = Field(description="There are more generations back than were asked for.")
    later: bool = Field(description="There are more generations on than were asked for.")
    capped: bool = Field(
        description=f"It stopped at {MAX_GRAPH_ENTITIES} entities or {MAX_GRAPH_LINKS} links, "
        "so some within the generations asked for are left out."
    )


@router.get(
    "/entities/{ref}/lineage/graph",
    response=LineageGraphOut,
    summary="Its ancestors and descendants",
)
def get_lineage_graph(
    request: HttpRequest, ref: str, params: Query[LineageGraphParams]
) -> dict[str, Any]:
    """What it was made from, what that was made from and so on, ``up`` generations back;
    and what was made from it, ``down`` generations on. Only entities you can read are in
    it, and only runs whose project you can read are named."""
    me = caller(request)
    root = _find(me, ref)
    readable = access.visible(me).values("pk")
    seen: set[Any] = {root.pk}
    rows: dict[int, Lineage] = {}
    capped = False
    more = {True: False, False: False}
    for back, generations in ((True, params.up), (False, params.down)):
        near, far = ("entity", "parent") if back else ("parent", "entity")
        frontier: set[Any] = {root.pk}
        for _ in range(generations):
            room = MAX_GRAPH_LINKS - len(rows)
            found = list(
                Lineage.objects.filter(**{f"{near}__in": frontier, f"{far}__in": readable})
                .exclude(pk__in=rows.keys())
                .select_related("run")
                .order_by("id")[: room + 1]
            )
            frontier = set()
            for row in found[:room]:
                other = getattr(row, f"{far}_id")
                if other not in seen and len(seen) >= MAX_GRAPH_ENTITIES:
                    capped = True
                    continue
                if other not in seen:
                    seen.add(other)
                    frontier.add(other)
                rows[row.pk] = row
            capped = capped or len(found) > room
            if capped or not frontier:
                break
        else:
            more[back] = bool(frontier) and (
                Lineage.objects.filter(**{f"{near}__in": frontier, f"{far}__in": readable})
                .exclude(pk__in=rows.keys())
                .exists()
            )
    refs, _ = _lookup(me, {"ref": seen, "file": set()})
    links = sorted(rows.values(), key=lambda r: r.pk)
    named = set(
        visible_runs(me).filter(pk__in={r.run_id for r in links}).values_list("pk", flat=True)
    )
    keys: dict[Any, str] = {}
    runs: list[dict[str, Any]] = []
    for row in links:
        if row.run_id in keys:
            continue
        if row.run_id in named:
            keys[row.run_id] = row.run.hrid
            runs.append(
                {
                    "key": row.run.hrid,
                    "run": {"id": row.run.pk, "hrid": row.run.hrid, "action": row.run.action},
                }
            )
        else:
            keys[row.run_id] = f"hidden-{sum(r['run'] is None for r in runs) + 1}"
            runs.append({"key": keys[row.run_id], "run": None})
    return {
        "root": root.pk,
        "entities": sorted(
            ({"id": pk, **refs[pk]} for pk in seen if pk in refs), key=lambda e: e["hrid"]
        ),
        "runs": runs,
        "links": [
            {
                "parent": r.parent_id,
                "entity": r.entity_id,
                "run": keys[r.run_id],
                "input": r.input,
                "revision": r.parent_revision,
            }
            for r in links
        ],
        "earlier": more[True],
        "later": more[False],
        "capped": capped,
    }


# --- Projects ------------------------------------------------------------------------------------


def _link_out(link: ProjectLink, entity: Entity) -> dict[str, Any]:
    return {
        "project": link.project,
        "owner": link.project_id == entity.owner_project_id,
        "linked_by": link.linked_by,
        "linked_at": link.linked_at,
    }


@router.get(
    "/entities/{ref}/projects",
    response=list[EntityProjectOut],
    summary="The projects an entity belongs to",
)
def list_entity_projects(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """Only the projects you can see."""
    me = caller(request)
    entity = _find(me, ref)
    links = (
        ProjectLink.objects.filter(
            entity=entity, project__in=permissions.visible_projects(me).values("pk")
        )
        .select_related("project", "linked_by")
        .order_by("linked_at", "pk")
    )
    return [_link_out(link, entity) for link in links]


@router.put(
    "/entities/{ref}/projects/{project_id}",
    response={200: EntityProjectOut, 201: EntityProjectOut},
    summary="Add an entity to a project",
)
def link_entity(request: HttpRequest, ref: str, project_id: UUID) -> Any:  # noqa: ANN401
    """Its members can then read it. Only its owner project's Editors can change it.
    You need to be able to read the entity, and to be an Editor in the project."""
    me = caller(request)
    entity = _find(me, ref)
    target = project(me, project_id, Action.PROJECT_EDIT)
    created = services.link(me, entity, target)
    link = ProjectLink.objects.select_related("project", "linked_by").get(
        entity=entity, project=target
    )
    return Status(201 if created else 200, _link_out(link, entity))


@router.delete(
    "/entities/{ref}/projects/{project_id}",
    response={204: None},
    summary="Take an entity out of a project",
)
def unlink_entity(request: HttpRequest, ref: str, project_id: UUID) -> Status[None]:
    """An Editor in that project, or in the entity's owner project, can. The owner project
    itself can't be removed."""
    me = caller(request)
    entity = _find(me, ref)
    target = project(me, project_id)
    owner = Project.objects.select_related("team").get(pk=entity.owner_project_id)
    if not (
        permissions.can(me, Action.PROJECT_EDIT, target)
        or permissions.can(me, Action.PROJECT_EDIT, owner)
    ):
        require(me, Action.PROJECT_EDIT, target)
    if not services.unlink(me, entity, target):
        raise Problem(404, f"{entity.hrid} isn’t in that project.")
    return Status(204, None)

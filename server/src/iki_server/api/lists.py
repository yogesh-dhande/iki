"""Lab lists (ADR 0270): the dropdowns a lab curates, and their options.

- ``/lists``: the lists of a registry, for a project's members (``project``) or
  its curators (``registry``).
- ``/lists/{type}/options``: a list's options, in its order: the active ones,
  which a dropdown offers, or every one (``archived=true``). Records keep naming
  an option archived or superseded since, and show it as such.
- Its registry's registrars, and the list's curators, add options, change them
  (``PATCH``, with a reason), reorder the list (``PUT …/order``), archive and
  restore options, and supersede a wrong code with the right one. A project's
  Editor moves its records from a superseded option to its replacement.
- ``/registries/{id}/list-curators``: who curates each list. Org Admins appoint
  them, a list at a time.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from ninja import Field, HeaderEx, P, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api import registries as registries_api
from iki_server.api.common import caller, person, require
from iki_server.api.entities import _etag, _expected, _invalid
from iki_server.api.problems import Problem
from iki_server.api.schemas import RegistryRef, UserOut
from iki_server.entities import access, entries, lists, registry, services
from iki_server.entities.values import Invalid, shape
from iki_server.org import permissions, registries
from iki_server.org.models import ListCurator, Project, Registry
from iki_server.org.permissions import Action
from iki_spec.model import LIST_CODE, LIST_DESCRIPTION, LIST_LABEL, EntityType

router = Router(tags=["lists"])


class Scope(Schema):
    project: str | None = Field(
        None,
        description="A project you read, by ID or slug: its registry's lists, which its "
        "records pick from.",
    )
    registry: UUID | None = Field(
        None,
        description="A registry you can see, by ID: for its registrars, its lists' curators "
        "and Org Admins, who may read none of its projects.",
    )


def _registry(user: User, scope: Scope) -> Registry:
    """The registry the caller asks about: a project's they read, or one they see; else
    404."""
    if scope.project is not None:
        try:
            key: dict[str, Any] = {"pk": UUID(scope.project)}
        except ValueError:
            key = {"slug": scope.project}
        found = access.readable_projects(user).filter(**key).select_related("registry").first()
        if found is None:
            raise Problem(404, "No such project, or you can’t read it.")
        return found.registry
    if scope.registry is None:
        raise Problem(
            422,
            "Say whose lists: a project's, or a registry's.",
            code="validation_failed",
            errors=[{"parameter": "project", "detail": "Give `project` or `registry`."}],
        )
    # Whoever sees the registry reads its lists: what its projects' records pick from, which
    # says nothing of any project's work.
    return registries_api._registry(user, scope.registry)


def _list(type_name: str) -> EntityType:
    found = registry.types().get(type_name)
    if found is None or not found.is_lab_list:
        raise Problem(404, f"There’s no lab list {type_name}.")
    return found


class ListCan(Schema):
    curate: bool = Field(
        description="Add, change, reorder, archive and supersede its options: its registry's "
        "registrars, and the list's curators."
    )


class ListOut(Schema):
    type: str = Field(description="The list's type, such as iki/lots.PurityMethod.")
    plugin: str
    label: str
    label_plural: str
    description: str | None
    registry: RegistryRef
    options: int = Field(description="How many options a dropdown offers: the active ones.")
    detached: bool = Field(
        description="Its plugin is detached (ADR 0091): its options can be read, not changed."
    )
    can: ListCan


def _list_out(user: User, found: Registry, entity_type: EntityType, counts: dict[str, int]) -> Any:  # noqa: ANN401
    name = entity_type.qualified_name
    detached = services.frozen(shape(name)) is not None
    return {
        "type": name,
        "plugin": entity_type.plugin,
        "label": entity_type.label,
        "label_plural": entity_type.label_plural,
        "description": entity_type.description,
        "registry": found,
        "options": counts.get(name, 0),
        "detached": detached,
        "can": {"curate": not detached and permissions.can_curate_list(user, found, name)},
    }


@router.get("/lists", response=list[ListOut], summary="A registry's lab lists")
def list_lists(request: HttpRequest, scope: Query[Scope]) -> list[Any]:
    """Every installed list, by label, with how many options each offers in the registry."""
    me = caller(request)
    found = _registry(me, scope)
    counts = lists.counted(found.pk)
    every = sorted(registry.lab_lists().values(), key=lambda t: (t.label_plural.casefold(), t.name))
    return [_list_out(me, found, t, counts) for t in every]


@router.get("/lists/{qualified:name}", response=ListOut, summary="A lab list")
def get_list(request: HttpRequest, name: str, scope: Query[Scope]) -> Any:  # noqa: ANN401
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, scope)
    return _list_out(me, found, entity_type, lists.counted(found.pk))


class OptionReplacementOut(Schema):
    id: UUID
    hrid: str
    code: str
    label: str


class OptionOut(Schema):
    id: UUID = Field(description="What a record's reference to it holds.")
    hrid: str = Field(
        description="Its page's address, such as OPTION-000123, from the one sequence every "
        "list's options share (ADR 0295). It's shown by its label and named by its code."
    )
    code: str = Field(
        description="Its identity in the list, which never changes: what exports give for it."
    )
    label: str = Field(description="What people see and pick, as it's called now.")
    description: str
    position: int = Field(description="Its place in the list's order, from 1.")
    archived: bool = Field(description="No dropdown offers it, but the records naming it keep it.")
    superseded_by: OptionReplacementOut | None = Field(
        description="The option that replaced it: dropdowns offer that one instead."
    )
    values: dict[str, Any] = Field(
        description="Its values for the list's other fields. A reference is the ID of "
        "another list's option."
    )
    revision: int = Field(description="Its latest revision. Send it back as If-Match.")
    uses: int | None = Field(
        description="For its curators: how many records name it, in every project. Never which."
    )


class OptionFilters(Scope):
    archived: bool = Field(
        False, description="Every option, archived and superseded ones too, as curators see them."
    )
    q: str | None = Field(
        None, max_length=200, description="Only options whose label or code has this in it."
    )


def _options_out(
    user: User, found: Registry, entity_type: EntityType, chosen: list[lists.Option]
) -> list[dict[str, Any]]:
    name = entity_type.qualified_name
    used = lists.usage(found.pk, name) if permissions.can_curate_list(user, found, name) else None
    replacements = {o.superseded_by.pk for o in chosen if o.superseded_by is not None}
    replaced = (
        {o.entry.pk: o for o in lists.options(found.pk, name, archived=True, ids=replacements)}
        if replacements
        else {}
    )
    out = []
    for option in chosen:
        by = replaced.get(option.superseded_by.pk) if option.superseded_by is not None else None
        out.append(
            {
                "id": option.entry.pk,
                "hrid": option.entry.hrid,
                "code": option.code,
                "label": option.label,
                "description": option.description,
                "position": option.position,
                "archived": option.archived,
                "superseded_by": {
                    "id": by.entry.pk,
                    "hrid": by.entry.hrid,
                    "code": by.code,
                    "label": by.label,
                }
                if by is not None
                else None,
                "values": option.values,
                "revision": option.entry.revision,
                "uses": used.get(option.entry.pk, 0) if used is not None else None,
            }
        )
    return out


@router.get(
    "/lists/{qualified:name}/options", response=list[OptionOut], summary="A lab list's options"
)
def list_options(request: HttpRequest, name: str, filters: Query[OptionFilters]) -> list[Any]:
    """In the list's order. Without ``archived``, what a dropdown offers."""
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, filters)
    chosen = lists.options(found.pk, name, archived=filters.archived)
    if filters.q:
        wanted = filters.q.strip().casefold()
        chosen = [o for o in chosen if wanted in o.label.casefold() or wanted in o.code.casefold()]
    return _options_out(me, found, entity_type, chosen)


def _option(found: Registry, name: str, code: str) -> lists.Option:
    option = lists.find(found.pk, name, code)
    if option is None:
        raise Problem(404, f"{_list(name).label_plural} have no option `{code}` here.")
    return option


@router.get(
    "/lists/{qualified:name}/options/{code}", response=OptionOut, summary="One of a list's options"
)
def get_option(
    request: HttpRequest, name: str, code: str, scope: Query[Scope], response: HttpResponse
) -> Any:  # noqa: ANN401
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, scope)
    option = _option(found, name, code)
    response["ETag"] = _etag(option.entry)
    return _options_out(me, found, entity_type, [option])[0]


class OptionIn(Schema):
    code: str = Field(
        min_length=1,
        max_length=64,
        description="Its identity in the list, which never changes, such as `hplc`.",
    )
    label: str = Field(min_length=1, max_length=200, description="What people see and pick.")
    description: str = Field("", max_length=2000)
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="Its values for the list's other fields, by name. A reference is the ID "
        "of another list's option.",
    )
    reason: str = Field("", max_length=2000, description="Why it's added, if you'd say.")


def _curating(user: User, found: Registry, name: str) -> None:
    if not permissions.can_curate_list(user, found, name):
        require(user, Action.LISTS_CURATE, found)
        raise Problem(
            403,
            f"Only {found.name}'s registrars, and the curators of "
            f"{_list(name).label_plural}, curate its options.",
        )


@router.post(
    "/lists/{qualified:name}/options",
    response={201: OptionOut},
    summary="Add an option to a lab list",
)
def add_option(
    request: HttpRequest, name: str, scope: Query[Scope], payload: OptionIn
) -> Status[Any]:
    """Its registry's registrars, and the list's curators. It's added at the end of the
    list; every project in the registry can pick it."""
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, scope)
    _curating(me, found, name)
    values = {
        **payload.values,
        LIST_CODE: payload.code.strip(),
        LIST_LABEL: payload.label,
        LIST_DESCRIPTION: payload.description or None,
    }
    try:
        option = lists.add(me, found, name, values, reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return Status(201, _options_out(me, found, entity_type, [option])[0])


class OptionPatch(Schema):
    label: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000, description="Empty clears it.")
    values: dict[str, Any] = Field(
        default_factory=dict, description="Only the other fields to change; null clears one."
    )
    reason: str = Field(
        min_length=1, max_length=2000, description="Why: every project in the registry sees it."
    )


@router.patch(
    "/lists/{qualified:name}/options/{code}",
    response=OptionOut,
    summary="Change a lab list's option",
)
def patch_option(
    request: HttpRequest,
    name: str,
    code: str,
    scope: Query[Scope],
    payload: OptionPatch,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> Any:  # noqa: ANN401
    """Its label, description or other values, with a reason. Records naming it show what
    it's called now; their history says what it was called when. Its code never changes:
    supersede it with an option of the right one."""
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, scope)
    _curating(me, found, name)
    option = _option(found, name, code)
    expected = _expected(if_match)
    values = dict(payload.values)
    if payload.label is not None:
        values[LIST_LABEL] = payload.label
    if payload.description is not None:
        values[LIST_DESCRIPTION] = payload.description or None
    try:
        lists.change(me, option.entry, values, reason=payload.reason, expected_revision=expected)
    except services.Stale as stale:
        raise Problem(
            412,
            f"It changed since you read it: it’s at revision {stale.current} now.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    fresh = _option(found, name, code)
    response["ETag"] = _etag(fresh.entry)
    return _options_out(me, found, entity_type, [fresh])[0]


class Reason(Schema):
    reason: str = Field(
        min_length=1, max_length=2000, description="Why: every project in the registry sees it."
    )


@router.post(
    "/lists/{qualified:name}/options/{code}/archive",
    response=OptionOut,
    summary="Archive a lab list's option",
)
def archive_option(
    request: HttpRequest, name: str, code: str, scope: Query[Scope], payload: Reason
) -> Any:  # noqa: ANN401
    """No dropdown offers it from now on. The records that name it keep it, and it's still
    in their search, filters, exports and the API."""
    return _archive(request, name, code, scope, payload, archived=True)


@router.post(
    "/lists/{qualified:name}/options/{code}/restore",
    response=OptionOut,
    summary="Restore a lab list's archived option",
)
def restore_option(
    request: HttpRequest, name: str, code: str, scope: Query[Scope], payload: Reason
) -> Any:  # noqa: ANN401
    """Dropdowns offer it again, in its place in the list."""
    return _archive(request, name, code, scope, payload, archived=False)


def _archive(
    request: HttpRequest, name: str, code: str, scope: Scope, payload: Reason, *, archived: bool
) -> Any:  # noqa: ANN401
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, scope)
    _curating(me, found, name)
    option = _option(found, name, code)
    try:
        lists.archive(me, option.entry, archived=archived, reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return _options_out(me, found, entity_type, [_option(found, name, code)])[0]


class OrderIn(Schema):
    codes: list[str] = Field(
        min_length=1,
        max_length=10000,
        description="Every option's code, archived and superseded ones too, in the new order.",
    )
    reason: str = Field(
        min_length=1, max_length=2000, description="Why: every project's dropdowns follow it."
    )


@router.put("/lists/{qualified:name}/order", response=list[OptionOut], summary="Reorder a lab list")
def reorder_list(
    request: HttpRequest, name: str, scope: Query[Scope], payload: OrderIn
) -> list[Any]:
    """Every option of the list, as its curators see it, in its new order."""
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, scope)
    _curating(me, found, name)
    try:
        lists.reorder(me, found, name, payload.codes, reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return _options_out(me, found, entity_type, lists.options(found.pk, name, archived=True))


class OptionSupersedeIn(Schema):
    replacement: str = Field(description="The option that replaces it, by its code.")
    reason: str = Field(
        min_length=1, max_length=2000, description="Why: projects that name it are told."
    )


@router.post(
    "/lists/{qualified:name}/options/{code}/supersede",
    response=OptionOut,
    summary="Supersede a lab list's option",
)
def supersede_option(
    request: HttpRequest,
    name: str,
    code: str,
    scope: Query[Scope],
    payload: OptionSupersedeIn,
) -> Any:  # noqa: ANN401
    """For a wrong code: the option is replaced for good by another of the list, which
    dropdowns offer instead. The Admins of each project whose records name it are told, to
    move them to the replacement."""
    me = caller(request)
    entity_type = _list(name)
    found = _registry(me, scope)
    _curating(me, found, name)
    option = _option(found, name, code)
    replacement = lists.find(found.pk, name, payload.replacement.strip())
    if replacement is None:
        raise Problem(
            422,
            "It can’t be superseded so.",
            errors=[
                {
                    "pointer": "#/replacement",
                    "detail": f"{entity_type.label_plural} have no option "
                    f"`{payload.replacement}` here.",
                }
            ],
        )
    try:
        entries.supersede(me, option.entry, replacement.entry, reason=payload.reason)
    except Invalid as error:
        raise Problem(
            422,
            "It can’t be superseded so.",
            errors=[{"pointer": f"#/{n}", "detail": " ".join(m)} for n, m in error.errors.items()],
        ) from None
    return _options_out(me, found, entity_type, [_option(found, name, code)])[0]


class OptionMoveIn(Schema):
    project: str = Field(description="The project whose records move, by ID or slug.")
    reason: str = Field(
        min_length=1, max_length=2000, description="Why: each record's history keeps it."
    )


class OptionMovedOut(Schema):
    moved: list[str] = Field(description="The records that now name the replacement, by HRID.")


@router.post(
    "/lists/{qualified:name}/options/{code}/move",
    response=OptionMovedOut,
    summary="Move a project's records to a superseded option's replacement",
)
def move_references(request: HttpRequest, name: str, code: str, payload: OptionMoveIn) -> Any:  # noqa: ANN401
    """A project's Editor, once the option was superseded: each of the project's records
    that names it names the replacement, a revision with the reason. Archived and locked
    records keep theirs."""
    me = caller(request)
    _list(name)
    found = _registry(me, Scope(project=payload.project, registry=None))
    try:
        key: dict[str, Any] = {"pk": UUID(payload.project)}
    except ValueError:
        key = {"slug": payload.project}
    chosen = Project.objects.select_related("team").get(**key)
    require(me, Action.PROJECT_EDIT, chosen)
    option = _option(found, name, code)
    try:
        moved = lists.repoint(me, chosen, option.entry, reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return {"moved": [e.hrid for e in moved]}


# --- Curators ------------------------------------------------------------------------------------


class ListCuratorOut(Schema):
    list: str = Field(description="The list's type, such as iki/lots.PurityMethod.")
    user: UserOut
    appointed_by: UserOut | None
    appointed_at: datetime


def _manageable(user: User, registry_id: UUID) -> Registry:
    require(user, Action.REGISTRIES_MANAGE)
    found = Registry.objects.filter(pk=registry_id).first()
    if found is None:
        raise Problem(404, "No such registry, or you can’t see it.")
    return found


@router.get(
    "/registries/{registry_id}/list-curators",
    response=list[ListCuratorOut],
    summary="Who curates a registry's lab lists",
)
def list_curators(request: HttpRequest, registry_id: UUID) -> list[dict[str, Any]]:
    """Each list's curators. Its registrars curate every list besides."""
    me = caller(request)
    found = registries_api._registry(me, registry_id)
    rows = found.list_curators.select_related("user", "appointed_by").order_by(
        "list_type", "user__email"
    )
    return [_curator_out(row) for row in rows]


def _curator_out(row: ListCurator) -> dict[str, Any]:
    return {
        "list": row.list_type,
        "user": row.user,
        "appointed_by": row.appointed_by,
        "appointed_at": row.appointed_at,
    }


@router.put(
    "/registries/{registry_id}/list-curators/{qualified:name}/{user_id}",
    response={200: ListCuratorOut, 201: ListCuratorOut},
    summary="Make someone a lab list's curator",
)
def appoint_curator(request: HttpRequest, registry_id: UUID, name: str, user_id: UUID) -> Any:  # noqa: ANN401
    """Org Admins appoint them, a list at a time: a QC lead for deviation categories, a
    purification lead for lot methods. Being an Org Admin doesn't make someone one."""
    me = caller(request)
    found = _manageable(me, registry_id)
    _list(name)
    created = registries.appoint_curator(found, name, person(user_id), by=me)
    row = ListCurator.objects.select_related("user", "appointed_by").get(
        registry=found, list_type=name, user_id=user_id
    )
    return Status(201 if created else 200, _curator_out(row))


@router.delete(
    "/registries/{registry_id}/list-curators/{qualified:name}/{user_id}",
    response={204: None},
    summary="Stop someone curating a lab list",
)
def dismiss_curator(
    request: HttpRequest, registry_id: UUID, name: str, user_id: UUID
) -> Status[None]:
    me = caller(request)
    found = _manageable(me, registry_id)
    registries.dismiss_curator(found, name, person(user_id), by=me)
    return Status(204, None)

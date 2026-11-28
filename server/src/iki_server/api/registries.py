"""Registries and their entries (ADR 0115).

- ``/registries``: the organization's identity boundaries. Org Admins add and
  change them, and choose which teams use each. Others see the registries
  their teams' projects may be in, and their own projects' registries.
- ``/registries/{id}/roles``: who holds each registry role (ADR 0292): its
  registrars, and its lab lists' curators. Org Admins grant and remove them.
- ``/entries/{ref}``: a registry entry, by ID or HRID: its identity, derived
  values and facts, for anyone who can read a record of it, and for its
  registry's registrars. It never says which projects hold it; registrars
  see how many do.
- Registrars change an entry's facts (``PATCH``), and supersede a mistaken
  entry with another (``POST …/supersede``). A holder moves its record to
  the replacement (``POST /entities/{ref}/move``).
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api import preconditions
from iki_server.api.common import caller, person, project, require
from iki_server.api.entities import (
    CreatedOut,
    EntityOut,
    FileRef,
    RefOut,
    RevisionDetailOut,
    RevisionOut,
    TrailFilter,
    _created,
    _etag,
    _find,
    _invalid,
    _lookup,
    _one,
    _revision,
    _runs_of,
    revision_states,
    undisclosed,
)
from iki_server.api.pagination import PageParams, page
from iki_server.api.preconditions import IfMatch
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, ProjectRef, RegistryRef, TeamRef, UserOut
from iki_server.entities import access, entries, lists, registry, services
from iki_server.entities.history import FieldNames
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Invalid, ids_in, shape, to_json
from iki_server.org import permissions, registries
from iki_server.org.models import Project, Registry, RegistryRole, Team
from iki_server.org.permissions import Action
from iki_server.org.services import Refused

router = Router(tags=["registries"])


# --- Registries ----------------------------------------------------------------------------------


class RegistryCan(Schema):
    manage: bool = Field(description="Rename it, choose its teams, grant its roles.")
    curate: bool = Field(
        description="You're one of its registrars: see its entries, change their facts, "
        "supersede them."
    )
    curate_lists: bool = Field(
        description="You curate some of its lab lists, or all of them as a registrar "
        "(ADR 0270): GET /lists?registry= says which."
    )


class RegistryOut(RegistryRef):
    description: str
    default: bool = Field(description="New projects are in it unless they're given another.")
    all_teams: bool = Field(description="Every team's projects may be in it.")
    teams: list[TeamRef] = Field(
        description="Without all_teams, the teams whose projects may be in it. Listed to Org "
        "Admins only."
    )
    created_at: datetime
    can: RegistryCan


class RegistryIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=2000)
    all_teams: bool = Field(False, description="Every team's projects may be in it.")
    teams: list[UUID] = Field(
        default_factory=list, description="Otherwise, the teams whose projects may be."
    )


class RegistryPatch(Schema):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    all_teams: bool | None = None
    teams: list[UUID] | None = Field(
        None,
        description="A team taken off keeps its projects in the registry: it can't put new "
        "ones there.",
    )


class RegistryFilters(PageParams):
    team: UUID | None = Field(
        None, description="Only those this team's new projects may be in, to choose one."
    )


def _registries_for(user: User) -> QuerySet[Registry]:
    """The registries the user may see: all, for an Org Admin; otherwise those their teams'
    projects may be in, their projects' own, and those they hold a role in."""
    Registry.default()
    if permissions.can(user, Action.REGISTRIES_MANAGE):
        return Registry.objects.all()
    teams = permissions.visible_teams(user).values("pk")
    projects = access.readable_projects(user).values("registry_id")
    return Registry.objects.filter(
        Q(all_teams=True) | Q(teams__in=teams) | Q(pk__in=projects) | Q(roles__user=user)
    ).distinct()


def _registry(user: User, registry_id: UUID) -> Registry:
    found = _registries_for(user).filter(pk=registry_id).first()
    if found is None:
        raise Problem(404, "No such registry, or you can’t see it.")
    return found


def _registry_out(user: User, registry: Registry) -> dict[str, Any]:
    manage = permissions.can(user, Action.REGISTRIES_MANAGE)
    return {
        "id": registry.pk,
        "name": registry.name,
        "slug": registry.slug,
        "description": registry.description,
        "default": registry.is_default,
        "all_teams": registry.all_teams,
        "teams": list(registry.teams.order_by("name")) if manage else [],
        "created_at": registry.created_at,
        "can": {
            "manage": manage,
            "curate": permissions.can(user, Action.REGISTRY_CURATE, registry),
            "curate_lists": permissions.can(user, Action.LISTS_CURATE, registry),
        },
    }


def _teams(ids: list[UUID]) -> list[Team]:
    found = list(Team.objects.filter(pk__in=ids))
    if len(found) != len(set(ids)):
        raise Problem(
            422,
            "Some of those teams don’t exist.",
            code="validation_failed",
            errors=[{"pointer": "#/teams", "detail": "Not every one is a team."}],
        )
    return found


@router.get("/registries", response=Page[RegistryOut], summary="Registries")
def list_registries(request: HttpRequest, filters: Query[RegistryFilters]) -> dict[str, Any]:
    """The default one first, then by name."""
    me = caller(request)
    found = _registries_for(me)
    if filters.team is not None:
        chosen = Team.objects.filter(pk=filters.team).first()
        if chosen is None or not permissions.can(me, Action.TEAM_VIEW, chosen):
            raise Problem(404, "No such team, or you can’t see it.")
        found = found.filter(pk__in=registries.for_team(chosen).values("pk"))
    result = page(found, filters)
    result["items"] = [_registry_out(me, r) for r in result["items"]]
    return result


@router.post("/registries", response={201: RegistryOut}, summary="Add a registry")
def create_registry(request: HttpRequest, payload: RegistryIn) -> Status[dict[str, Any]]:
    """Org Admins. A separate registry isolates its projects' identities from every other's:
    for a CRO's client, or a partner program."""
    me = caller(request)
    require(me, Action.REGISTRIES_MANAGE)
    made = registries.create_registry(
        payload.name.strip(),
        payload.description,
        all_teams=payload.all_teams,
        teams=_teams(payload.teams),
        by=me,
    )
    return Status(201, _registry_out(me, made))


@router.get("/registries/{registry_id}", response=RegistryOut, summary="A registry")
def get_registry(request: HttpRequest, registry_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _registry_out(me, _registry(me, registry_id))


@router.patch("/registries/{registry_id}", response=RegistryOut, summary="Change a registry")
def patch_registry(
    request: HttpRequest, registry_id: UUID, payload: RegistryPatch
) -> dict[str, Any]:
    me = caller(request)
    require(me, Action.REGISTRIES_MANAGE)
    found = _registry(me, registry_id)
    changed = registries.update_registry(
        found,
        by=me,
        name=payload.name.strip() if payload.name is not None else None,
        description=payload.description,
        all_teams=payload.all_teams,
        teams=_teams(payload.teams) if payload.teams is not None else None,
    )
    return _registry_out(me, changed)


# --- Registry roles (ADR 0292) ------------------------------------------------------------------

RoleName = Literal["registrar", "list_curator"]


class RegistryRoleOut(Schema):
    id: UUID = Field(description="The grant's: GET or DELETE /registries/{registry_id}/roles/{id}.")
    role: RoleName = Field(
        description="registrar: curates the registry's facts and supersedes its entries, and "
        "curates every lab list in it. list_curator: curates one lab list (ADR 0270)."
    )
    list: str | None = Field(
        description="A list curator's list, such as iki/lots.PurityMethod. Null for a role over "
        "the whole registry."
    )
    user: UserOut
    granted_by: UserOut | None
    granted_at: datetime


class RegistryRoleIn(Schema):
    role: RoleName
    list: str | None = Field(
        None, description="The lab list, for a list curator; none for a registrar."
    )
    user: UUID = Field(description="The person: never a service account.")


class RegistryRoleFilters(PageParams):
    role: RoleName | None = None
    list: str | None = Field(None, description="Only the curators of this lab list.")
    user: UUID | None = Field(None, description="Only this person's roles.")


def _role_out(grant: RegistryRole) -> dict[str, Any]:
    return {
        "id": grant.pk,
        "role": grant.role,
        "list": grant.list_type,
        "user": grant.user,
        "granted_by": grant.granted_by,
        "granted_at": grant.granted_at,
    }


def _grant(registry: Registry, role_id: UUID) -> RegistryRole:
    found = (
        RegistryRole.objects.filter(registry=registry, pk=role_id)
        .select_related("registry", "user", "granted_by")
        .first()
    )
    if found is None:
        raise Problem(404, "No such role in this registry.")
    return found


@router.get(
    "/registries/{registry_id}/roles",
    response=Page[RegistryRoleOut],
    summary="Who holds a registry's roles",
)
def list_registry_roles(
    request: HttpRequest, registry_id: UUID, filters: Query[RegistryRoleFilters]
) -> dict[str, Any]:
    """Its registrars, and its lab lists' curators, oldest grant first. A registrar curates
    every list besides the lists' own curators."""
    me = caller(request)
    found = _registry(me, registry_id)
    rows = found.roles.select_related("user", "granted_by")
    if filters.role is not None:
        rows = rows.filter(role=filters.role)
    if filters.list is not None:
        rows = rows.filter(list_type=filters.list)
    if filters.user is not None:
        rows = rows.filter(user_id=filters.user)
    result = page(rows, filters)
    result["items"] = [_role_out(r) for r in result["items"]]
    return result


@router.post(
    "/registries/{registry_id}/roles",
    response={200: RegistryRoleOut, 201: RegistryRoleOut},
    summary="Give someone a registry role",
)
def grant_registry_role(
    request: HttpRequest, registry_id: UUID, payload: RegistryRoleIn
) -> Status[dict[str, Any]]:
    """Org Admins grant them: a registrar, over the whole registry, or a lab list's curator,
    a list at a time, such as a QC lead for deviation categories. 201 with the new grant,
    or 200 with the one they hold already. Being an Org Admin doesn't make someone either."""
    me = caller(request)
    require(me, Action.REGISTRIES_MANAGE)
    found = _registry(me, registry_id)
    try:
        grant, created = registries.grant_role(
            found,
            RegistryRole.Role(payload.role),
            person(payload.user),
            by=me,
            list_type=payload.list,
        )
    except Refused as refusal:
        if refusal.field != "list":
            raise
        # The role and its list don't go together, or there's no such list.
        raise Problem(
            422,
            "Some fields are invalid.",
            code="validation_failed",
            errors=[{"pointer": "#/list", "detail": str(refusal)}],
        ) from None
    return Status(201 if created else 200, _role_out(_grant(found, grant.pk)))


@router.get(
    "/registries/{registry_id}/roles/{role_id}",
    response=RegistryRoleOut,
    summary="A registry role",
)
def get_registry_role(request: HttpRequest, registry_id: UUID, role_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _role_out(_grant(_registry(me, registry_id), role_id))


@router.delete(
    "/registries/{registry_id}/roles/{role_id}",
    response={204: None},
    summary="Take a registry role away",
)
def remove_registry_role(request: HttpRequest, registry_id: UUID, role_id: UUID) -> Status[None]:
    """Org Admins. A curator of a list whose plugin was removed can still be removed."""
    me = caller(request)
    require(me, Action.REGISTRIES_MANAGE)
    registries.remove_role(_grant(_registry(me, registry_id), role_id), by=me)
    return Status(204, None)


class RegistryEntryOut(Schema):
    id: UUID
    hrid: str
    type: str
    revision: int
    created_at: datetime
    holders: int = Field(description="How many projects have a record of it. Never which.")
    superseded: bool


class RegistryEntryFilters(PageParams):
    type: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Only these types' entries (and their subtypes'): repeat it for several.",
    )
    q: str | None = Field(None, max_length=32, description="An HRID, or the start of one.")
    superseded: bool | None = None


@router.get(
    "/registries/{registry_id}/entries",
    response=Page[RegistryEntryOut],
    summary="Every entry in a registry, for its registrars",
)
def list_registry_entries(
    request: HttpRequest, registry_id: UUID, filters: Query[RegistryEntryFilters]
) -> dict[str, Any]:
    """Its registrars see each entry, and how many projects hold it: never which, nor
    what's in their records."""
    me = caller(request)
    found = _registry(me, registry_id)
    require(me, Action.REGISTRY_CURATE, found)
    rows = entries.registrar_entries(found)
    if filters.type:
        from iki_server.entities.values import subtypes

        rows = rows.filter(type__in={n for t in filters.type for n in subtypes(t)})
    if filters.q:
        rows = rows.filter(hrid__startswith=filters.q.strip().upper())
    if filters.superseded is not None:
        rows = rows.filter(superseded_by__isnull=not filters.superseded)
    result = page(rows, filters)
    result["items"] = [
        {
            "id": e.pk,
            "hrid": e.hrid,
            "type": e.type,
            "revision": e.revision,
            "created_at": e.created_at,
            "holders": e.holders,
            "superseded": e.superseded_by_id is not None,
        }
        for e in result["items"]
    ]
    return result


# --- Entries -------------------------------------------------------------------------------------


class EntryRecordOut(Schema):
    id: UUID = Field(description="Your record of it: GET /entities/{id}.")
    project: ProjectRef


class SupersedeAsOut(Schema):
    type: str
    fields: list[str] = Field(
        description="The registry fields a registrar gives its new entry, as `values`: those "
        "this entry's values don't fill as they are, outside what the two types share or "
        "refined by the new type (its parts' roles). The rest come from this entry."
    )


class EntryCan(Schema):
    curate: bool = Field(description="Change its facts and supersede it: its registrars.")
    supersede_as: list[SupersedeAsOut] = Field(
        default_factory=list,
        description="For its registrars: the other types its identity can be registered as "
        "(ADR 0165), as POST /entries/{ref}/supersede with `type` does, by a new entry of "
        "one that it hands its identity to. Types under one identity share entries: a "
        "protein complex's chains registered as an antibody are that antibody.",
    )


class EntrySupersededOut(Schema):
    by: RefOut
    by_id: UUID
    at: datetime
    reason: str


class EntryOut(Schema):
    id: UUID
    hrid: str
    type: str
    registry: RegistryRef
    revision: int = Field(description="Its latest revision. Send it back as If-Match.")
    created_at: datetime
    values: dict[str, Any] = Field(
        description="Its identity, derived values and facts: every holding project's. "
        "References to entities you can't read are shown as `unreadable`."
    )
    refs: dict[str, RefOut]
    files: dict[str, FileRef]
    superseded: EntrySupersededOut | None
    records: list[EntryRecordOut] = Field(
        description="The records of it in projects you can read: yours, and those shared with them."
    )
    holders: int | None = Field(
        description="For its registrars: how many projects have a record of it. Never which."
    )
    add_to: list[ProjectRef] = Field(
        description="The projects you could add a record of it to (POST "
        "/entries/{ref}/records): those you edit in its registry that haven't one, where a "
        "project you read it in may share with them (ADR 0067)."
    )
    can: EntryCan


def _entry(user: User, ref: str) -> tuple[Entity, bool]:
    """The entry, by ID or HRID, if the user reads a record of it or curates its registry;
    and whether they curate it. Otherwise 404: an entry's ID alone grants nothing."""
    base = Entity.objects.filter(registry__isnull=False).select_related("registry")
    try:
        key: Q = Q(pk=UUID(ref))
    except ValueError:
        key = Q(hrid=ref.upper())
    found = base.filter(key).first()
    if found is None:
        # A record's ID or HRID names its entry too.
        record = access.visible(user).filter(key, entry__isnull=False).first()
        found = base.filter(records=record).first() if record is not None else None
    if found is None:
        raise Problem(404, "No such registry entry, or you can’t read it.")
    assert found.registry is not None  # noqa: S101 - entries have one
    curates = _curates(user, found)
    if not curates and not access.visible_entries(user).filter(pk=found.pk).exists():
        raise Problem(404, "No such registry entry, or you can’t read it.")
    return found, curates


def _curates(user: User, entry: Entity) -> bool:
    """Whether the user curates the entry: its registry's registrars do, and a lab list's
    option's curators (ADR 0270)."""
    assert entry.registry is not None  # noqa: S101 - entries have one
    if registry.is_lab_list(entry.type):
        return permissions.can_curate_list(user, entry.registry, entry.type)
    return permissions.can(user, Action.REGISTRY_CURATE, entry.registry)


def _not_an_option(entry: Entity, change: str, path: str) -> None:
    """A lab list's option (ADR 0270) is changed on its list, under the list's checks: this
    endpoint reads it, and says where."""
    if registry.is_lab_list(entry.type):
        code = lists.option_of(entry).code
        raise Problem(
            409,
            f"{entry.hrid} is an option of a lab list: {change} it on its list, with "
            f"/api/v1/lists/{entry.type}/options/{code}{path}.",
            code="refused",
        )


def _entry_references(
    user: User, entry: Entity, curates: bool, wanted: dict[str, set[Any]]
) -> tuple[dict[Any, dict[str, Any]], dict[Any, dict[str, Any]]]:
    refs, files = _lookup(user, wanted)
    if curates:
        # A registrar reads the entries the registry's facts name.
        names = set(wanted["ref"]) - set(refs)
        for other in Entity.objects.filter(pk__in=names, registry=entry.registry):
            refs[other.pk] = {
                "hrid": other.hrid,
                "title": other.hrid,
                "type": other.type,
                "archived": False,
                "purged": other.purged_at is not None,
            }
    return refs, files


def _entry_out(user: User, entry: Entity, curates: bool) -> dict[str, Any]:
    form = shape(entry.type)
    fields = form.entry_fields()
    values = {
        n: v for n, v in services.current_values(services.specific(entry)).items() if n in fields
    }
    refs, files = _entry_references(user, entry, curates, ids_in(fields, values))
    superseded = None
    found = entries.supersession(entry)
    if found is not None:
        superseded = {
            "by": {
                "hrid": found.replacement.hrid,
                "title": found.replacement.hrid,
                "type": found.replacement.type,
                "archived": False,
                "purged": found.replacement.purged_at is not None,
            },
            "by_id": found.replacement.pk,
            "at": found.at,
            "reason": found.reason,
        }
    readable = {p.pk: p for p in access.readable_projects(user)}
    records = [
        {
            "id": r.pk,
            "project": {
                "id": readable[r.owner_project_id].pk,
                "name": readable[r.owner_project_id].name,
                "slug": readable[r.owner_project_id].slug,
            },
        }
        for r in access.visible(user).filter(entry=entry).order_by("created_at", "pk")
        if r.owner_project_id in readable
    ]
    return {
        "id": entry.pk,
        "hrid": entry.hrid,
        "type": entry.type,
        "registry": entry.registry,
        "revision": entry.revision,
        "created_at": entry.created_at,
        "values": {n: to_json(fields[n], v, readable_refs=refs) for n, v in values.items()},
        "refs": {str(i): r for i, r in refs.items()},
        "files": {str(i): f for i, f in files.items()},
        "superseded": superseded,
        "records": records,
        "holders": entries.holders(entry).count() if curates else None,
        "add_to": _addable(user, entry),
        "can": {
            "curate": curates and entry.superseded_by_id is None,
            "supersede_as": [
                {"type": t, "fields": entries.to_give(entry.type, t)}
                for t in entries.registrable_as(entry)
            ]
            if curates and entry.superseded_by_id is None
            else [],
        },
    }


def _addable(user: User, entry: Entity) -> list[Project]:
    """The projects ``user`` could add a record of ``entry`` to, as :func:`entries.add_record`
    allows: never for a superseded or purged entry."""
    if entry.superseded_by_id is not None or entry.purged_at is not None:
        return []
    held = set(Entity.objects.filter(entry=entry).values_list("owner_project_id", flat=True))
    candidates = (
        access.readable_projects(user)
        .filter(registry=entry.registry, archived_at__isnull=True)
        .exclude(pk__in=held)
        .order_by("name")
    )
    return [
        p
        for p in candidates
        if permissions.can(user, Action.PROJECT_EDIT, p)
        and access.shareable_into(user, p).filter(entry=entry).exists()
    ]


@router.get(
    "/entries/{ref}",
    response=EntryOut,
    summary="A registry entry, by ID or HRID",
    openapi_extra=preconditions.reads(),
)
def get_entry(request: HttpRequest, ref: str, response: HttpResponse) -> dict[str, Any]:
    """For whoever reads a record of it, and its registry's registrars."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    response["ETag"] = _etag(entry)
    return _entry_out(me, entry, curates)


class EntryPatch(Schema):
    values: dict[str, Any] = Field(description="Only the facts to change; null clears one.")
    reason: str = Field(min_length=1, max_length=2000, description="Why: every holder is told.")


@router.patch(
    "/entries/{ref}",
    response=EntryOut,
    summary="Change a registry entry's facts",
    openapi_extra=preconditions.changes(),
)
def patch_entry(
    request: HttpRequest,
    ref: str,
    payload: EntryPatch,
    response: HttpResponse,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    """Its registry's registrars, with a reason. Audited, and told to every holding project:
    who, when and why. Its identity and derived values never change: supersede it instead.
    A lab list's option changes on its list (`/lists/{name}/options/{code}`), not here."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    assert entry.registry is not None  # noqa: S101
    _not_an_option(entry, "change", "")
    if not curates:
        require(me, Action.REGISTRY_CURATE, entry.registry)
    expected = preconditions.expected(if_match)
    try:
        entries.curate(me, entry, payload.values, reason=payload.reason, expected_revision=expected)
    except services.Stale as stale:
        raise preconditions.stale(
            stale.current,
            f"It changed since you read it: it’s at revision {stale.current} now.",
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    fresh = Entity.objects.select_related("registry").get(pk=entry.pk)
    response["ETag"] = _etag(fresh)
    return _entry_out(me, fresh, curates)


class SupersedeIn(Schema):
    replacement: str | None = Field(
        None,
        description="The entry that replaces it, by ID or HRID: one of its type, or of "
        "another type under its identity (ADR 0165). Give this or `type`.",
    )
    type: str | None = Field(
        None,
        description="Register its identity as this type instead (ADR 0165), one of "
        "`can.supersede_as`: a new entry, which it's superseded by and hands its identity "
        "to. Give this or `replacement`.",
    )
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="With `type`: the new entry's values, in place of the superseded "
        "entry's, which it has for the fields the two types share: the new type's facts, "
        "say, or its parts' roles as the new type names them. Its identity stays the same.",
    )
    reason: str = Field(min_length=1, max_length=2000, description="Why: its holders are told it.")


@router.post(
    "/entries/{ref}/supersede",
    response=EntryOut,
    summary="Supersede a registry entry",
    openapi_extra=preconditions.changes(),
)
def supersede_entry(
    request: HttpRequest,
    ref: str,
    payload: SupersedeIn,
    response: HttpResponse,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    """Its registry's registrars replace a mistaken entry with another in the registry, for
    good, from the revision they read (If-Match): one of its type, or of another type under
    its identity; or with a new entry of its identity as another type (``type``, ADR 0165).
    Each holding project's Admins, and whoever added its record, are told, to move their
    records to the replacement. A lab list's option is superseded on its list
    (`/lists/{name}/options/{code}/supersede`), not here, either way."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    assert entry.registry is not None  # noqa: S101
    _not_an_option(entry, "supersede", "/supersede")
    if not curates:
        require(me, Action.REGISTRY_CURATE, entry.registry)
    if (payload.replacement is None) == (payload.type is None):
        raise Problem(
            422,
            "It can’t be superseded so.",
            errors=[
                {
                    "pointer": "#/replacement",
                    "detail": "Give the entry that replaces it, or the type to register it as.",
                }
            ],
        )
    if payload.values and payload.type is None:
        raise Problem(
            422,
            "It can’t be superseded so.",
            errors=[
                {
                    "pointer": "#/values",
                    "detail": "An entry that replaces it has values of its own: give values "
                    "only with `type`.",
                }
            ],
        )
    expected = preconditions.expected(if_match)
    try:
        if payload.type is not None:
            entries.supersede_as(
                me,
                entry,
                payload.type,
                payload.values,
                expected_revision=expected,
                reason=payload.reason,
            )
        else:
            assert payload.replacement is not None  # noqa: S101 - checked above
            replacement, _ = _entry(me, payload.replacement)
            entries.supersede(
                me, entry, replacement, expected_revision=expected, reason=payload.reason
            )
    except services.Stale as stale:
        raise preconditions.stale_revision(stale.current) from None
    except Invalid as error:
        raise Problem(
            422,
            "It can’t be superseded so.",
            errors=[
                {"pointer": _pointer(name), "detail": " ".join(messages)}
                for name, messages in error.errors.items()
            ],
        ) from None
    fresh = Entity.objects.select_related("registry").get(pk=entry.pk)
    preconditions.tagged(response, fresh.revision)
    return _entry_out(me, fresh, True)


def _pointer(name: str) -> str:
    """Where in a supersession's or a move's body an error is: its own fields, or a value."""
    if name in ("reason", "replacement", "type", "values"):
        return f"#/{name}"
    return f"#/values/{name}" if name else "#/values"


@router.get(
    "/entries/{ref}/revisions",
    response=Page[RevisionOut],
    summary="A registry entry's revisions",
    by_alias=True,
)
def list_entry_revisions(
    request: HttpRequest, ref: str, params: Query[PageParams], trail: Query[TrailFilter]
) -> dict[str, Any]:
    """Newest first. Its registrars see each in full, without the project it came through.
    A holder sees who and when for its own projects' and registrars' changes; for others,
    only what was set. ``person`` finds only those you're told they made."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    # A lab list's option is curated in the open (ADR 0270): every reader is told who
    # changed it, when and why, as no project's work is in it.
    curates = curates or registry.is_lab_list(entry.type)
    readable = set(access.readable_projects(me).values_list("pk", flat=True))
    names = FieldNames.for_type(entry.type, entry=True)
    revisions = trail.applied(
        Revision.objects.filter(entity=entry),
        names,
        disclosed=None if curates else services.disclosing(readable),
    ).select_related("user", "agent")
    result = page(revisions, params, key="number", newest_first=True)
    states = revision_states(result["items"])
    runs = _runs_of(me, result["items"])
    result["items"] = [
        {**_revision(r, runs, states=states, names=names), "entry": True}
        if curates or services.discloses(r, readable)
        else undisclosed(r, names)
        for r in result["items"]
    ]
    return result


@router.get(
    "/entries/{ref}/revisions/{number}",
    response=RevisionDetailOut,
    summary="A registry entry as it was at a revision",
    by_alias=True,
)
def get_entry_revision(request: HttpRequest, ref: str, number: int) -> dict[str, Any]:
    """Its values then and what the revision changed, which every holder sees, as reviewing
    a curated fact needs; who made it, how and when as its list of revisions tells them."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    revision = (
        Revision.objects.filter(entity=entry, number=number).select_related("user", "agent").first()
    )
    if revision is None:
        raise Problem(404, f"{entry.hrid} has no revision {number}.")
    # Fields since removed are read back too, as history keeps them (ADR 0035).
    fields = shape(entry.type).entry_fields(removed=True)
    values = services.revision_values(entry, number, fields)
    changes = {n: c for n, c in services.changes(entry, revision).items() if n in fields}
    wanted = ids_in(fields, values)
    for name, (before, after) in changes.items():
        for kind, ids in ids_in(fields, {name: before}).items():
            wanted[kind] |= ids
        for kind, ids in ids_in(fields, {name: after}).items():
            wanted[kind] |= ids
    refs, files = _entry_references(me, entry, curates, wanted)
    readable = set(access.readable_projects(me).values_list("pk", flat=True))
    names = FieldNames.for_type(entry.type, entry=True)
    told = (
        {**_revision(revision, _runs_of(me, [revision]), names=names), "entry": True}
        if curates or registry.is_lab_list(entry.type) or services.discloses(revision, readable)
        else undisclosed(revision, names)
    )
    return {
        **told,
        "values": {n: to_json(fields[n], v, readable_refs=refs) for n, v in values.items()},
        "changes": {
            name: {
                "before": to_json(fields[name], before, readable_refs=refs),
                "after": to_json(fields[name], after, readable_refs=refs),
            }
            for name, (before, after) in changes.items()
        },
        "refs": {str(i): r for i, r in refs.items()},
        "files": {str(i): f for i, f in files.items()},
    }


class EntryRecordIn(Schema):
    project: UUID = Field(description="The project to add it to. It needs you as an Editor.")
    values: dict[str, Any] = Field(
        default_factory=dict, description="The record's own values, as creating it takes."
    )
    reason: str = Field("", max_length=2000)


@router.post(
    "/entries/{ref}/records",
    response={201: CreatedOut, 200: CreatedOut},
    summary="Add a record of a registry entry to a project",
)
def add_entry_record(
    request: HttpRequest, ref: str, payload: EntryRecordIn, response: HttpResponse
) -> Any:  # noqa: ANN401
    """As registering its identity in the project: the project gets its own record of the
    entry (201), or keeps the one it has (200, ``reused``). You must read the entry through
    a project that allows sharing, in the same registry."""
    me = caller(request)
    entry, _ = _entry(me, ref)
    target = project(me, payload.project, Action.PROJECT_EDIT)
    try:
        saved = entries.add_record(me, entry, target, payload.values, reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return _created(me, saved, response)


# --- Moving a record -----------------------------------------------------------------------------


class MoveIn(Schema):
    reason: str = Field(min_length=1, max_length=2000, description="Why the holder is moving it.")
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="For a replacement of another type (ADR 0165): the new record's own "
        "values, beside those it carries from this one (`entry.superseded.carries`).",
    )


class MovedOut(EntityOut):
    repointed: list[RefOut] = Field(
        description="The project's records whose references to the superseded entry now "
        "name its replacement."
    )


@router.post(
    "/entities/{ref}/move",
    response=MovedOut,
    summary="Move a record to the entry that superseded its own",
    openapi_extra=preconditions.changes(),
)
def move_entity(
    request: HttpRequest,
    ref: str,
    payload: MoveIn,
    response: HttpResponse,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    """An Editor of its project, from the record's revision they read (If-Match). It keeps
    its data, history and runs; the project's own records that refer to the superseded
    entry are changed to refer to the new one, each a revision with the reason. Refused if
    the project has a record of the new one already.

    A record can't change its type, so to a replacement of another type (ADR 0165) it's the
    project's new record of the replacement that's returned: with this one's values for the
    fields the two types share and ``values``, while this one is archived, keeping its
    history, signatures and runs."""
    me = caller(request)
    record = _find(me, ref)
    assert record.owner_project_id is not None  # noqa: S101 - records have projects
    project(me, record.owner_project_id, Action.PROJECT_EDIT)
    expected = preconditions.expected(if_match)
    try:
        moved = entries.move(
            me, record, expected_revision=expected, reason=payload.reason, values=payload.values
        )
    except services.Stale as stale:
        raise preconditions.stale_revision(stale.current) from None
    except Invalid as error:
        problem = _invalid(error)
        if getattr(error, "broken", ()):
            # Its new entry's values break its type's rules (ADR 0127), each named.
            raise problem from None
        raise Problem(
            422,
            "It can’t be moved as requested.",
            code=problem.code,
            errors=[
                {"pointer": _pointer(name), "detail": " ".join(messages)}
                for name, messages in error.errors.items()
            ],
        ) from None
    refs, _ = _lookup(me, {"ref": {e.pk for e in moved.repointed}, "file": set()})
    out = _one(me, Entity.objects.select_related("created_by").get(pk=moved.record.pk), response)
    return {**out, "repointed": [refs[e.pk] for e in moved.repointed if e.pk in refs]}

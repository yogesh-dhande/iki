"""Registries and their entries (ADR 0115).

- ``/registries``: the organization's identity boundaries. Org Admins add and
  change them, choose which teams use each, and appoint their registrars.
  Others see the registries their teams' projects may be in, and their own
  projects' registries.
- ``/entries/{ref}``: a registry entry, by ID or HRID: its identity, derived
  values and facts, for anyone who can read a record of it, and for its
  registry's registrars. It never says which projects hold it; registrars
  see how many do.
- Registrars change an entry's facts (``PATCH``), and supersede a mistaken
  entry with another (``POST …/supersede``). A holder moves its record to
  the replacement (``POST /entities/{ref}/move``).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from ninja import Field, HeaderEx, P, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, person, project, require
from iki_server.api.entities import (
    CreatedOut,
    EntityOut,
    FileRef,
    RefOut,
    RevisionDetailOut,
    RevisionOut,
    _created,
    _etag,
    _expected,
    _find,
    _ids_in,
    _invalid,
    _lookup,
    _one,
    _revision,
    revision_states,
    undisclosed,
)
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, ProjectRef, RegistryRef, TeamRef, UserOut
from iki_server.entities import access, entries, services
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Invalid, shape, to_json
from iki_server.org import permissions, registries
from iki_server.org.models import Project, Registrar, Registry, Team
from iki_server.org.permissions import Action

router = Router(tags=["registries"])


# --- Registries ----------------------------------------------------------------------------------


class RegistryCan(Schema):
    manage: bool = Field(description="Rename it, choose its teams, appoint its registrars.")
    curate: bool = Field(
        description="You're one of its registrars: see its entries, change their facts, "
        "supersede them."
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


class RegistrarOut(Schema):
    user: UserOut
    appointed_by: UserOut | None
    appointed_at: datetime


class RegistryFilters(PageParams):
    team: UUID | None = Field(
        None, description="Only those this team's new projects may be in, to choose one."
    )


def _registries_for(user: User) -> QuerySet[Registry]:
    """The registries the user may see: all, for an Org Admin; otherwise those their teams'
    projects may be in, their projects' own, and those they're a registrar of."""
    Registry.default()
    if permissions.can(user, Action.REGISTRIES_MANAGE):
        return Registry.objects.all()
    teams = permissions.visible_teams(user).values("pk")
    projects = access.readable_projects(user).values("registry_id")
    return Registry.objects.filter(
        Q(all_teams=True) | Q(teams__in=teams) | Q(pk__in=projects) | Q(registrars__user=user)
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


@router.get(
    "/registries/{registry_id}/registrars",
    response=list[RegistrarOut],
    summary="A registry's registrars",
)
def list_registrars(request: HttpRequest, registry_id: UUID) -> list[Registrar]:
    """Who curates its entries' facts and supersedes them."""
    me = caller(request)
    found = _registry(me, registry_id)
    return list(found.registrars.select_related("user", "appointed_by").order_by("user__email"))


@router.put(
    "/registries/{registry_id}/registrars/{user_id}",
    response={200: RegistrarOut, 201: RegistrarOut},
    summary="Make someone a registrar",
)
def appoint_registrar(request: HttpRequest, registry_id: UUID, user_id: UUID) -> Any:  # noqa: ANN401
    """Org Admins appoint them, a registry at a time. Being an Org Admin doesn't make
    someone one."""
    me = caller(request)
    require(me, Action.REGISTRIES_MANAGE)
    found = _registry(me, registry_id)
    created = registries.appoint(found, person(user_id), by=me)
    row = Registrar.objects.select_related("user", "appointed_by").get(
        registry=found, user_id=user_id
    )
    return Status(201 if created else 200, row)


@router.delete(
    "/registries/{registry_id}/registrars/{user_id}",
    response={204: None},
    summary="Stop someone being a registrar",
)
def dismiss_registrar(request: HttpRequest, registry_id: UUID, user_id: UUID) -> Status[None]:
    me = caller(request)
    require(me, Action.REGISTRIES_MANAGE)
    found = _registry(me, registry_id)
    registries.dismiss(found, person(user_id), by=me)
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
    type: str | None = Field(None, description="Only this type's entries (and its subtypes').")
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

        rows = rows.filter(type__in=subtypes(filters.type))
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


class EntryCan(Schema):
    curate: bool = Field(description="Change its facts and supersede it: its registrars.")


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
        description="Its identity, derived values and facts: every holding project's."
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
    curates = permissions.can(user, Action.REGISTRY_CURATE, found.registry)
    if not curates and not access.visible_entries(user).filter(pk=found.pk).exists():
        raise Problem(404, "No such registry entry, or you can’t read it.")
    return found, curates


def _entry_out(user: User, entry: Entity, curates: bool) -> dict[str, Any]:
    form = shape(entry.type)
    fields = form.entry_fields()
    values = services.current_values(services.specific(entry))
    shown = {n: to_json(fields[n], v) for n, v in values.items() if n in fields}
    wanted = _ids_in(fields, {n: v for n, v in values.items() if n in fields})
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
        "values": shown,
        "refs": {str(i): r for i, r in refs.items()},
        "files": {str(i): f for i, f in files.items()},
        "superseded": superseded,
        "records": records,
        "holders": entries.holders(entry).count() if curates else None,
        "add_to": _addable(user, entry),
        "can": {"curate": curates and entry.superseded_by_id is None},
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


@router.get("/entries/{ref}", response=EntryOut, summary="A registry entry, by ID or HRID")
def get_entry(request: HttpRequest, ref: str, response: HttpResponse) -> dict[str, Any]:
    """For whoever reads a record of it, and its registry's registrars."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    response["ETag"] = _etag(entry)
    return _entry_out(me, entry, curates)


class EntryPatch(Schema):
    values: dict[str, Any] = Field(description="Only the facts to change; null clears one.")
    reason: str = Field(min_length=1, max_length=2000, description="Why: every holder is told.")


@router.patch("/entries/{ref}", response=EntryOut, summary="Change a registry entry's facts")
def patch_entry(
    request: HttpRequest,
    ref: str,
    payload: EntryPatch,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Its registry's registrars, with a reason. Audited, and told to every holding project:
    who, when and why. Its identity and derived values never change: supersede it instead."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    assert entry.registry is not None  # noqa: S101
    require(me, Action.REGISTRY_CURATE, entry.registry)
    expected = _expected(if_match)
    try:
        entries.curate(me, entry, payload.values, reason=payload.reason, expected_revision=expected)
    except services.Stale as stale:
        raise Problem(
            412,
            f"It changed since you read it: it’s at revision {stale.current} now.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    fresh = Entity.objects.select_related("registry").get(pk=entry.pk)
    response["ETag"] = _etag(fresh)
    return _entry_out(me, fresh, curates)


class SupersedeIn(Schema):
    replacement: str = Field(description="The entry that replaces it, by ID or HRID.")
    reason: str = Field(min_length=1, max_length=2000, description="Why: its holders are told it.")


@router.post("/entries/{ref}/supersede", response=EntryOut, summary="Supersede a registry entry")
def supersede_entry(request: HttpRequest, ref: str, payload: SupersedeIn) -> dict[str, Any]:
    """Its registry's registrars replace a mistaken entry with another of its type in the
    registry, for good. Each holding project's Admins, and whoever added its record, are
    told, to move their records to the replacement."""
    me = caller(request)
    entry, _ = _entry(me, ref)
    assert entry.registry is not None  # noqa: S101
    require(me, Action.REGISTRY_CURATE, entry.registry)
    replacement, _ = _entry(me, payload.replacement)
    try:
        entries.supersede(me, entry, replacement, reason=payload.reason)
    except Invalid as error:
        raise Problem(
            422,
            "It can’t be superseded so.",
            errors=[
                {"pointer": f"#/{name}", "detail": " ".join(messages)}
                for name, messages in error.errors.items()
            ],
        ) from None
    fresh = Entity.objects.select_related("registry").get(pk=entry.pk)
    return _entry_out(me, fresh, True)


@router.get(
    "/entries/{ref}/revisions",
    response=Page[RevisionOut],
    summary="A registry entry's revisions",
    by_alias=True,
)
def list_entry_revisions(
    request: HttpRequest, ref: str, params: Query[PageParams]
) -> dict[str, Any]:
    """Newest first. Its registrars see each in full, without the project it came through.
    A holder sees who and when for its own projects' and registrars' changes; for others,
    only what was set."""
    me = caller(request)
    entry, curates = _entry(me, ref)
    revisions = Revision.objects.filter(entity=entry).select_related("user", "agent")
    result = page(revisions, params, key="number", newest_first=True)
    readable = set(access.readable_projects(me).values_list("pk", flat=True))
    states = revision_states(result["items"])
    result["items"] = [
        {**_revision(r, {}, states=states), "entry": True}
        if curates or services.discloses(r, readable)
        else undisclosed(r)
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
    fields = shape(entry.type).entry_fields()
    values = {n: v for n, v in services.values_at(entry, number).items() if n in fields}
    changes = {n: c for n, c in services.changes(entry, revision).items() if n in fields}
    wanted = _ids_in(fields, values)
    for name, (before, after) in changes.items():
        for kind, ids in _ids_in(fields, {name: before}).items():
            wanted[kind] |= ids
        for kind, ids in _ids_in(fields, {name: after}).items():
            wanted[kind] |= ids
    refs, files = _lookup(me, wanted)
    readable = set(access.readable_projects(me).values_list("pk", flat=True))
    told = (
        {**_revision(revision, {}), "entry": True}
        if curates or services.discloses(revision, readable)
        else undisclosed(revision)
    )
    return {
        **told,
        "values": {n: to_json(fields[n], v) for n, v in values.items()},
        "changes": {
            name: {"before": to_json(fields[name], before), "after": to_json(fields[name], after)}
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
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class MovedOut(EntityOut):
    repointed: list[RefOut] = Field(
        description="The project's records whose references to the superseded entry now "
        "name its replacement."
    )


@router.post(
    "/entities/{ref}/move",
    response=MovedOut,
    summary="Move a record to the entry that superseded its own",
)
def move_entity(
    request: HttpRequest, ref: str, payload: MoveIn, response: HttpResponse
) -> dict[str, Any]:
    """An Editor of its project. It keeps its data, history and runs; the project's own
    records that refer to the superseded entry are changed to refer to the new one, each a
    revision with the reason. Refused if the project has a record of the new one already."""
    me = caller(request)
    record = _find(me, ref)
    assert record.owner_project_id is not None  # noqa: S101 - records have projects
    project(me, record.owner_project_id, Action.PROJECT_EDIT)
    moved = entries.move(me, record, reason=payload.reason)
    refs, _ = _lookup(me, {"ref": {e.pk for e in moved.repointed}, "file": set()})
    out = _one(me, Entity.objects.select_related("created_by").get(pk=record.pk), response)
    return {**out, "repointed": [refs[e.pk] for e in moved.repointed if e.pk in refs]}

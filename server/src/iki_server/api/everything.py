"""Search everything (ADR 0176): records, requests, runs, projects, people and saved views,
across every project the caller can read, in one call.

Each kind is found by its own reading rules, the ones its own list uses, so this finds
nothing its caller couldn't open there:

- **records** and **requests** are entities: :func:`iki_server.entities.search.queryset`,
  with no project, finds those the caller can read. Requests are records of the types
  that make requests (ADR 0132); the rest are records. Archived ones aren't searched, as
  the lists don't show them unless asked.
- **runs**: those in projects whose data the caller can read, as ``GET /runs``.
- **projects**: those the caller can see, as ``GET /projects``.
- **people**: everyone, for those who manage the organization's people; for everyone else,
  the people on the members pages of the projects they can see, and of their teams.
- **saved views**: saved searches of projects the caller can read, their own and those
  shared with the project.

Each group says how many it found, up to :data:`TOTAL_CAP`, and links to its full list.
A count that runs out of time is left out, not estimated.

Text in records and runs is found by their trigram indexes, which need three characters:
shorter text isn't searched there (``searched_records`` says so). An HRID's start
(``PRT-0001``) is found by the HRIDs' own index. Calls are rate limited per caller, as
every API call is.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode
from uuid import UUID

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.http import HttpRequest
from ninja import Field, Query, Router, Schema

from iki_server.accounts.models import Kind, User
from iki_server.api.common import caller
from iki_server.api.schemas import ProjectRef
from iki_server.entities import access, registry, requests, search, services
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.entities.values import shape, to_json
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectMembership, TeamMembership
from iki_server.org.permissions import Action
from iki_server.runs import catalog
from iki_server.runs.models import ActionRun, RunStatus
from iki_server.runs.services import visible_runs
from iki_server.searches.models import SavedSearch
from iki_spec import titles

router = Router(tags=["search"])

#: The kinds of thing searched, in the order they're shown.
KINDS = ("entities", "requests", "runs", "projects", "saved_views", "people")
SearchKind = Literal["entities", "requests", "runs", "projects", "saved_views", "people"]
ResultKind = Literal["entity", "request", "run", "project", "saved_view", "person"]

#: Counts stop here: past it, a group says "1,000+". Counting further reads more rows.
TOTAL_CAP = 1000
#: The shortest text searched in records and runs: their trigram indexes need three.
MIN_TEXT = 3
#: How long each group's queries may take, in milliseconds: a palette answers as one types.
TIME_LIMIT_MS = 3000
#: The most recent things shown before anything is typed.
RECENT = 8
#: The most projects a result lists.
PROJECTS_SHOWN = 3

_LABELS = {
    "entities": "Entities",
    "requests": "Requests",
    "runs": "Runs",
    "projects": "Projects",
    "saved_views": "Saved views",
    "people": "People",
}


class SearchEverythingParams(Schema):
    q: str = Field("", max_length=search.MAX_TEXT, description="What to look for.")
    kind: SearchKind | None = Field(None, description="Only this kind, with more of it.")
    limit: int | None = Field(
        None,
        ge=1,
        le=50,
        description="Results per kind: 5 by default, 20 for one `kind`.",
    )


class SearchResultOut(Schema):
    kind: ResultKind
    id: UUID
    hrid: str | None = Field(description="Its HRID, for records, requests and runs.")
    title: str
    detail: str = Field(
        description="What it is: a record's type, a run's action and status, a project's "
        "team, a person's email address, whose a saved view is."
    )
    type: str | None = Field(description="A record's or request's type, qualified.")
    projects: list[ProjectRef] = Field(
        description="The projects it's in that you can read, its owner first, up to three. "
        "A person's are the ones you share with them."
    )
    href: str = Field(description="Where it opens.")
    query: dict[str, Any] | None = Field(
        description="A saved view's search, as `POST /entities/search` takes it."
    )


class SearchGroupOut(Schema):
    kind: SearchKind
    label: str
    total: int | None = Field(
        description="How many there are, up to 1,000; null when counting would be too slow."
    )
    more_than_total: bool = Field(description="There are more than `total`.")
    href: str | None = Field(description="Their full list, when there is one.")
    items: list[SearchResultOut]


class SearchEverythingOut(Schema):
    q: str
    groups: list[SearchGroupOut] = Field(description="Each kind searched, in order, even if empty.")
    searched_records: bool = Field(
        description="Whether records, requests and runs were searched: text needs three "
        "characters, or the start of an HRID."
    )
    recent: list[SearchResultOut] = Field(
        description="With no text: what you changed or ran lately, newest first."
    )
    projects_readable: int = Field(description="How many projects you can read: where it looked.")


@dataclass
class _Group:
    kind: str
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = 0
    more: bool = False
    href: str | None = None


@router.get("/search", response=SearchEverythingOut, summary="Search everything you can read")
def search_everything(
    request: HttpRequest, params: Query[SearchEverythingParams]
) -> dict[str, Any]:
    """Records, requests, runs, projects, people and saved views matching `q`, across every
    project you can read, grouped by kind: each kind by its own reading rules, so nothing
    you couldn't open is found, or counted. Records, requests and runs are found by their
    HRID (or its start), or text in their title or action; the others by name. With no
    `q`, `recent` lists what you changed or ran lately."""
    me = caller(request)
    q = " ".join(params.q.split())
    kinds = [params.kind] if params.kind else list(KINDS)
    limit = params.limit or (20 if params.kind else 5)
    hrid_like = search.looks_like_hrid(q)
    records = len(q) >= MIN_TEXT or hrid_like
    groups: list[_Group] = []
    if q:
        finders: dict[str, Callable[[User, str, int], _Group]] = {
            "entities": _entities,
            "requests": _requests,
            "runs": _runs,
            "projects": _projects,
            "saved_views": _saved_views,
            "people": _people,
        }
        for kind in kinds:
            if kind in ("entities", "requests", "runs") and not records:
                groups.append(_Group(kind))
                continue
            groups.append(_timed(kind, finders[kind], me, q, limit))
    return {
        "q": q,
        "groups": [
            {
                "kind": g.kind,
                "label": _LABELS[g.kind],
                "total": g.total,
                "more_than_total": g.more,
                "href": g.href,
                "items": g.items,
            }
            for g in groups
        ],
        "searched_records": bool(q) and records,
        "recent": [] if q else _recent(me),
        "projects_readable": access.readable_projects(me).count(),
    }


def _timed(
    kind: str, find: Callable[[User, str, int], _Group], me: User, q: str, limit: int
) -> _Group:
    """The group, or an empty one without a count if its search ran out of time."""
    try:
        with search.time_limit(TIME_LIMIT_MS, pages=False):
            return find(me, q, limit)
    except search.TooSlow:
        return _Group(kind, total=None)


def _counted(found: QuerySet[Any]) -> tuple[int, bool]:
    """How many, up to the cap, and whether there are more: it reads no further."""
    total = found.values("pk")[: TOTAL_CAP + 1].count()
    return min(total, TOTAL_CAP), total > TOTAL_CAP


def _list_href(path: str, q: str) -> str:
    return f"{path}?{urlencode({'q': q})}"


# --- Records and requests -------------------------------------------------------------------


def _entities(me: User, q: str, limit: int) -> _Group:
    wanted = set(requests.request_types())
    names = [n for n in registry.types() if n not in wanted and n != "iki/core.Entity"]
    return _records(me, q, limit, "entities", names, "/entities/")


def _requests(me: User, q: str, limit: int) -> _Group:
    return _records(me, q, limit, "requests", requests.request_types(), "/requests/")


def _records(me: User, q: str, limit: int, kind: str, names: list[str], path: str) -> _Group:
    group = _Group(kind, href=_list_href(path, q))
    if not names:
        return group
    wanted = search.Search(types=tuple(sorted(names)), exact_type=True, archived=False, q=q)
    found = search.queryset(me, wanted)
    # The one it names, if the text is a whole HRID, then the newest.
    exact = list(found.filter(hrid=q.upper())[:limit]) if search.looks_like_hrid(q) else []
    rest = list(found.exclude(pk__in=[e.pk for e in exact]).order_by("-pk")[: limit - len(exact)])
    group.total, group.more = _counted(found)
    group.items = _entities_out(me, [*exact, *rest], "request" if kind == "requests" else "entity")
    return group


def _entities_out(me: User, entities: Sequence[Entity], kind: str) -> list[dict[str, Any]]:
    if not entities:
        return []
    values = services.current_values_of(entities)
    projects = _projects_of(me, entities)
    types = registry.types()
    out = []
    for entity in entities:
        entity_type = types.get(entity.type)
        fields = shape(entity.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in values[entity.pk].items() if n in fields}
        template = registry.display_title(entity_type) if entity_type is not None else None
        theirs = projects.get(entity.pk, [])
        href = f"/entities/{entity.hrid}/"
        owner = next((p for p in theirs if p.pk == entity.owner_project_id), None)
        if entity.entry_id is not None and owner is not None:
            # A record of a registry entry shares its HRID: say which project's record.
            href += "?" + urlencode({"project": owner.slug})
        out.append(
            _result(
                kind,
                entity.pk,
                titles.render(template, shown, entity.hrid, fields),
                detail=entity_type.label if entity_type is not None else entity.type,
                hrid=entity.hrid,
                type=entity.type,
                projects=theirs,
                href=href,
            )
        )
    return out


def _projects_of(me: User, entities: Sequence[Entity]) -> dict[Any, list[Project]]:
    """The projects each is in that the caller can read: its owner first, then by name."""
    owners = {e.pk: e.owner_project_id for e in entities}
    links = (
        ProjectLink.objects.filter(
            entity__in=list(owners), project__in=access.readable_projects(me).values("pk")
        )
        .select_related("project")
        .order_by("project__name", "project_id")
    )
    out: dict[Any, list[Project]] = {}
    for link in links:
        out.setdefault(link.entity_id, []).append(link.project)
    for pk, listed in out.items():
        listed.sort(key=lambda p: p.pk != owners[pk])
        del listed[PROJECTS_SHOWN:]
    return out


# --- Runs -------------------------------------------------------------------------------------


def _action_label(name: str) -> str:
    found = catalog.action(name)
    return (found.label or found.name) if found is not None else name


def _runs(me: User, q: str, limit: int) -> _Group:
    group = _Group("runs", href=_list_href("/runs/", q))
    text = q.casefold()
    # The installed actions whose label or name has the text: each found by its index.
    actions = [
        name
        for name, action in catalog.actions().items()
        if text in (action.label or "").casefold() or text in name.casefold()
    ]
    matches = Q(action__in=actions)
    if search.looks_like_hrid(q):
        matches |= Q(hrid__startswith=q.upper())
    found = visible_runs(me).filter(matches)
    group.total, group.more = _counted(found)
    group.items = _runs_out(list(found.select_related("project").order_by("-pk")[:limit]))
    return group


def _runs_out(runs: Iterable[ActionRun]) -> list[dict[str, Any]]:
    return [
        _result(
            "run",
            run.pk,
            _action_label(run.action),
            detail=RunStatus(run.status).label,
            hrid=run.hrid,
            projects=[run.project],
            href=f"/runs/{run.hrid}/",
        )
        for run in runs
    ]


# --- Projects, saved views and people -----------------------------------------------------------


def _projects(me: User, q: str, limit: int) -> _Group:
    group = _Group("projects", href="/projects/")
    found = permissions.visible_projects(me).filter(
        Q(name__icontains=q) | Q(team__name__icontains=q)
    )
    group.total, group.more = _counted(found)
    shown = found.order_by("archived_at", "name", "pk")[:limit]
    group.items = [
        _result(
            "project",
            p.pk,
            p.name,
            detail=p.team.name + (" · Archived" if p.archived_at else ""),
            href=f"/projects/{p.slug}/",
        )
        for p in shown
    ]
    return group


def _saved_views(me: User, q: str, limit: int) -> _Group:
    group = _Group("saved_views")
    # Your own and those shared, in projects you read; and your own of every project you
    # read, which have no project and are never shared.
    found = SavedSearch.objects.filter(
        (Q(created_by=me) | Q(shared=True))
        & Q(project__in=access.readable_projects(me).values("pk"))
        | Q(created_by=me, project__isnull=True),
        name__icontains=q,
    )
    group.total, group.more = _counted(found)
    shown = found.select_related("project").order_by("name", "pk")[:limit]
    group.items = [
        _result(
            "saved_view",
            s.pk,
            s.name,
            detail=(
                "Shared with the project"
                if s.shared
                else "Yours"
                if s.project
                else "Yours, across your projects"
            ),
            projects=[s.project] if s.project else [],
            # The app opens it in the project's grid, as its saved searches do; one of every
            # project in All entities.
            href=f"/projects/{s.project.slug}/entities/" if s.project else "/entities/",
            query=dict(s.query),
        )
        for s in shown
    ]
    return group


def _directory(me: User) -> tuple[QuerySet[User], bool]:
    """The people the caller may look up, and whether they manage people (and so open each
    one's page). Everyone else sees whom their projects' and teams' members pages show."""
    people = User.objects.filter(kind=Kind.PERSON, imported_from="")
    if permissions.can(me, Action.ORG_MANAGE):
        return people, True
    projects = permissions.visible_projects(me).values("pk")
    grants = ProjectMembership.objects.filter(project__in=projects)
    teams = permissions.visible_teams(me).values("pk")
    shared = (
        Q(Exists(grants.filter(user=OuterRef("pk"))))
        | Q(
            Exists(
                TeamMembership.objects.filter(
                    user=OuterRef("pk"),
                    team__in=grants.filter(team__isnull=False).values("team"),
                )
            )
        )
        | Q(Exists(TeamMembership.objects.filter(user=OuterRef("pk"), team__in=teams)))
    )
    return people.filter(shared, is_active=True), False


def _people(me: User, q: str, limit: int) -> _Group:
    directory, manages = _directory(me)
    group = _Group("people", href="/admin/people/" if manages else None)
    found = directory.filter(Q(name__icontains=q) | Q(email__icontains=q))
    group.total, group.more = _counted(found)
    shown = list(found.order_by("name", "email", "pk")[:limit])
    shared = _shared_projects(me, shown)
    teams = _shared_teams(me, shown) if not manages else {}
    out = []
    for person in shown:
        theirs = shared.get(person.pk, [])
        if manages:
            href = f"/admin/people/{person.pk}/"
        elif theirs:
            href = f"/projects/{theirs[0].slug}/members/"
        else:
            href = f"/teams/{teams[person.pk]}/" if person.pk in teams else "/teams/"
        out.append(
            _result(
                "person",
                person.pk,
                person.get_full_name(),
                detail=person.email + ("" if person.is_active else " · Deactivated"),
                projects=theirs,
                href=href,
            )
        )
    group.items = out
    return group


def _shared_projects(me: User, people: Sequence[User]) -> dict[Any, list[Project]]:
    """Of the projects the caller can see, those each person is a member of (by their own
    grant or a team's), by name."""
    if not people:
        return {}
    ids = [p.pk for p in people]
    grants = ProjectMembership.objects.filter(
        project__in=permissions.visible_projects(me).values("pk")
    )
    pairs = set(grants.filter(user__in=ids).values_list("user_id", "project_id"))
    pairs |= set(
        grants.filter(team__memberships__user__in=ids).values_list(
            "team__memberships__user_id", "project_id"
        )
    )
    projects = Project.objects.filter(pk__in={pk for _, pk in pairs}).order_by("name", "pk")
    out: dict[Any, list[Project]] = {}
    for project in projects:
        for person in ids:
            listed = out.setdefault(person, [])
            if (person, project.pk) in pairs and len(listed) < PROJECTS_SHOWN:
                listed.append(project)
    return out


def _shared_teams(me: User, people: Sequence[User]) -> dict[Any, str]:
    """A team each person is in that the caller can see, by slug."""
    rows = (
        TeamMembership.objects.filter(
            user__in=[p.pk for p in people],
            team__in=permissions.visible_teams(me).values("pk"),
        )
        .order_by("team__name")
        .values_list("user_id", "team__slug")
    )
    out: dict[Any, str] = {}
    for pk, slug in rows:
        out.setdefault(pk, slug)
    return out


# --- Recent -----------------------------------------------------------------------------------


def _recent(me: User) -> list[dict[str, Any]]:
    """What the caller changed or ran lately, that they can still read, newest first."""
    changed: dict[Any, Any] = {}
    for entity_id, at in (
        Revision.objects.filter(user=me).order_by("-id").values_list("entity_id", "created_at")
    )[:100]:
        changed.setdefault(entity_id, at)
    entities = {
        e.pk: e for e in access.visible(me).filter(pk__in=list(changed), archived_at__isnull=True)
    }
    asked = set(requests.request_types())
    runs = list(
        visible_runs(me).filter(requested_by=me).select_related("project").order_by("-pk")[:RECENT]
    )
    dated: list[tuple[Any, str, Any]] = [
        (changed[pk], "entity", e) for pk, e in entities.items()
    ] + [(r.created_at, "run", r) for r in runs]
    dated.sort(key=lambda d: d[0], reverse=True)
    latest = dated[:RECENT]
    shown_entities = [e for _, kind, e in latest if kind == "entity"]
    entity_results = {r["id"]: r for r in _entities_out(me, shown_entities, "entity")}
    run_results = {r["id"]: r for r in _runs_out([r for _, kind, r in latest if kind == "run"])}
    out = []
    for _, kind, thing in latest:
        if kind == "run":
            out.append(run_results[thing.pk])
            continue
        result = entity_results[thing.pk]
        if thing.type in asked:
            result["kind"] = "request"
        out.append(result)
    return out


def _result(
    kind: str,
    pk: Any,  # noqa: ANN401 - a UUID
    title: str,
    *,
    detail: str,
    href: str,
    hrid: str | None = None,
    type: str | None = None,
    projects: Sequence[Project] = (),
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": pk,
        "hrid": hrid,
        "title": title,
        "detail": detail,
        "type": type,
        "projects": [{"id": p.pk, "name": p.name, "slug": p.slug} for p in projects],
        "href": href,
        "query": query,
    }

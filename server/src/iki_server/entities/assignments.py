"""Assigning records to people, and each person's work (ADR 0122).

A type marks some of its fields that name people as **assignment fields**
(``assignment: true``): whoever such a field names is assigned the record. A type
with state fields (ADR 0125) can say in which states of one each counts
(``assignment: {field: status, states: [in_review]}``): a sample's reviewer reviews it
while its status is In review, not before.

Someone is **assigned** a record while an assignment field that counts in its states
names them and they could be named there now (:mod:`iki_server.entities.people`):
active, able to read the record's project, holding the field's lab role. That's what
their My work lists (:func:`work`), what the search's ``assigned`` finds, and what
``{{ me }}`` matches in a home's section. Someone who can't be named there any more
drops off it at once, and the record **needs reassigning**
(``needs_reassigning``), which its project's Admins are told of
(:func:`check_people`).

A record whose assignment field is empty, or names someone who can't be named, is
**open** to whoever holds the field's lab role: :func:`open_work` lists those, and
:func:`take` assigns one to its taker. A lab role is how a record goes to a group
(a lab's QA): a field names one person, who answers for it.

Each change to an assignment field is part of the record's revision, as every edit
is, and also audited (``entity.assigned``), told to webhooks and told to whoever it
names or stops naming (:func:`changed`). Many changes at once (a bulk reassignment, a
run's results) tell each person once (:func:`batch`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache, partial
from typing import Any, cast

from django.core import signing
from django.db.models import Exists, F, OuterRef, Q, QuerySet, Value
from django.db.models.functions import Cast

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import access, people, registry, requests, states
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Part, Shape, shape, subtypes
from iki_server.org import lab_roles, permissions
from iki_server.org.models import Project
from iki_spec import naming
from iki_spec.model import Field, names_people, person_role

__all__ = [
    "Assignment",
    "Change",
    "WorkItem",
    "batch",
    "changed",
    "check_people",
    "condition",
    "due_field",
    "of",
    "open_work",
    "work",
]


@dataclass(frozen=True)
class Assignment:
    """An assignment field of a type."""

    name: str
    field: Field
    label: str
    #: The lab role whoever it names holds, qualified; None for anyone in the project.
    role: str | None
    #: The state field (ADR 0125) whose states it counts in; None for every state.
    state_field: str | None
    #: The states of ``state_field`` it counts in.
    states: tuple[str, ...]
    #: A list naming several people, rather than one.
    many: bool
    part: Part
    #: A request's service field (ADR 0132): it names the service's people, with the roles
    #: they hold in the service's project.
    service: bool = False

    @property
    def project_column(self) -> str:
        """Where a record's project for this field is: its own, or for a request's
        service field, the service it was sent to."""
        return "iki_request__service_id" if self.service else "owner_project_id"

    def counts(self, values: Mapping[str, Any]) -> bool:
        """Whether it counts for a record of these values: its state field is in one of
        its states."""
        return self.state_field is None or values.get(self.state_field) in self.states


@cache
def of(type_name: str) -> tuple[Assignment, ...]:
    """The assignment fields of a type: its own, its parents' and those extensions add."""
    if type_name not in registry.types() or type_name == "iki/core.Entity":
        return ()
    form = shape(type_name)
    out = []
    for name, f in form.fields().items():
        if not f.assignment or not names_people(f):
            continue
        out.append(
            Assignment(
                name=name,
                field=f,
                label=f.label or name,
                role=person_role(f),
                state_field=f.assignment_field,
                states=f.assignment_states,
                many=f.type == "list",
                part=form.part_of(name),
                service=requests.is_service_field(type_name, name),
            )
        )
    return tuple(out)


@cache
def due_field(type_name: str) -> Field | None:
    """The field saying when a type's records are due: its own ``due``, or the nearest
    ancestor's."""
    name = due_field_name(type_name)
    if name is None:
        return None
    found = shape(type_name).fields().get(name)
    return found if found is not None and found.type in ("date", "datetime") else None


def assigned_types(within: Iterable[str] | None = None) -> list[str]:
    """The concrete types with assignment fields, of ``within`` (and their subtypes) or all."""
    names = set(registry.types()) if within is None else set(within)
    return sorted(
        n for n in names if n in registry.types() and not registry.types()[n].abstract and of(n)
    )


# --- Conditions -----------------------------------------------------------------------------------


def _path(form: Shape, a: Assignment) -> str:
    """The column a single person's assignment is in, from the type's model."""
    return f"{_column_of(form, a.name)}_id"


def _column_of(form: Shape, name: str) -> str:
    """A field's column, from the type's model: through its extension's, for an extension's."""
    part = form.part_of(name)
    prefix = f"{naming.extension_related_name(part.plugin)}__" if part.extension else ""
    return f"{prefix}{name}"


def due_name(type_name: str) -> str:
    found = due_field_name(type_name)
    assert found is not None  # noqa: S101 - asked only of types with one
    return found


@cache
def due_field_name(type_name: str) -> str | None:
    """The name of the field saying when a type's records are due."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    for t in [entity_type, *registry.ancestors(entity_type)]:
        if t.due is not None:
            return t.due
    return None


def _in_state(form: Shape, a: Assignment) -> Q:
    """Records whose state field ``a`` counts in is in one of its states (ADR 0125)."""
    if a.state_field is None:
        return Q()
    return Q(**{f"{_column_of(form, a.state_field)}__in": a.states})


def _named(form: Shape, a: Assignment, persons: Sequence[uuid.UUID] | None, *, able: bool) -> Q:
    """Records whose field ``a`` names one of ``persons`` (anyone, if None) who could (or,
    ``able=False``, couldn't) be named there now."""
    owner = OuterRef(a.project_column)
    if a.many:
        items = a.part.list_model(a.name)._default_manager.filter(entity_id=OuterRef("pk"))
        if persons is not None:
            items = items.filter(item_id__in=persons)
        can = people.eligible(OuterRef("item_id"), OuterRef(owner), a.role)
        return Q(Exists(items.filter(can if able else ~can)))
    path = _path(form, a)
    can = people.eligible(OuterRef(path), owner, a.role)
    named = (
        Q(**{f"{path}__in": persons}) if persons is not None else Q(**{f"{path}__isnull": False})
    )
    # ``OuterRef`` in a filter on the model itself reads its own row: wrap in a subquery.
    rows = form.model._default_manager.filter(pk=OuterRef("pk")).filter(can if able else ~can)
    return named & Q(Exists(rows))


def _per_type(type_name: str, build: Any) -> QuerySet[Any]:  # noqa: ANN401 - a callable
    form = shape(type_name)
    found = Q()
    for a in of(type_name):
        found |= _in_state(form, a) & build(form, a)
    return form.model._default_manager.filter(type=type_name).filter(found).values("pk")


def condition(
    within: Iterable[str] | None,
    column: str,
    op: str,
    persons: Sequence[uuid.UUID] | None = None,
    *,
    value: bool = True,
) -> Q:
    """What the search's ``assigned`` and ``needs_reassigning`` find, among records of
    ``within`` (types; every type if None).

    - ``assigned`` ``eq``/``in``: someone in ``persons`` is assigned it now; ``ne``: not;
      ``is_set``: someone is (``value=False``: work nobody is assigned).
    - ``needs_reassigning`` ``eq``: an assignment field counting in its state names
      someone who can't be named there now (``value=False``: none does)."""
    names = assigned_types(within)
    if column == "needs_reassigning":
        stale = _union([_per_type(n, lambda f, a: _named(f, a, None, able=False)) for n in names])
        return stale if value else ~stale
    if op in ("eq", "in", "ne"):
        mine = _union([_per_type(n, lambda f, a: _named(f, a, persons, able=True)) for n in names])
        return ~mine if op == "ne" else mine
    someone = _union([_per_type(n, lambda f, a: _named(f, a, None, able=True)) for n in names])
    if value:
        return someone
    work = _union([_per_type(n, lambda f, a: Q()) for n in names])
    return work & ~someone


def _union(parts: list[QuerySet[Any]]) -> Q:
    if not parts:
        return Q(pk__in=[])
    return Q(pk__in=parts[0].union(*parts[1:]) if len(parts) > 1 else parts[0])


# --- Changes --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """Whom one assignment field of a record named before a revision, and after."""

    field: str
    label: str
    added: tuple[uuid.UUID, ...]
    removed: tuple[uuid.UUID, ...]


def diff(type_name: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
    """How a revision changed the record's assignment fields: ``after`` holds the values it
    set, ``before`` those they replaced."""
    out = []
    for a in of(type_name):
        if a.name not in after:
            continue
        old, new = _people_in(before.get(a.name)), _people_in(after[a.name])
        added = tuple(p for p in new if p not in old)
        removed = tuple(p for p in old if p not in new)
        if added or removed:
            out.append(Change(a.name, a.label, added, removed))
    return out


def _people_in(value: Any) -> list[uuid.UUID]:  # noqa: ANN401 - a model value
    if value is None:
        return []
    return [uuid.UUID(str(v)) for v in (value if isinstance(value, list) else [value])]


@dataclass
class _Told:
    """What one person is to be told, for one project: the records, and the fields."""

    project: Project
    kind: str
    records: list[tuple[Entity, str]] = field(default_factory=list)


@dataclass
class _Batch:
    actor: User | None
    told: dict[tuple[uuid.UUID, str, uuid.UUID], _Told] = field(default_factory=dict)
    #: Whose work changed, to tell their pages.
    touched: set[uuid.UUID] = field(default_factory=set)


_batch: ContextVar[_Batch | None] = ContextVar("iki_assignments_batch", default=None)


@contextmanager
def batch(actor: User | None) -> Iterator[None]:
    """Changes inside tell each person once, of all of theirs in each project: a bulk
    reassignment, or a run's results. Call it inside the changes' transaction."""
    if _batch.get() is not None:
        yield
        return
    current = _Batch(actor)
    token = _batch.set(current)
    try:
        yield
    finally:
        _batch.reset(token)
    _tell(current)


def changed(
    entity: Entity,
    revision: Revision,
    changes: Sequence[Change],
    actor: User | None,
    *,
    values: Mapping[str, Any] | None = None,
    took: bool = False,
    group: str = "",
) -> None:
    """Records changes to a record's assignment fields that its revision made: audits each,
    which tells webhooks, and tells whoever it names or stops naming. ``group`` names a
    bulk reassignment the change was part of."""
    if not changes:
        return
    project = Project.objects.select_related("team").get(
        pk=cast("uuid.UUID", entity.owner_project_id)
    )
    if values is None:
        from iki_server.entities import services

        values = services.current_values_of([entity])[entity.pk]
    names = {p for c in changes for p in (*c.added, *c.removed)}
    shown = {u.pk: u for u in User.objects.filter(pk__in=names)}
    assignments = {a.name: a for a in of(entity.type)}
    for c in changes:
        log.record(
            "entity.assigned",
            actor=actor,
            target=entity,
            project=project,
            data={
                "field": c.field,
                "label": c.label,
                "added": [_person_data(shown.get(p), p) for p in c.added],
                "removed": [_person_data(shown.get(p), p) for p in c.removed],
                "revision": revision.number,
                "reason": revision.reason,
                **({"took": True} if took or _took.get() else {}),
                **({"group": group} if group else {}),
            },
        )
    own = _batch.get()
    current = own or _Batch(actor)
    for c in changes:
        a = assignments.get(c.field)
        counting = a is not None and a.counts(values)
        told_in = (requests.field_project(entity, c.field) if a is not None else None) or project
        for kind, persons in (
            ("assignment.assigned", c.added),
            ("assignment.unassigned", c.removed),
        ):
            for person in persons:
                current.touched.add(person)
                if kind == "assignment.assigned" and not counting:
                    # Theirs once its state field reaches a state it counts in: told then.
                    continue
                key = (person, kind, told_in.pk)
                told = current.told.setdefault(key, _Told(told_in, kind))
                told.records.append((entity, c.label))
    if own is None:
        _tell(current)


def became(
    entity: Entity,
    revision: Revision,
    state_field: str,
    before: str,
    after: str,
    actor: User | None,
) -> None:
    """Someone set a record's state field from ``before`` to ``after`` (ADR 0125): whoever
    an assignment field counting in that field's states names, that counts now and didn't
    before, is told it's theirs."""
    from iki_server.entities import services

    fields = [
        a
        for a in of(entity.type)
        if a.state_field == state_field and after in a.states and before not in a.states
    ]
    if not fields:
        return
    values = services.current_values_of([entity])[entity.pk]
    project = Project.objects.select_related("team").get(
        pk=cast("uuid.UUID", entity.owner_project_id)
    )
    own = _batch.get()
    current = own or _Batch(actor)
    for a in fields:
        told_in = requests.field_project(entity, a.name) or project
        for person in _people_in(values.get(a.name)):
            if not people.nameable(told_in, a.role).filter(pk=person).exists():
                continue
            key = (person, "assignment.ready", told_in.pk)
            current.told.setdefault(key, _Told(told_in, "assignment.ready")).records.append(
                (entity, a.label)
            )
            current.touched.add(person)
    if own is None:
        _tell(current)


def _person_data(person: User | None, pk: uuid.UUID) -> dict[str, Any]:
    return {"id": str(pk), "name": person.name if person is not None else ""}


def _tell(current: _Batch) -> None:
    from iki_server.audit.log import current_agent
    from iki_server.notifications import services as notifications
    from iki_server.realtime import events

    actor = current.actor
    agent = current_agent()
    # Whoever's told is in the record's project, as the actor is: their name's no secret.
    who = actor.get_full_name() if actor is not None else "Iki"
    recipients = {u.pk: u for u in User.objects.filter(pk__in={k[0] for k in current.told})}
    for (person, kind, _), told in current.told.items():
        user = recipients.get(person)
        if user is None:
            continue
        title, body, path = _message(kind, who, told)
        notifications.notify(
            [user],
            kind,
            title=title,
            body=body,
            path=path,
            project=told.project,
            actor=actor,
            agent=agent,
            about=[entity for entity, _ in told.records],
            # Told with what they were told of moments ago, unread (ADR 0129).
            fold=notifications.Fold(partial(_many, kind, told)),
        )
    for person in current.touched:
        events.publish(person, "work", {})


def _message(kind: str, who: str, told: _Told) -> tuple[str, str, str]:
    project = told.project
    records = told.records
    labels = sorted({label for _, label in records})
    roles = " and ".join(labels)
    distinct = len({e.pk for e, _ in records})
    if distinct == 1:
        entity, label = records[0]
        path = f"/entities/{entity.pk}"
        title = {
            "assignment.assigned": f"{who} assigned you {entity.hrid} as {label}",
            "assignment.unassigned": f"{who} took you off {entity.hrid} as {label}",
            "assignment.ready": f"{entity.hrid} is ready for you, as {label}",
        }[kind]
    else:
        path = f"/work?project={project.pk}"
        what = f"{distinct} records"
        title = {
            "assignment.assigned": f"{who} assigned you {what} as {roles}",
            "assignment.unassigned": f"{who} took you off {what} as {roles}",
            "assignment.ready": f"{what} are ready for you, as {roles}",
        }[kind]
    return title, _body(kind, project), path


def _body(kind: str, project: Project) -> str:
    return (
        f"In {project.name}. It's on My work."
        if kind != "assignment.unassigned"
        else f"In {project.name}. It's no longer on My work."
    )


def _many(kind: str, told: _Told, count: int) -> tuple[str, str, str]:
    """One about ``count`` records, after more joined it, from whoever."""
    title = {
        "assignment.assigned": f"You were assigned {count} records",
        "assignment.unassigned": f"You were taken off {count} records",
        "assignment.ready": f"{count} records are ready for you",
    }[kind]
    return title, _body(kind, told.project), f"/work?project={told.project.pk}"


# --- Work -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItem:
    """A record on someone's work: assigned to them, or open to their lab roles."""

    entity: Entity
    #: The assignment fields that name them, or (open work) that they could take.
    fields: tuple[str, ...]
    due: dt.date | dt.datetime | None


#: How My work can be sorted: soonest due first (undated last), or latest changed first.
SORTS = ("due", "updated")
_SALT = "iki.entities.assignments.work"


class BadCursor(Exception):  # noqa: N818 - reads as "the cursor is bad"
    pass


@dataclass(frozen=True)
class WorkFilters:
    projects: tuple[uuid.UUID, ...] = ()
    types: tuple[str, ...] = ()
    #: Only records whose state fields are in these states (ADR 0125), by field: records
    #: in any of a field's states, and of every field named.
    states: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    sort: str = "due"


def work(
    user: User, filters: WorkFilters, cursor: str | None, limit: int
) -> tuple[list[WorkItem], str | None]:
    """A page of what's assigned to ``user`` now, across the projects they read."""

    def mine(type_name: str, form: Shape) -> tuple[Q, list[Assignment]]:
        found = Q()
        for a in of(type_name):
            found |= _in_state(form, a) & _named(form, a, [user.pk], able=True)
        return found, list(of(type_name))

    return _page(user, filters, cursor, limit, mine, assigned_to=user.pk)


def open_work(
    user: User, filters: WorkFilters, cursor: str | None, limit: int
) -> tuple[list[WorkItem], str | None]:
    """A page of records open to ``user``'s lab roles: an assignment field limited to a role
    they hold in its project counts in its states, and names nobody who could be named."""
    readable = list(access.readable_projects(user))
    permissions.remember_roles(user, readable)
    held = {p.pk: permissions.lab_roles(user, p) for p in readable}

    def open_to_me(type_name: str, form: Shape) -> tuple[Q, list[Assignment]]:
        found = Q()
        fields = []
        for a in of(type_name):
            if a.role is None:
                continue
            projects = [pk for pk, roles in held.items() if a.role in roles]
            if not projects:
                continue
            fields.append(a)
            nobody = ~_named(form, a, None, able=True)
            found |= _in_state(form, a) & Q(**{f"{a.project_column}__in": projects}) & nobody
        return (found if fields else Q(pk__in=[])), fields

    return _page(user, filters, cursor, limit, open_to_me, assigned_to=None)


def _page(
    user: User,
    filters: WorkFilters,
    cursor: str | None,
    limit: int,
    build: Any,  # noqa: ANN401 - (type, form) → (condition, fields)
    *,
    assigned_to: uuid.UUID | None,
) -> tuple[list[WorkItem], str | None]:
    key = filters.sort
    after = _decode(cursor, key) if cursor else None
    names = assigned_types({s for t in filters.types for s in subtypes(t)} or None)
    rows: list[tuple[Any, Entity, list[Assignment], str]] = []
    for name in names:
        form = shape(name)
        condition, fields = build(name, form)
        if not fields:
            continue
        found = (
            access.visible_of(user, form.model)
            .filter(type=name, archived_at__isnull=True)
            .filter(condition)
        )
        if filters.projects:
            # A request's work is in the service it was sent to, too (ADR 0132).
            found = found.filter(
                Q(owner_project_id__in=filters.projects)
                | Q(iki_request__service_id__in=filters.projects)
            )
        if filters.states:
            own = form.fields()
            if any(
                n not in own or own[n].type != "state" or own[n].removed for n in filters.states
            ):
                continue
            for n, wanted in filters.states.items():
                found = found.filter(**{f"{_column_of(form, n)}__in": wanted})
        due = due_field(name)
        if key == "due":
            sort: Any = (
                Cast(F(_column_of(form, due_name(name))), output_field=_TIMESTAMP)
                if due is not None
                else None
            )
        else:
            sort = F("updated_at")
        found = (
            found.annotate(work_sort=sort)
            if sort is not None
            else found.annotate(work_sort=Value(None, output_field=_TIMESTAMP))
        )
        found = _after(found, key, after)
        order: list[Any] = (
            [F("work_sort").asc(nulls_last=True), "pk"]
            if key == "due"
            else [F("work_sort").desc(), "-pk"]
        )
        rows.extend(
            (entity.work_sort, entity, fields, name)  # type: ignore[attr-defined]
            for entity in found.select_related("owner_project").order_by(*order)[: limit + 1]
        )
    rows.sort(key=lambda r: _sort_key(key, r[0], r[1].pk))
    more = len(rows) > limit
    rows = rows[:limit]
    entities = [r[1] for r in rows]
    values = _values_of(entities) if entities else {}
    items = []
    for _sort_value, entity, fields, name in rows:
        named = tuple(
            a.name
            for a in fields
            if a.counts(values.get(entity.pk, {}))
            and (
                assigned_to is None
                or assigned_to in _people_in(values.get(entity.pk, {}).get(a.name))
            )
        )
        due = due_field(name)
        items.append(
            WorkItem(
                entity=entity,
                fields=named,
                due=values.get(entity.pk, {}).get(due_name(name)) if due is not None else None,
            )
        )
    last = rows[-1] if rows and more else None
    next_cursor = _encode(key, last[0], last[1].pk) if last is not None else None
    return items, next_cursor


def _values_of(entities: list[Entity]) -> dict[Any, dict[str, Any]]:
    from iki_server.entities import services

    return services.current_values_of(entities)


_TIMESTAMP: Any = None


def _timestamp() -> Any:  # noqa: ANN401 - a Django field
    from django.db.models import DateTimeField

    return DateTimeField()


_TIMESTAMP = _timestamp()


def _sort_key(key: str, value: Any, pk: uuid.UUID) -> tuple[Any, ...]:  # noqa: ANN401
    if key == "due":
        return (value is None, value or dt.datetime.min.replace(tzinfo=dt.UTC), pk)
    stamp = value.timestamp() if value is not None else 0.0
    return (-stamp, _reverse(pk))


def _reverse(pk: uuid.UUID) -> int:
    return -pk.int


def _after(found: QuerySet[Any], key: str, after: tuple[Any, uuid.UUID] | None) -> QuerySet[Any]:
    if after is None:
        return found
    value, pk = after
    if key == "due":
        if value is None:
            return found.filter(work_sort__isnull=True, pk__gt=pk)
        return found.filter(
            Q(work_sort__gt=value) | Q(work_sort=value, pk__gt=pk) | Q(work_sort__isnull=True)
        )
    return found.filter(Q(work_sort__lt=value) | Q(work_sort=value, pk__lt=pk))


def _encode(key: str, value: Any, pk: uuid.UUID) -> str:  # noqa: ANN401
    stamp = value.isoformat() if isinstance(value, dt.datetime) else None
    return signing.Signer(salt=_SALT).sign_object([key, stamp, str(pk)], compress=True)


def _decode(cursor: str, key: str) -> tuple[Any, uuid.UUID]:
    try:
        sort, stamp, pk = signing.Signer(salt=_SALT).unsign_object(cursor)
    except (signing.BadSignature, ValueError, TypeError) as error:
        raise BadCursor("Not a cursor from this API.") from error
    if sort != key:
        raise BadCursor("This cursor is from a list sorted another way.")
    return (dt.datetime.fromisoformat(stamp) if stamp else None), uuid.UUID(pk)


# --- Reassignment ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stale:
    """Someone a record's assignment field names who can't be named there now."""

    field: str
    person: uuid.UUID
    why: str


def stale_of(
    entities: Sequence[Entity], values: Mapping[Any, Mapping[str, Any]]
) -> dict[Any, list[Stale]]:
    """For each record, who its assignment fields counting in its states name that can't
    be named there now, and why."""
    out: dict[Any, list[Stale]] = {}
    wanted: dict[Any, list[tuple[Entity, Assignment, uuid.UUID]]] = defaultdict(list)
    services = requests.services_of(entities)
    for entity in entities:
        if entity.owner_project_id is None:
            continue
        for a in of(entity.type):
            if not a.counts(values.get(entity.pk, {})):
                continue
            project_id = services.get(entity.pk) if a.service else entity.owner_project_id
            if project_id is None:
                continue
            for person in _people_in(values.get(entity.pk, {}).get(a.name)):
                wanted[project_id].append((entity, a, person))
    if not wanted:
        return out
    projects = {p.pk: p for p in Project.objects.filter(pk__in=wanted)}
    users = {
        u.pk: u for u in User.objects.filter(pk__in={p for w in wanted.values() for *_, p in w})
    }
    for project_id, named in wanted.items():
        project = projects[project_id]
        able: dict[str | None, set[uuid.UUID]] = {}
        for _, a, _ in named:
            if a.role not in able:
                able[a.role] = set(
                    people.nameable(project, a.role)
                    .filter(pk__in={p for *_, p in named})
                    .values_list("pk", flat=True)
                )
        for entity, a, person in named:
            if person in able[a.role]:
                continue
            user = users.get(person)
            why = (
                people.why_not(user, project, a.role) if user is not None else None
            ) or "can't be named here"
            out.setdefault(entity.pk, []).append(Stale(a.name, person, why))
    return out


def check_people(user_ids: Iterable[Any]) -> int:
    """Tells each project's Admins how many of its records name one of these people in an
    assignment field that counts now, who can't be named there any more: they were
    deactivated, removed from it, or lost the field's lab role. Returns how many projects
    were told."""
    from iki_server.notifications import services as notifications
    from iki_server.org.models import ProjectRole

    told = 0
    for person in User.objects.filter(pk__in=list(user_ids)):
        counts: dict[uuid.UUID, int] = defaultdict(int)
        roles: dict[uuid.UUID, set[str]] = defaultdict(set)
        for name in assigned_types():
            form = shape(name)
            for a in of(name):
                rows = (
                    form.model._default_manager.filter(
                        type=name, archived_at__isnull=True, purged_at__isnull=True
                    )
                    .filter(_in_state(form, a) & _named(form, a, [person.pk], able=False))
                    .values_list(a.project_column, flat=True)
                )
                for project_id in rows:
                    counts[project_id] += 1
                    if a.role is not None:
                        roles[project_id].add(a.role)
        for project in Project.objects.select_related("team").filter(pk__in=counts):
            n = counts[project.pk]
            why = people.why_not(person, project) or (
                "no longer "
                + " or ".join(sorted(lab_roles.label(r) for r in roles[project.pk]))
                + f" in {project.name}"
            )
            who = person.name or "Someone"
            records = f"{n} record{'s' if n != 1 else ''}"
            notifications.notify(
                permissions.members_with(project, ProjectRole.ADMIN),
                "assignment.needs_reassigning",
                title=f"{records} in {project.name} need{'s' if n == 1 else ''} reassigning",
                body=f"{who} is assigned {'it' if n == 1 else 'them'}, and can't be any more: "
                f"{why}.",
                path=f"/projects/{project.slug}/entities?q=needs%3Areassigning",
                project=project,
            )
            told += 1
    return told


# --- Taking and reassigning -----------------------------------------------------------------------


def takeable(
    user: User,
    entities: Sequence[Entity],
    values: Mapping[Any, Mapping[str, Any]],
    stale: Mapping[Any, Sequence[Stale]],
) -> dict[Any, list[str]]:
    """For each record, the assignment fields ``user`` may take it in now: limited to a lab
    role they hold there, counting in its states, and naming nobody who can be named (a
    list: not them already). Lab-role work: a Viewer holding the role may. Not while a
    state locks it: its people are locked with its other values."""
    out: dict[Any, list[str]] = {}
    services = requests.service_projects_of(entities)
    for entity in entities:
        owner = entity.owner_project
        if entity.locked or entity.archived_at is not None or owner is None:
            continue
        stale_fields = {s.field for s in stale.get(entity.pk, ())}
        found = []
        for a in of(entity.type):
            project = services.get(entity.pk) if a.service else owner
            if project is None or not permissions.can(
                user, permissions.Action.PROJECT_SET_STATE, project
            ):
                continue
            held = permissions.lab_roles(user, project)
            if a.role is None or a.role not in held or not a.counts(values.get(entity.pk, {})):
                continue
            named = _people_in(values.get(entity.pk, {}).get(a.name))
            if user.pk in named:
                continue
            if a.many or not named or a.name in stale_fields:
                found.append(a.name)
        if found:
            out[entity.pk] = found
    return out


def take(user: User, entity: Entity, name: str, *, expected_revision: int, reason: str = "") -> Any:  # noqa: ANN401 - a Saved
    """Assigns ``user`` the record in assignment field ``name``, theirs to take as
    :func:`takeable` says: a revision, like any edit. Raises :class:`PermissionDenied`
    when they may not, and :class:`Refused` when the record can't change now."""
    from django.core.exceptions import PermissionDenied

    from iki_server.entities import services
    from iki_server.org.services import Refused

    assignment = next((a for a in of(entity.type) if a.name == name), None)
    if assignment is None:
        raise Refused(f"{entity.hrid} has no assignment field `{name}`.")
    if entity.owner_project is None:
        raise Refused(f"{entity.hrid} is a registry entry: its projects' records are assigned.")
    project = requests.field_project(entity, name)
    if project is None:
        raise Refused(
            f"{assignment.label} is the service's: it's taken once the request is submitted."
        )
    if assignment.role is None:
        raise PermissionDenied(
            f"{assignment.label} isn't limited to a lab role: an Editor assigns it."
        )
    if assignment.role not in permissions.lab_roles(user, project) or not permissions.can(
        user, permissions.Action.PROJECT_SET_STATE, project
    ):
        raise PermissionDenied(
            f"Only someone with the {lab_roles.label(assignment.role)} role in {project.name} "
            f"takes it as {assignment.label}."
        )
    why = states.locked_why(entity, doing="be taken")
    if why:
        raise Refused(why)
    values = services.current_values_of([entity])
    stale = stale_of([entity], values)
    if name not in takeable(user, [entity], values, stale).get(entity.pk, []):
        if not assignment.counts(values[entity.pk]):
            raise Refused(f"{entity.hrid} isn't {assignment.label}'s to work on in its state.")
        raise Refused(f"{entity.hrid} isn't open as {assignment.label}: someone has it.")
    current = values[entity.pk].get(name)
    value: Any = (
        [*(str(p) for p in current or []), str(user.pk)] if assignment.many else str(user.pk)
    )
    with _taking():
        return services.update(
            user,
            entity,
            {name: value},
            expected_revision=expected_revision,
            reason=reason or f"Took it as {assignment.label}.",
            taking=True,
        )


_took: ContextVar[bool] = ContextVar("iki_assignments_took", default=False)


@contextmanager
def _taking() -> Iterator[None]:
    """Inside, the record's assignment changes are someone taking it: its audit says so."""
    token = _took.set(True)
    try:
        yield
    finally:
        _took.reset(token)


@dataclass(frozen=True)
class Skipped:
    entity: uuid.UUID
    hrid: str
    why: str


@dataclass(frozen=True)
class Reassigned:
    changed: list[Entity]
    skipped: list[Skipped]
    group: str


#: How many records one reassignment changes at most.
MAX_REASSIGN = 500
#: How one reassignment changes each record's field.
REASSIGN_OPS = ("set", "add", "remove")


def reassign(
    user: User,
    ids: Sequence[uuid.UUID],
    name: str,
    person: str | None,
    *,
    op: str = "set",
    reason: str = "",
) -> Reassigned:
    """Names ``person`` (an ID or email address; None to empty it) in assignment field
    ``name`` of each record: ``set`` it, or for a list ``add`` or ``remove`` them. Each change
    is its record's own revision, audited as part of one reassignment, which tells each
    person once. A record that can't change (you can't edit it, its state locks it, the
    field isn't its, the person can't be named in its project) is skipped, with why."""
    from django.db import transaction

    from iki_server.entities import services
    from iki_server.entities.values import Invalid
    from iki_server.org.services import Refused

    group = uuid.uuid4().hex
    changed: list[Entity] = []
    skipped: list[Skipped] = []
    wanted = list(dict.fromkeys(ids))
    found = {
        e.pk: e for e in access.visible(user).filter(pk__in=wanted).select_related("owner_project")
    }
    with transaction.atomic(), batch(user):
        values = services.current_values_of(list(found.values()))
        for pk in wanted:
            entity = found.get(pk)
            if entity is None:
                skipped.append(Skipped(pk, "", "No such entity, or you can't read it."))
                continue
            why = _cannot_reassign(user, entity, name)
            if why is not None:
                skipped.append(Skipped(pk, entity.hrid, why))
                continue
            assignment = next(a for a in of(entity.type) if a.name == name)
            value = _reassigned(assignment, _people_in(values[pk].get(name)), person, op)
            try:
                with transaction.atomic():
                    saved = services.update(
                        user,
                        entity,
                        {name: value},
                        expected_revision=entity.revision,
                        reason=reason,
                        group=group,
                    )
            except Invalid as error:
                said = error.errors.get(name) or [str(error)]
                skipped.append(Skipped(pk, entity.hrid, f"{assignment.label} {' '.join(said)}"))
                continue
            except services.Stale:
                skipped.append(Skipped(pk, entity.hrid, "Someone changed it meanwhile."))
                continue
            except Refused as error:
                skipped.append(Skipped(pk, entity.hrid, str(error)))
                continue
            if saved.revision is not None:
                changed.append(cast("Entity", saved.entity))
    return Reassigned(changed, skipped, group)


def _reassigned(a: Assignment, current: list[uuid.UUID], person: str | None, op: str) -> Any:  # noqa: ANN401 - a value as the API takes it
    named = [str(p) for p in current]
    if not a.many:
        if op == "remove":
            # Takes off whoever it names, only if that's them.
            return None if person is None or (named and named[0] == person) else named[0]
        return person
    if op == "add":
        return named if person is None or person in named else [*named, person]
    if op == "remove":
        return [p for p in named if person is not None and p != person]
    return [person] if person else []


def _cannot_reassign(user: User, entity: Entity, name: str) -> str | None:
    if not any(a.name == name for a in of(entity.type)):
        label = registry.types()[entity.type].label or entity.type
        return f"{label} records have no field `{name}` to assign."
    owner = requests.field_project(entity, name) if entity.owner_project else None
    if owner is None or not permissions.can(user, permissions.Action.PROJECT_EDIT, owner):
        return f"It needs you as an Editor in {owner.name if owner else 'its project'}."
    if entity.archived_at is not None:
        return "It's archived: restore it first."
    return states.locked_why(entity, doing="be reassigned")

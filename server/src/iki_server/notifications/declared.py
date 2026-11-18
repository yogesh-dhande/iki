"""Sending the notifications types declare (ADR 0129).

A type says when its records tell someone (a record made, a field changing, a state field
entering or leaving states, a field of a record one of its references names) and whom:
the people its fields name, holders of a lab role in its project, its creator, its
watchers, and whoever made the change. The services call :func:`changed` and
:func:`created` in each change's transaction, after its values are written, on every path
(the web, the API, MCP, runs, triggers), so a notification exists exactly when its change
does. A bulk import, which tells webhooks of its changes in one event, tells nobody record
by record, as it fires no trigger (:func:`iki_server.entities.services.quietly`).

Each firing is checked at send time, for each person: they're active and a person, not a
service account; they can read the record (and, for a change to a record it refers to,
that one too); they haven't turned the notification off, there or everywhere, nor muted
the record, unless the plugin requires it. Whoever made a change with their own hands
isn't told of it, unless the type says so: an agent's change, a run's or a trigger's tells
the person it was made for, who didn't make it themselves. Each firing is audited
(``notification.sent``) with whom it told and whom it didn't and why, so a lab can answer
"was QA told?"; each email it sends is too (``notification.emailed``).

What it says is the type's message, over the record's values, and only in Iki: the
inbox shows it only while the person can still read the record. Emails say what it is,
which record and where, and link to it (:mod:`iki_server.notifications.tasks`). A person
told of several records of one kind in a project at once, or within minutes, gets one
notification listing them (:func:`iki_server.notifications.services.deliver`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache, partial
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import structlog

from iki_server.accounts.models import Kind as UserKind
from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.notifications import kinds, services
from iki_server.notifications.models import Delivery, Watch, WatchMode
from iki_spec import titles, units
from iki_spec.model import EntityType, Field, TypeNotification

if TYPE_CHECKING:
    from iki_server.entities.models import Entity, Revision
    from iki_server.org.models import Project

logger = structlog.get_logger("iki.notifications")

__all__ = ["Declared", "changed", "created", "describe", "of_type"]

#: The most records a change to one they refer to tells of: a booking's instrument, say.
MAX_REFERRING = 1000
#: The most people a firing's audit event names, told and not.
MAX_AUDITED = 500
#: Ways a change is made that aren't someone's own hands (ADR 0072, ADR 0080).
_ON_THEIR_BEHALF = frozenset({"run", "trigger", "system"})


@dataclass(frozen=True)
class Declared:
    """A notification a type declares, as its records and its subtypes' send it."""

    kind: str
    type: EntityType
    n: TypeNotification


@cache
def of_type(type_name: str) -> tuple[Declared, ...]:
    """The notifications a type's records send: its own and its ancestors'."""
    from iki_server.entities import registry

    found = registry.types().get(type_name)
    if found is None:
        return ()
    chain = [*reversed(registry.ancestors(found)), found]
    return tuple(
        Declared(kinds.key_of(t.qualified_name, n.name), t, n)
        for t in chain
        for n in t.notifications.values()
    )


@cache
def _referring() -> dict[tuple[str, str], tuple[tuple[str, Declared], ...]]:
    """(the type a reference names, its field) → (each type whose records refer to it, the
    notification its change sends): what a booking's instrument going out of service tells."""
    from iki_server.entities import registry
    from iki_server.entities.values import shape

    out: dict[tuple[str, str], list[tuple[str, Declared]]] = defaultdict(list)
    for name, entity_type in registry.types().items():
        if entity_type.abstract or name == "iki/core.Entity":
            continue
        for d in of_type(name):
            if d.n.ref is None:
                continue
            f = shape(name).fields().get(d.n.ref)
            if f is not None and f.to is not None:
                out[(f.to, d.n.target)].append((name, d))
    return {k: tuple(v) for k, v in out.items()}


def _of_kind(type_name: str) -> list[str]:
    """A type and its ancestors, nearest first: what a reference to it may name."""
    from iki_server.entities import registry

    found = registry.types().get(type_name)
    if found is None:
        return []
    return [type_name, *(t.qualified_name for t in registry.ancestors(found)), "iki/core.Entity"]


# --- Changes ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """A change as its notifications tell of it."""

    entity: Entity
    revision: Revision
    actor: User | None

    @property
    def by_hand(self) -> bool:
        """Whether the actor made it themselves, not through an agent, a run or a trigger."""
        return (
            self.actor is not None
            and self.revision.agent_id is None
            and self.revision.via not in _ON_THEIR_BEHALF
        )


def created(entity: Entity, revision: Revision, actor: User | None) -> None:
    """A record was made: what its type tells of that."""
    if _quiet():
        return
    change = Change(entity, revision, actor)
    for d in of_type(entity.type):
        if d.n.created:
            _fire(d, entity, change)


def changed(
    entity: Entity,
    revision: Revision,
    values: Mapping[str, tuple[Any, Any]],
    actor: User | None,
) -> None:
    """A revision of a record changed these fields, each (before, after): what its type, and
    types whose records refer to it, tell of that."""
    if _quiet() or entity.registry_id is not None or not values:
        return
    change = Change(entity, revision, actor)
    for d in of_type(entity.type):
        if d.n.ref is None and not d.n.created and _sends(d.n, values.get(d.n.field)):
            _fire(d, entity, change)
    referring = _referring()
    if not referring:
        return
    for kind in _of_kind(entity.type):
        for name, before_after in values.items():
            for type_name, d in referring.get((kind, name), ()):
                if _sends(d.n, before_after):
                    for record in _referrers(type_name, d, entity):
                        _fire(d, record, change)


def _quiet() -> bool:
    from iki_server.entities import services as entities

    return entities._quiet.get()


def _sends(n: TypeNotification, before_after: tuple[Any, Any] | None) -> bool:
    if before_after is None:
        return False
    before, after = before_after
    if before == after:
        return False
    if n.to_values and after not in n.to_values:
        return False
    return not (n.from_values and before not in n.from_values)


def _referrers(type_name: str, d: Declared, target: Entity) -> list[Entity]:
    """The records of ``type_name`` whose reference names ``target``: by it, or by its
    registry entry, among those in its project (ADR 0115)."""
    from iki_server.entities import search
    from iki_server.entities.values import shape

    form = shape(type_name)
    assert d.n.ref is not None  # noqa: S101 - only referring ones
    col = search.column(form, d.n.ref)
    if col is None or col.entry is not None:
        return []
    named = form.model._default_manager.filter(
        archived_at__isnull=True, purged_at__isnull=True, registry__isnull=True
    )
    if target.entry_id is not None:
        found = named.filter(
            **{col.path: target.entry_id}, owner_project_id=target.owner_project_id
        )
    else:
        found = named.filter(**{col.path: target.pk})
    records = list(found.order_by("-created_at")[: MAX_REFERRING + 1])
    if len(records) > MAX_REFERRING:
        logger.warning(
            "notification_too_many_referring",
            kind=d.kind,
            entity=str(target.pk),
            told=MAX_REFERRING,
        )
        records = records[:MAX_REFERRING]
    return records


# --- Firing -------------------------------------------------------------------------------------


@dataclass
class _Candidate:
    user: User
    #: Why they'd be told: "field", "role", "creator", "watcher" or "actor".
    why: str


def _fire(d: Declared, record: Entity, change: Change) -> None:
    """Tells whom the notification names about ``record``, as each may be told of it."""
    from iki_server.entities import access
    from iki_server.entities import services as entities
    from iki_server.org.models import Project

    project = (
        Project.objects.select_related("team").filter(pk=record.owner_project_id).first()
        if record.owner_project_id
        else None
    )
    if project is None or record.archived_at is not None:
        return
    values = entities.current_values_of([record])[record.pk]
    if d.n.where is not None and not _found(record, d):
        return
    kind = kinds.get(d.kind)
    candidates = _candidates(d.n, record, values, project, change)
    told: list[dict[str, Any]] = []
    untold: list[dict[str, Any]] = []
    other = change.entity if change.entity.pk != record.pk else None
    muted = set(
        Watch.objects.filter(
            entity=record, mode=WatchMode.MUTED, user__in=[c.user.pk for c in candidates]
        ).values_list("user", flat=True)
    )
    # Who can read it, and the record that changed: a few queries for everyone at once.
    people = [c.user for c in candidates]
    can_read = access.readers(record, people)
    can_read_other = access.readers(other, people) if other is not None else can_read
    ready: list[_Candidate] = []
    for c in candidates:
        why_not = None
        if not c.user.is_active:
            why_not = "deactivated"
        elif c.user.kind != UserKind.PERSON or c.user.imported_from:
            why_not = "can't sign in here"
        elif (
            change.actor is not None
            and c.user.pk == change.actor.pk
            and change.by_hand
            and not d.n.tell_actor
        ):
            why_not = "made the change"
        elif c.user.pk not in can_read:
            why_not = "can't read it"
        elif other is not None and c.user.pk not in can_read_other:
            why_not = f"can't read {other.hrid}"
        elif c.user.pk in muted and kind.can_turn_off:
            why_not = "muted it"
        if why_not is not None:
            untold.append(_person(c.user, why_not))
        else:
            ready.append(c)
    chosen = services.deliveries([c.user for c in ready], kind, project)
    shown = _Shown(record, values, d, [c.user for c in ready])
    for c in ready:
        delivery = chosen[c.user.pk]
        if delivery == Delivery.OFF:
            untold.append(_person(c.user, "turned it off"))
            continue
        title, body = shown.message(c.user)
        services.deliver(
            c.user,
            kind,
            delivery,
            title=title,
            body=body,
            path=f"/entities/{record.pk}",
            project=project,
            actor=change.actor,
            agent=change.revision.agent,
            about=[record],
            fold=services.Fold(partial(_many, d, project)),
        )
        told.append(
            {
                **_person(c.user),
                "as": c.why,
                "email": delivery == Delivery.EMAIL and services.email_enabled(),
            }
        )
    if not told and not untold:
        return
    audit.record(
        "notification.sent",
        actor=change.actor,
        target=record,
        project=project,
        agent=change.revision.agent,
        data={
            "kind": d.kind,
            "label": d.n.label,
            "required": d.n.required,
            "revision": change.revision.number,
            **({"about": {"id": str(other.pk), "hrid": other.hrid}} if other else {}),
            "told": told[:MAX_AUDITED],
            "not_told": untold[:MAX_AUDITED],
            **(
                {"more": more}
                if (more := max(0, len(told) - MAX_AUDITED) + max(0, len(untold) - MAX_AUDITED))
                else {}
            ),
        },
    )


def _found(record: Entity, d: Declared) -> bool:
    """Whether the notification's conditions find the record."""
    from iki_server.entities import search
    from iki_server.entities.values import shape, subtypes

    form = shape(record.type)
    compiler = search._Compiler(None, form, None, subtypes(d.type.qualified_name))
    assert d.n.where is not None  # noqa: S101 - only with conditions
    q = compiler.condition(d.n.where, "#/where")
    if compiler.errors:
        logger.warning("notification_conditions_failed", kind=d.kind, errors=compiler.errors)
        return False
    return bool(form.model._default_manager.filter(pk=record.pk).filter(q).exists())


def _candidates(
    n: TypeNotification,
    record: Entity,
    values: Mapping[str, Any],
    project: Project,
    change: Change,
) -> list[_Candidate]:
    """Whom it names, each once, for the first reason it names them."""
    from iki_server.entities import people

    wanted: dict[uuid.UUID, str] = {}

    def add(ids: Sequence[Any], why: str) -> None:
        for pk in ids:
            if pk is not None:
                wanted.setdefault(uuid.UUID(str(pk)), why)

    for name in n.tell_fields:
        value = values.get(name)
        add(value if isinstance(value, list) else [value], "field")
    for role in n.tell_roles:
        add(list(people.nameable(project, role).values_list("pk", flat=True)), "role")
    if n.tell_creator:
        add([record.created_by_id], "creator")
    if n.tell_watchers:
        add(
            list(
                Watch.objects.filter(entity=record, mode=WatchMode.WATCHING).values_list(
                    "user", flat=True
                )
            ),
            "watcher",
        )
    if n.tell_actor and change.actor is not None:
        add([change.actor.pk], "actor")
    found = User.objects.in_bulk(list(wanted))
    return [_Candidate(found[pk], why) for pk, why in wanted.items() if pk in found]


def _person(user: User, why_not: str | None = None) -> dict[str, Any]:
    return {
        "id": str(user.pk),
        "name": str(user.get_full_name()),
        **({"why": why_not} if why_not else {}),
    }


def _many(d: Declared, project: Project, count: int) -> tuple[str, str, str]:
    """How one about ``count`` records of the kind reads: none of their values."""
    plural = d.type.label_plural or d.type.label or d.type.name
    return (
        f"{count} {plural}: {d.n.label}",
        "",
        f"/projects/{project.slug}/entities?type={quote(d.type.qualified_name, safe='/.')}",
    )


# --- What it says -------------------------------------------------------------------------------


class _Shown:
    """A record's message, as each person told of it may read it: the people it names by
    name, and the records it refers to by HRID, where they can read them."""

    def __init__(
        self, record: Entity, values: Mapping[str, Any], d: Declared, readers: Sequence[User]
    ) -> None:
        from iki_server.entities import access
        from iki_server.entities.models import Entity
        from iki_server.entities.values import shape

        self.record = record
        self.values = values
        self.d = d
        self.fields = shape(record.type).fields()
        named = {
            n
            for template in (d.n.title, d.n.body)
            if template
            for n in _names(template)
            if n in self.fields
        }
        self.people = _names_of([values.get(n) for n in named if self.fields[n].type == "person"])
        refs = {
            uuid.UUID(str(values[n]))
            for n in named
            if self.fields[n].type == "ref" and values.get(n) is not None
        }
        # The records it refers to, and who of those told can read each: a few queries a
        # record, however many are told.
        found = Entity.objects.filter(pk__in=refs) if refs else Entity.objects.none()
        self.refs = {e.pk: (e.hrid, access.readers(e, readers)) for e in found}
        self._by_readable: dict[frozenset[uuid.UUID], tuple[str, str]] = {}

    def message(self, reader: User) -> tuple[str, str]:
        readable = frozenset(pk for pk, (_, who) in self.refs.items() if reader.pk in who)
        if readable not in self._by_readable:
            hrids = {pk: self.refs[pk][0] for pk in readable}
            title = (
                self.render(self.d.n.title, hrids)
                if self.d.n.title
                else f"{self.record.hrid}: {self.d.n.label}"
            )
            body = self.render(self.d.n.body, hrids) if self.d.n.body else ""
            self._by_readable[readable] = (title or f"{self.record.hrid}: {self.d.n.label}", body)
        return self._by_readable[readable]

    def render(self, template: str, hrids: Mapping[Any, str]) -> str:
        def part(expression: str) -> str:
            for token in titles.TOKEN.finditer(expression):
                string, word = token.group("string"), token.group("word")
                if string is not None:
                    if string[1:-1]:
                        return string[1:-1]
                elif word == "hrid":
                    return self.record.hrid
                elif word is not None and word != "or" and word in self.fields:
                    shown = self.value(self.fields[word], self.values.get(word), hrids)
                    if shown:
                        return shown
            return ""

        text = titles._PART.sub(lambda m: part(m.group(1)), template)
        return " ".join(text.split())

    def value(self, f: Field, value: Any, hrids: Mapping[Any, str]) -> str:  # noqa: ANN401 - a model value
        if value is None:
            return ""
        if f.type == "state":
            state = f.states.get(str(value))
            return state.label if state is not None else str(value)
        if f.type == "person":
            return self.people.get(uuid.UUID(str(value)), "")
        if f.type == "ref":
            return hrids.get(uuid.UUID(str(value)), "")
        if isinstance(value, dt.datetime):
            return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        if isinstance(value, dt.date):
            return value.isoformat()
        if f.type == "quantity" and f.unit and isinstance(value, int | float):
            return f"{value:g} {units.symbol(f.unit)}".strip()
        return titles._value(f, value)


def _names(template: str) -> set[str]:
    return {
        m.group("word")
        for part in titles._PART.findall(template)
        for m in titles.TOKEN.finditer(part)
        if m.group("word")
    }


def _names_of(ids: Sequence[Any]) -> dict[uuid.UUID, str]:
    wanted = {uuid.UUID(str(i)) for i in ids if i is not None}
    return {u.pk: str(u.get_full_name()) for u in User.objects.filter(pk__in=wanted)}


# --- What a type's page shows -------------------------------------------------------------------


def describe(entity_type: EntityType) -> list[dict[str, Any]]:
    """A type's notifications as its page previews them: when each is sent and whom it
    tells, in words, from the manifest."""
    from iki_server.entities import registry
    from iki_server.entities.values import shape
    from iki_server.org import lab_roles

    if entity_type.qualified_name not in registry.types():
        return []
    fields = shape(entity_type.qualified_name).fields()
    out = []
    for d in of_type(entity_type.qualified_name):
        n = d.n
        out.append(
            {
                "name": n.name,
                "kind": d.kind,
                "label": n.label,
                "description": n.description,
                "declared_by": d.type.qualified_name,
                "when": _when(n, fields),
                "only_some": n.where is not None,
                "tells": _tells(n, fields, lab_roles.label),
                "email": n.email,
                "required": n.required,
            }
        )
    return out


def _label(f: Field | None, name: str) -> str:
    return f.label if f is not None and f.label else name


def _values(f: Field | None, values: tuple[Any, ...]) -> str:
    def one(v: Any) -> str:  # noqa: ANN401 - a manifest value
        if f is not None and f.type == "state" and str(v) in f.states:
            return f.states[str(v)].label
        if isinstance(v, bool):
            return "yes" if v else "no"
        return str(v)

    shown = [one(v) for v in values]
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " or " + shown[-1]


def _when(n: TypeNotification, fields: Mapping[str, Field]) -> str:
    from iki_server.entities import registry
    from iki_server.entities.values import shape

    if n.created:
        return "When one is made"
    if n.ref is not None:
        ref = fields.get(n.ref)
        target_type = ref.to if ref is not None else None
        target = (
            shape(target_type).fields().get(n.target)
            if target_type and target_type in registry.types()
            else None
        )
        what = f"its {_label(ref, n.ref)}’s {_label(target, n.target)}"
    else:
        target = fields.get(n.field)
        what = f"its {_label(target, n.field)}"
    if n.to_values and n.from_values:
        return (
            f"When {what} changes from {_values(target, n.from_values)} "
            f"to {_values(target, n.to_values)}"
        )
    if n.to_values:
        return f"When {what} changes to {_values(target, n.to_values)}"
    if n.from_values:
        return f"When {what} changes from {_values(target, n.from_values)}"
    return f"When {what} changes"


def _tells(n: TypeNotification, fields: Mapping[str, Field], role_label: Any) -> list[str]:  # noqa: ANN401 - a callable
    out = [f"Whoever its {_label(fields.get(f), f)} names" for f in n.tell_fields]
    out += [f"Everyone with the {role_label(r)} role in its project" for r in n.tell_roles]
    if n.tell_creator:
        out.append("Whoever made it")
    if n.tell_watchers:
        out.append("Whoever watches it")
    if n.tell_actor:
        out.append("Whoever made the change")
    return out

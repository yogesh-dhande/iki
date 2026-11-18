"""Records' states and the transitions between them (ADR 0118).

A type declares its states and transitions; its subtypes have the same. A
record's state is ``iki_state`` in its declaring type's table, and only a
transition changes it: :func:`transition`, a numbered revision of its own.
Each transition is limited to lab roles (ADR 0116), may need a reason, and
may refuse whoever made earlier ones (``not_by``, separation of duties).

In a state that locks, a record's values can't change by any path. The
services refuse with :func:`locked_why`, and the database refuses too:
``iki_entity.locked`` follows the state, and triggers on the tables holding
a record's values refuse changes while it's set (:func:`install_guards`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from psycopg import sql

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import registry
from iki_server.entities.models import Entity, Revision, RevisionKind
from iki_server.entities.values import Invalid
from iki_server.org import lab_roles, permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_spec.model import STATE_COLUMN, EntityType, State, Transition

#: The triggers' names start with this; ``sync_plugins`` drops those no table needs.
TRIGGER_PREFIX = "iki_locked__"
#: The longest reason a transition keeps.
MAX_REASON = 2000


def machine(type_name: str) -> EntityType | None:
    """The type whose states ``type_name``'s records move through: its own, or the
    ancestor's that declares them. None if they have none."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    return next((t for t in [entity_type, *registry.ancestors(entity_type)] if t.states), None)


def state_of(entity: Entity) -> str | None:
    """A record's state, read from its declaring type's table. None for an entry, or a
    type without states."""
    found = machine(entity.type)
    if found is None or entity.registry_id is not None:
        return None
    model = registry.model(found.qualified_name)
    value = model._default_manager.filter(pk=entity.pk).values_list(STATE_COLUMN, flat=True)
    return value.first()


def states_of(entities: Iterable[Entity]) -> dict[Any, str]:
    """Each record's state, for those that have one, in a query per declaring type."""
    by_machine: dict[str, list[Any]] = {}
    for entity in entities:
        found = machine(entity.type) if entity.registry_id is None else None
        if found is not None:
            by_machine.setdefault(found.qualified_name, []).append(entity.pk)
    out: dict[Any, str] = {}
    for name, ids in by_machine.items():
        model = registry.model(name)
        out.update(model._default_manager.filter(pk__in=ids).values_list("pk", STATE_COLUMN))
    return out


def state(type_name: str, name: str | None) -> State | None:
    found = machine(type_name)
    if found is None or name is None:
        return None
    return found.states.get(name)


def locked_why(entity: Entity, *, doing: str = "be changed") -> str | None:
    """Why a record can't change now because its state locks it, or None if it can."""
    if not entity.locked:
        return None
    found = machine(entity.type)
    current = state_of(entity)
    label = found.states[current].label if found and current in found.states else current
    out = [
        f"“{t.label}”"
        for t in (found.transitions.values() if found else ())
        if current in t.from_states and not (found and found.states[t.to].locks)
    ]
    how = f" until {_either(out)} takes it out" if out else ""
    return f"{entity.hrid} is {label}, which locks it: it can’t {doing}{how}."


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


# --- Transitions --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Option:
    """A transition from a record's state, and whether this person may make it."""

    transition: Transition
    why_not: str | None


def options(
    user: User,
    entity: Entity,
    current: str | None = None,
    made: dict[str, Any] | None = None,
) -> list[Option]:
    """The transitions from the record's state, each with why ``user`` may not make it.
    ``made``: who made the latest of each of its transitions, from :func:`made_by`."""
    found = machine(entity.type)
    if found is None or entity.registry_id is not None:
        return []
    current = current if current is not None else state_of(entity)
    return [
        Option(t, why_not(user, entity, t, made))
        for t in found.transitions.values()
        if current in t.from_states
    ]


def made_by(entities: Iterable[Entity]) -> dict[Any, dict[str, Any]]:
    """For each record, who made the latest of each transition on it: one query."""
    ids = [e.pk for e in entities if machine(e.type) is not None]
    out: dict[Any, dict[str, Any]] = {pk: {} for pk in ids}
    if not ids:
        return out
    latest = (
        Revision.objects.filter(entity_id__in=ids, kind=RevisionKind.TRANSITIONED)
        .order_by("entity_id", "transition", "-number")
        .distinct("entity_id", "transition")
        .values_list("entity_id", "transition", "user_id")
    )
    for entity_id, name, by in latest:
        out[entity_id][name] = by
    return out


def why_not(
    user: User, entity: Entity, t: Transition, made: dict[str, Any] | None = None
) -> str | None:
    """Why ``user`` may not make ``t`` on the record now, or None if they may. The record's
    state isn't checked here: :func:`transition` does that, under its lock. ``made``: who
    made the latest of each transition on it, if it's known already."""
    owner = entity.owner_project
    if owner is None:
        return "It's a registry entry: its projects' records have states, not it."
    if entity.purged_at is not None:
        return "Its plugin's data was purged."
    if entity.archived_at is not None:
        return "It's archived: restore it first."
    if registry.detached() and _frozen(entity):
        return "Its plugin is detached: its records can be read but not changed."
    if not permissions.can(user, Action.PROJECT_TRANSITION, owner):
        if permissions.token_limits(user) is not None and permissions.can(
            user, Action.PROJECT_READ, owner
        ):
            return "This token can't change records in the project."
        if owner.is_archived:
            return f"{owner.name} is archived."
        return f"It needs a role in {owner.name}."
    held = permissions.lab_roles(user, owner)
    if not held & set(t.roles):
        roles = _either([lab_roles.label(r) for r in t.roles])
        return f"It needs someone with the {roles} role in {owner.name}."
    if t.not_by:
        # Whoever made the latest of each named transition: a submitter isn't the approver.
        latest = made if made is not None else made_by([entity]).get(entity.pk, {})
        yours = [name for name in t.not_by if latest.get(name) == user.pk]
        if yours:
            found = machine(entity.type)
            labels = [f"“{found.transitions[n].label if found else n}”" for n in yours]
            return (
                f"You made {_and(labels)} on it, so someone else makes “{t.label}”: "
                "the plugin keeps them apart."
            )
    return None


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _frozen(entity: Entity) -> bool:
    from iki_server.entities import services
    from iki_server.entities.values import shape

    return services.frozen(shape(entity.type)) is not None


def transition(
    user: User,
    entity: Entity,
    name: str,
    *,
    expected_revision: int,
    reason: str = "",
) -> Revision:
    """Moves the record along transition ``name``: checked, then a revision of its own,
    told to webhooks and triggers. Raises :class:`PermissionDenied` when ``user`` may not
    make it, :class:`Refused` when the record can't take it now, and :class:`Invalid` for a
    missing reason."""
    from iki_server.entities import services

    with transaction.atomic():
        locked = (
            Entity.objects.select_for_update(of=("self",))
            .select_related("owner_project")
            .get(pk=entity.pk)
        )
        found = machine(locked.type)
        if found is None or locked.registry_id is not None:
            raise Refused(f"{locked.hrid} has no states.")
        t = found.transitions.get(name)
        if t is None:
            raise Refused(f"{found.label} records have no transition `{name}`.")
        if locked.revision != expected_revision:
            raise services.Stale(locked.revision)
        current = state_of(locked)
        if current not in t.from_states:
            label = found.states[current].label if current in found.states else current
            raise Refused(
                f"{locked.hrid} is {label}: “{t.label}” leads from "
                + _either([found.states[s].label for s in t.from_states])
                + "."
            )
        why = why_not(user, locked, t)
        if why is not None:
            owner = locked.owner_project
            if (
                owner is not None
                and permissions.can(user, Action.PROJECT_TRANSITION, owner)
                and set(t.roles) & permissions.lab_roles(user, owner)
            ):
                raise Refused(why)  # separation of duties, or the record's own condition
            raise PermissionDenied(why)
        reason = reason.strip()
        if t.reason == "required" and not reason:
            raise Invalid({"reason": [f"“{t.label}” needs a reason: say why."]})
        target = found.states[t.to]
        model = registry.model(found.qualified_name)
        # The state changes while the record is unlocked, under its row's lock: the
        # database's guard refuses every change to a locked record, this one too.
        if locked.locked:
            Entity.objects.filter(pk=locked.pk).update(locked=False)
        model._default_manager.filter(pk=locked.pk).update(**{STATE_COLUMN: t.to})
        if target.locks:
            Entity.objects.filter(pk=locked.pk).update(locked=True)
        revision = services.transitioned(locked, t, current or "", user, reason)
        # Beside its history: the audit log is where QA reviews who released what.
        log.record(
            "entity.transitioned",
            actor=user,
            target=locked,
            project=locked.owner_project,
            data={
                "transition": t.name,
                "from": current or "",
                "to": t.to,
                "revision": revision.number,
                "reason": reason,
            },
        )
        return revision


# --- The database's guard -----------------------------------------------------------------------


def guarded_tables() -> list[tuple[str, str]]:
    """Every table holding values of records that have states, with its column naming the
    record: (table, column)."""
    from iki_server.entities.values import shape

    out: dict[str, str] = {}
    for entity_type in registry.types().values():
        if entity_type.abstract or machine(entity_type.qualified_name) is None:
            continue
        form = shape(entity_type.qualified_name)
        for part in form.parts:
            if part.entry:
                continue
            out[part.model._meta.db_table] = "entity_id"
            for f in part.fields.values():
                if f.removed:
                    continue
                if _is_list(f):
                    out[part.list_model(f.name)._meta.db_table] = "entity_id"
    return sorted(out.items())


def _is_list(f: Any) -> bool:  # noqa: ANN401 - a spec field
    from iki_server.entities.values import is_list_table

    return bool(is_list_table(f))


def install_guards(cursor: Any, wanted: list[tuple[str, str]]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """Puts the guard on each table, and takes it off tables that no longer need it."""
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname LIKE %s",
        [TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    needed = {table: _trigger(table) for table, _ in wanted}
    for table, name in cursor.fetchall():
        if needed.get(table) != name:
            cursor.execute(
                sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
            )
    for table, column in wanted:
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE TRIGGER {name} BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION iki_locked_guard({column})"
            ).format(
                name=sql.Identifier(needed[table]),
                table=sql.Identifier(table),
                column=sql.Literal(column),
            )
        )
    return sorted(needed)


def _trigger(table: str) -> str:
    return (TRIGGER_PREFIX + table)[:63]

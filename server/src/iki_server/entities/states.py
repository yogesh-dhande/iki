"""Records' state fields (ADR 0125).

A type's fields can include state fields (``type: state``), each with its own
states: a lot's production status beside its QA disposition, an instrument's
service beside its calibration. Each state says which lab roles (ADR 0116) may
set the field to it, from any other, and whether it locks the record. Setting
one is :func:`set_state`, a numbered revision of its own with who, how and why.
That's all core decides: which states may follow which, and who may not set
one after setting another, are a plugin's policy, for guards to come.

A state can need electronic signatures (ADR 0126): then it's set by its last
signature, never by :func:`set_state`, which refuses with :class:`NeedsSignatures`.

While any of a record's state fields is in a state that locks, the record is
locked: its other fields can't change by any path, and it can't be archived,
restored or moved to another registry entry. Its state fields stay settable by
their roles, which is how it's unlocked. The services refuse with
:func:`locked_why`, and the database refuses too: ``iki_entity.locked_by``
names the state fields locking it, and triggers on the tables holding a
record's values refuse changes while it names any (:func:`install_guards`). A state
can declare an ``on_edit`` return (ADR 0179). A reasoned correction takes that
transition and writes the changed values in the same transaction.

A state's ``guards`` (ADR 0128, :mod:`iki_server.entities.guards`) say when it may be
set: which state it follows, who mustn't set it after setting another (four eyes), what
else must be true of the record. And a record held for review by a rule (ADR 0127) can't
be set to a state that locks it until its holds are reviewed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from psycopg import sql

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import guards, registry
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Invalid
from iki_server.org import lab_roles, permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.signatures import entries
from iki_spec.model import Field, State, stored_in_list_table

__all__ = [
    "CorrectionReason",
    "GuardRefused",
    "NeedsSignatures",
    "Option",
    "conditions_of",
    "install_guards",
    "locked_why",
    "locking",
    "on_edit",
    "options",
    "options_of",
    "refusal",
    "return_for_edit",
    "set_state",
    "state_fields",
    "values_of",
    "why_not",
    "write",
]

#: The triggers' names start with this; ``sync_plugins`` drops those no table needs.
TRIGGER_PREFIX = "iki_locked__"
#: The longest reason a state change keeps.
MAX_REASON = 2000


def state_fields(type_name: str) -> dict[str, Field]:
    """A type's state fields that records have values in, its ancestors' first, in the
    order they're declared. Empty for a type without any."""
    from iki_server.entities.values import shape

    if type_name == "iki/core.Entity" or type_name not in registry.types():
        return {}
    form = shape(type_name)
    return {
        name: f
        for part in form.parts
        if not part.entry
        for name, f in part.fields.items()
        if f.type == "state" and not f.removed
    }


def locking(fields: Mapping[str, Field], values: Mapping[str, Any]) -> list[str]:
    """The state fields whose values lock a record, in their order."""
    return [name for name, f in fields.items() if values.get(name) in f.locking_states]


def transitions(
    entity: Entity,
    field: Field,
    current: str,
    history: Mapping[tuple[UUID, str], entries.Entry] | None = None,
) -> Mapping[str, State]:
    history = entries.for_records([entity]) if history is None else history
    entry = history.get((entity.pk, field.name))
    return entry.transitions(field, current) if entry else field.states


def on_edit(fields: Mapping[str, Field], values: Mapping[str, Any]) -> dict[str, str]:
    """The destination of each current state that returns the record on a value edit."""
    return {
        name: st.on_edit
        for name, f in fields.items()
        if not f.removed
        and (st := f.states.get(values.get(name, ""))) is not None
        and not st.removed
        and st.on_edit is not None
        and not st.signatures
    }


def values_of(entity: Entity) -> dict[str, str]:
    """A record's state fields' values, read from its type's tables. Empty for a registry
    entry, and for a type without state fields."""
    from iki_server.entities.values import shape

    if entity.registry_id is not None or not state_fields(entity.type):
        return {}
    out: dict[str, str] = {}
    for part in shape(entity.type).parts:
        names = [n for n, f in part.fields.items() if f.type == "state" and not f.removed]
        if part.entry or not names:
            continue
        row = part.model._default_manager.filter(pk=entity.pk).values(*names).first()
        out.update(row or {})
    return out


def _label(f: Field, state: str | None) -> str:
    found = f.states.get(state or "")
    return found.label if found is not None else str(state)


def locked_why(entity: Entity, *, doing: str = "be changed") -> str | None:
    """Why a record can't change now because a state locks it, or None if it can: which
    state fields lock it, and the states that would unlock it, with who sets them."""
    if entity.released_review_revision is not None:
        return _final_release(entity)
    if not entity.locked:
        return None
    fields = state_fields(entity.type)
    values = values_of(entity)
    # In its type's order, whichever order they came to lock it in.
    by = [name for name in fields if name in entity.locked_by] or list(entity.locked_by)
    held = [
        f"its {fields[n].label} ({_label(fields[n], values.get(n))})" if n in fields else n
        for n in by
    ]
    until = []
    for name in by:
        f = fields.get(name)
        if f is None:
            continue
        ways = [
            f"{st.label} ({_either([lab_roles.label(r) for r in st.set_by])})"
            for st in f.states.values()
            if not st.removed and not st.locks and st.set_by
        ]
        if ways:
            until.append(f"its {f.label} is set to {_either(ways)}")
    how = f" until {_and(until)}" if until else ""
    return f"{entity.hrid} is locked by {_and(held)}: it can’t {doing}{how}."


def _final_release(entity: Entity) -> str:
    return (
        f"{entity.hrid} was released to the requesting project after signing "
        f"for its reviewed revision {entity.released_review_revision}. "
        "Its values and states are final."
    )


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


# --- Setting a state ----------------------------------------------------------------------------


class GuardRefused(Refused):
    """A state's guard refuses setting it now (ADR 0128): its message says why."""

    def __init__(self, message: str, guard: str) -> None:
        super().__init__(message)
        self.guard = guard


class CorrectionReason(Invalid):
    """A correction needs a reason in the edit request."""


@dataclass(frozen=True)
class Option:
    """A state a record's state field could be set to, and why this person may not."""

    field: Field
    state: State
    why_not: str | None
    #: The guard refusing it, if one does (ADR 0128).
    guard: str | None = None

    @property
    def signed(self) -> bool:
        """It's set by signing for it (ADR 0126), not directly."""
        return bool(self.state.signatures)


def options(
    user: User,
    entity: Entity,
    values: Mapping[str, Any],
    *,
    history: Mapping[tuple[UUID, str], entries.Entry] | None = None,
) -> dict[str, list[Option]]:
    """For each of a record's state fields, the states it could be set to (every one but
    the one it's in), each with why ``user`` may not set it. ``values``: the record's,
    as the database stores them: its guards read them."""
    return options_of(user, [entity], {entity.pk: values}, history=history).get(entity.pk, {})


def options_of(
    user: User,
    entities: Sequence[Entity],
    values: Mapping[UUID, Mapping[str, Any]],
    *,
    history: Mapping[tuple[UUID, str], entries.Entry] | None = None,
) -> dict[UUID, dict[str, list[Option]]]:
    """:func:`options` for each of a page of records, by ID: their holds and what their
    guards read are read once for all of them. ``values``: each record's, by ID. A
    registry entry has none."""
    records = [e for e in entities if e.registry_id is None]
    history = entries.for_records(records) if history is None else history
    asks: list[tuple[Entity, Field, list[State], dict[str, tuple[str, bool] | None]]] = []
    for entity in records:
        for name, f in state_fields(entity.type).items():
            current = values[entity.pk].get(name)
            candidates = [
                st
                for st in transitions(entity, f, current or "", history).values()
                if not st.removed and st.name != current
            ]
            whys = {st.name: _why_not_who(user, entity, name, st) for st in candidates}
            asks.append((entity, f, candidates, whys))
    conditions = conditions_of(
        user,
        [
            guards.Ask(
                entity,
                f.name,
                [st for st in candidates if whys[st.name] is None],
                values[entity.pk],
            )
            for entity, f, candidates, whys in asks
        ],
    )
    out: dict[UUID, dict[str, list[Option]]] = {e.pk: {} for e in records}
    for (entity, f, candidates, whys), found in zip(asks, conditions, strict=True):
        offered = []
        for st in candidates:
            refusal = whys[st.name]
            if refusal is not None:
                offered.append(Option(f, st, refusal[0]))
            else:
                why, guard = found.get(st.name, (None, None))
                offered.append(Option(f, st, why, guard))
        out[entity.pk][f.name] = offered
    return out


def why_not(
    user: User, entity: Entity, f: Field, state: State, values: Mapping[str, Any] | None = None
) -> str | None:
    """Why ``user`` may not set ``f`` to ``state`` on the record now, or None if they may.
    Its current state isn't checked here: :func:`set_state` does that, under its lock."""
    values = values if values is not None else values_of(entity)
    state = transitions(entity, f, values.get(f.name) or "")[state.name]
    found = _why_not_who(user, entity, f.name, state)
    if found is not None:
        return found[0]
    return condition(user, entity, f.name, state, values)


def condition(
    user: User,
    entity: Entity,
    field: str,
    state: State,
    values: Mapping[str, Any] | None = None,
    signatures: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    """Why the record can't be set to ``state`` now, as it is, with ``user`` acting, if it
    can't: a hold, for a state that locks it (ADR 0127), or a guard (ADR 0128). A signed
    state is checked so at each of its signatures, with its signer acting and the
    ``signatures`` given for it so far, as its guards read them."""
    if values is None:
        from iki_server.entities import services

        values = services.current_values(services.specific(entity))
    found = _conditions(user, entity, field, [state], values, signatures)
    return found.get(state.name, (None, None))[0]


def _conditions(
    user: User,
    entity: Entity,
    field: str,
    candidates: list[State],
    values: Mapping[str, Any],
    signatures: Sequence[Mapping[str, Any]] = (),
) -> dict[str, tuple[str, str | None]]:
    """Why the record can't be set to each of ``candidates`` now, as the record is, by
    state: its holds (for a state that locks it, ADR 0127) or a guard (ADR 0128), with
    the guard's name."""
    return conditions_of(user, [guards.Ask(entity, field, candidates, values, signatures)])[0]


def conditions_of(
    user: User, asks: Sequence[guards.Ask]
) -> list[dict[str, tuple[str, str | None]]]:
    """Why each of ``asks``' records can't be set to each of its states now, by state, with
    the guard refusing it, if one does (:func:`condition`): a page of records' at once.
    Their holds are read once, for the holds and the guards alike."""
    from iki_server.entities import checking, holds, requests

    out: list[dict[str, tuple[str, str | None]]] = [{} for _ in asks]
    held = holds.open_of(
        {a.entity.pk for a in asks if any(st.locks or st.guards for st in a.states)}
    )
    # The rules a signed state checks again before signing, for each type's records at once.
    signed: dict[str, list[int]] = {}
    for i, ask in enumerate(asks):
        if any(st.signatures for st in ask.states):
            signed.setdefault(ask.entity.type, []).append(i)
    failures: dict[int, list[checking.Broken]] = {}
    for type_name, at in signed.items():
        savings = [
            checking.Saving(asks[i].values.get, id=asks[i].entity.pk, hrid=asks[i].entity.hrid)
            for i in at
        ]
        failures.update(
            zip(at, checking.broken(type_name, savings, before_signing=True), strict=True)
        )
    rest: list[guards.Ask] = []
    for i, (found, ask) in enumerate(zip(out, asks, strict=True)):
        entity, field, candidates = ask.entity, ask.field, ask.states
        if not candidates:
            rest.append(ask)
            continue
        completion = requests.spec_of(entity.type)
        if completion and completion.completed_field == field:
            for st in candidates:
                if st.name == completion.completed_state and (
                    why := requests.delivery_refusal(entity)
                ):
                    found[st.name] = (why, None)
        if any(st.locks for st in candidates):
            blocking = holds.blocks_locking(entity, held.get(entity.pk, []))
            if blocking:
                found.update({st.name: (blocking, None) for st in candidates if st.locks})
        if failed := failures.get(i):
            found.update(
                {
                    st.name: (failed[0].message, failed[0].rule.key)
                    for st in candidates
                    if st.signatures
                }
            )
        rest.append(replace(ask, states=[st for st in candidates if st.name not in found]))
    for found, refused in zip(out, guards.refusals_of(user, rest, held), strict=True):
        for name, refusal in refused.items():
            found[name] = (refusal.message, refusal.guard.name)
    return out


def _why_not_who(user: User, entity: Entity, field: str, state: State) -> tuple[str, bool] | None:
    """Why not, before the record's conditions: it's signed for, or ``user`` may not."""
    if state.signatures:
        return "It’s set by signing for it: see its signatures.", False
    return refusal(user, entity, state.set_by, state.label, field=field)


def refusal(
    user: User, entity: Entity, roles: tuple[str, ...], label: str, *, field: str
) -> tuple[str, bool] | None:
    """Why ``user`` may not set the record's state field ``field`` to the state ``label``,
    as one of ``roles`` may, and whether it's for who they are (True) rather than the
    record's own condition (False); None if they may. A signature's signer is checked
    with it too, holding the signature's ``roles`` (ADR 0126). A request's service field
    is set by the service's people, with the roles they hold there (ADR 0132)."""
    from iki_server.entities import requests

    if entity.owner_project is None:
        return "It's a registry entry: its projects' records have states, not it.", False
    owner = requests.field_project(entity, field)
    if owner is None:
        return (
            "It’s the service’s: the project the request is sent to sets it, once it’s submitted.",
            False,
        )
    if entity.released_review_revision is not None:
        return _final_release(entity), False
    if entity.purged_at is not None:
        return "Its plugin's data was purged.", False
    if entity.archived_at is not None:
        return "It's archived: restore it first.", False
    if registry.detached() and _frozen(entity):
        return "Its plugin is detached: its records can be read but not changed.", False
    if not permissions.can(user, Action.PROJECT_SET_STATE, owner):
        if permissions.token_limits(user) is not None and permissions.can(
            user, Action.PROJECT_READ, owner
        ):
            return "This token can't change records in the project.", True
        if owner.is_archived:
            return f"{owner.name} is archived.", False
        return f"It needs a role in {owner.name}.", True
    if not roles:
        return f"No one sets it to {label}: new records start in it.", False
    if not permissions.lab_roles(user, owner) & set(roles):
        named = _either([lab_roles.label(r) for r in roles])
        return f"It needs someone with the {named} role in {owner.name}.", True
    return None


def _frozen(entity: Entity) -> bool:
    from iki_server.entities import services
    from iki_server.entities.values import shape

    return services.frozen(shape(entity.type)) is not None


def set_state(
    user: User,
    entity: Entity,
    field: str,
    state: str,
    *,
    expected_revision: int,
    reason: str = "",
) -> Revision:
    """Sets the record's state field ``field`` to ``state``: checked, then a revision of
    its own, told to webhooks and triggers, and in the audit log. Raises
    :class:`PermissionDenied` when ``user`` may not, :class:`Refused` when the record
    can't take it now (:class:`NeedsSignatures` for a state its signatures set), and
    :class:`Invalid` for an unknown state or a missing reason."""
    from iki_server.entities import services

    with transaction.atomic():
        locked = (
            Entity.objects.select_for_update(of=("self",))
            .select_related("owner_project")
            .get(pk=entity.pk)
        )
        if locked.registry_id is not None:
            raise Refused(f"{locked.hrid} is a registry entry: its projects' records have states.")
        fields = state_fields(locked.type)
        f = fields.get(field)
        if f is None:
            type_label = registry.types()[locked.type].label
            raise Refused(f"{type_label} records have no state field `{field}`.")
        target = f.states.get(state)
        if target is None or target.removed:
            live = ", ".join(f"`{n}`" for n in f.live_states)
            raise Invalid({"state": [f"{f.label} has no state `{state}`: it's one of {live}."]})
        if locked.revision != expected_revision:
            raise services.Stale(locked.revision)
        values = values_of(locked)
        current = values.get(field)
        if current == state:
            raise Refused(f"{locked.hrid}'s {f.label} is {target.label} already.")
        if locked.released_review_revision is not None:
            raise Refused(_final_release(locked))
        target = transitions(locked, f, current or "")[state]
        if target.signatures:
            count = len(target.signatures)
            raise NeedsSignatures(
                f"Setting its {f.label} to {target.label} is signed: it’s set once "
                + ("its signature is given" if count == 1 else f"its {count} signatures are given")
                + ", each by a person who signs in again in Iki’s web app (21 CFR Part 11)."
            )
        found = refusal(user, locked, target.set_by, target.label, field=field)
        if found is not None:
            why, forbidden = found
            raise PermissionDenied(why) if forbidden else Refused(why)
        record = services.current_values(services.specific(locked))
        condition = _conditions(user, locked, field, [target], record).get(state)
        if condition is not None:
            why, guard = condition
            raise GuardRefused(why, guard) if guard is not None else Refused(why)
        reason = reason.strip()
        if target.reason == "required" and not reason:
            raise Invalid(
                {"reason": [f"Setting its {f.label} to {target.label} needs a reason: say why."]}
            )
        if len(reason) > MAX_REASON:
            raise Invalid({"reason": [f"Say why in {MAX_REASON} characters or fewer."]})
        return write(locked, f, state, user, reason)


def return_for_edit(user: User, locked: Entity, targets: Mapping[str, str], reason: str) -> None:
    """Take each return under the edit's row lock, checking its actual predecessor.
    A refused transition or failed value edit rolls them all back."""
    reason = reason.strip()
    if not reason:
        raise CorrectionReason({"reason": ["Say why these reviewed values need a correction."]})
    if len(reason) > MAX_REASON:
        raise CorrectionReason({"reason": [f"Say why in {MAX_REASON} characters or fewer."]})
    number = locked.revision
    for name, target in targets.items():
        change = set_state(user, locked, name, target, expected_revision=number, reason=reason)
        number = change.number


class NeedsSignatures(Refused):
    """The state is set only by signing for it (ADR 0126)."""


def write(
    locked: Entity,
    f: Field,
    state: str,
    user: User,
    reason: str,
    *,
    signatures: tuple[Any, ...] = (),
) -> Revision:
    """Sets ``f`` to ``state`` on a record whose row this transaction has locked, once it's
    been checked: by :func:`set_state`, or for a signed state by its last signature, whose
    revision carries them all (``signatures``, in order)."""
    from iki_server.entities import services

    fields = state_fields(locked.type)
    values = values_of(locked)
    current = values.get(f.name) or ""
    from iki_server.entities import requests

    # The field changes while the record is unlocked, under its row's lock: the
    # database's guard refuses every change to a locked record, this one too.
    if locked.locked_by:
        Entity.objects.filter(pk=locked.pk).update(locked_by=[])
    # A request's state field is its side's to write (ADR 0132), checked by who set it.
    with requests.writing(frozenset({requests.side_of(locked.type, f.name)})):
        revision = services.state_changed(
            user, locked, f, current, state, reason, signatures=signatures
        )
    after = locking(fields, {**values, f.name: state})
    if after:
        Entity.objects.filter(pk=locked.pk).update(locked_by=after)
    # A result released goes back to the project that asked for it (ADR 0132).
    requests.released(user, locked, f.name, state, revision)
    # Beside its history: the audit log is where QA reviews who released what.
    log.record(
        "entity.state_changed",
        actor=user,
        target=locked,
        project=locked.owner_project,
        data={
            "field": f.name,
            "from": current,
            "to": state,
            "revision": revision.number,
            "reason": reason,
            **({"signatures": [str(i) for i in signatures]} if signatures else {}),
        },
    )
    return revision


# --- The database's guard -----------------------------------------------------------------------


def guarded_tables() -> list[tuple[str, str]]:
    """Every table holding values of records with state fields, with its column naming the
    record: (table, column)."""
    from iki_server.entities.values import shape

    out: dict[str, str] = {}
    for entity_type in registry.types().values():
        if entity_type.abstract or not state_fields(entity_type.qualified_name):
            continue
        for part in shape(entity_type.qualified_name).parts:
            if part.entry:
                continue
            out[part.model._meta.db_table] = "entity_id"
            for f in part.fields.values():
                if not f.removed and stored_in_list_table(f):
                    out[part.list_model(f.name)._meta.db_table] = "entity_id"
    return sorted(out.items())


def install_guards(cursor: Any, wanted: Iterable[tuple[str, str]]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """Puts the guard on each table, and takes it off tables that no longer need it."""
    wanted = list(wanted)
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

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
record's values refuse changes while it names any (:func:`install_guards`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from psycopg import sql

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import registry
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Invalid
from iki_server.org import lab_roles, permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_spec.model import Field, State

__all__ = [
    "NeedsSignatures",
    "Option",
    "install_guards",
    "locked_why",
    "locking",
    "options",
    "refusal",
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

    if type_name not in registry.types():
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


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


# --- Setting a state ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Option:
    """A state a record's state field could be set to, and why this person may not."""

    field: Field
    state: State
    why_not: str | None

    @property
    def signed(self) -> bool:
        """It's set by signing for it (ADR 0126), not directly."""
        return bool(self.state.signatures)


def options(user: User, entity: Entity, values: Mapping[str, Any]) -> dict[str, list[Option]]:
    """For each of a record's state fields, the states it could be set to (every one but
    the one it's in), each with why ``user`` may not set it. ``values``: the record's."""
    if entity.registry_id is not None:
        return {}
    out: dict[str, list[Option]] = {}
    for name, f in state_fields(entity.type).items():
        current = values.get(name)
        out[name] = [
            Option(f, st, why_not(user, entity, f, st))
            for st in f.states.values()
            if not st.removed and st.name != current
        ]
    return out


def why_not(user: User, entity: Entity, f: Field, state: State) -> str | None:
    """Why ``user`` may not set ``f`` to ``state`` on the record now, or None if they may.
    Its current state isn't checked here: :func:`set_state` does that, under its lock."""
    if state.signatures:
        return "It’s set by signing for it: see its signatures."
    found = refusal(user, entity, state.set_by, state.label)
    return found[0] if found is not None else None


def refusal(
    user: User, entity: Entity, roles: tuple[str, ...], label: str
) -> tuple[str, bool] | None:
    """Why ``user`` may not set one of the record's state fields to the state ``label``,
    as one of ``roles`` may, and whether it's for who they are (True) rather than the
    record's own condition (False); None if they may. A signature's signer is checked
    with it too, holding the signature's ``roles`` (ADR 0126)."""
    owner = entity.owner_project
    if owner is None:
        return "It's a registry entry: its projects' records have states, not it.", False
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
        if target.signatures:
            count = len(target.signatures)
            raise NeedsSignatures(
                f"Setting its {f.label} to {target.label} is signed: it’s set once "
                + ("its signature is given" if count == 1 else f"its {count} signatures are given")
                + ", each by a person who signs in again in Iki’s web app (21 CFR Part 11)."
            )
        found = refusal(user, locked, target.set_by, target.label)
        if found is not None:
            why, forbidden = found
            raise PermissionDenied(why) if forbidden else Refused(why)
        reason = reason.strip()
        if target.reason == "required" and not reason:
            raise Invalid(
                {"reason": [f"Setting its {f.label} to {target.label} needs a reason: say why."]}
            )
        if len(reason) > MAX_REASON:
            raise Invalid({"reason": [f"Say why in {MAX_REASON} characters or fewer."]})
        return write(locked, f, state, user, reason)


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
    # The field changes while the record is unlocked, under its row's lock: the
    # database's guard refuses every change to a locked record, this one too.
    if locked.locked_by:
        Entity.objects.filter(pk=locked.pk).update(locked_by=[])
    revision = services.state_changed(
        user, locked, f, current, state, reason, signatures=signatures
    )
    after = locking(fields, {**values, f.name: state})
    if after:
        Entity.objects.filter(pk=locked.pk).update(locked_by=after)
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
    from iki_server.entities.values import is_list_table, shape

    out: dict[str, str] = {}
    for entity_type in registry.types().values():
        if entity_type.abstract or not state_fields(entity_type.qualified_name):
            continue
        for part in shape(entity_type.qualified_name).parts:
            if part.entry:
                continue
            out[part.model._meta.db_table] = "entity_id"
            for f in part.fields.values():
                if not f.removed and is_list_table(f):
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

"""What a signature decides: setting a record's state field to a state (ADR 0126).

Signatures are the primitive; what they sign for is a :class:`Change`, setting state
``to`` of state field ``field`` (ADR 0125), from whatever state the record's field is
in. Each state of a type's state field says which signatures setting it needs. This
module is the one place that reads that from the type, so the rest of signing doesn't
depend on how states are declared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from iki_server.entities import registry, states
from iki_server.org.services import Refused
from iki_server.signatures.entries import Reversal
from iki_spec.model import Field, State
from iki_spec.model import Signature as Wanted

if TYPE_CHECKING:
    from iki_server.accounts.models import User
    from iki_server.entities.models import Entity, Revision


@dataclass(frozen=True)
class Change:
    """Setting the record's state field ``field`` to ``to``, from ``current``, and the
    signatures that needs."""

    field: str
    field_label: str
    current: str
    current_label: str
    to: str
    to_label: str
    #: The lab roles that may set it: any one.
    roles: tuple[str, ...]
    signatures: tuple[Wanted, ...]
    #: Whether it asks why: "required", "optional", or None.
    reason: str | None
    #: The state field it sets (internal to this module).
    how: Field | None = None

    @property
    def reversal(self) -> bool:
        return any(isinstance(slot, Reversal) for slot in self.signatures)

    @property
    def label(self) -> str:
        """How people read it: “Approved”, the state it sets."""
        return self.to_label


def roles_for(change: Change, wanted: Wanted) -> tuple[str, ...]:
    """The lab roles whoever gives a signature holds: its own, or the state's."""
    if isinstance(wanted, Reversal):
        return wanted.entry_roles
    return (wanted.role,) if wanted.role else change.roles


def wanted(state: State) -> tuple[Wanted, ...]:
    """The signatures setting a state needs: none for one set directly."""
    return state.signatures or ()


def signed(options: Mapping[str, list[states.Option]], values: Mapping[str, Any]) -> list[Change]:
    """The signed transitions among the record's available state options."""
    return [
        _change(option.field, values.get(name) or "", option.state)
        for name, choices in options.items()
        for option in choices
        if option.signed
    ]


def find(entity: Entity, field: str, to: str) -> Change:
    """Setting ``field`` to ``to`` on the record now, if it needs signatures; else
    :class:`Refused`, saying why."""
    if entity.registry_id is not None:
        raise Refused(f"{entity.hrid} is a registry entry: its projects' records have states.")
    f = states.state_fields(entity.type).get(field)
    if f is None:
        type_label = registry.types()[entity.type].label if entity.type in registry.types() else ""
        raise Refused(f"{type_label or entity.type} records have no state field `{field}`.")
    target = f.states.get(to)
    if target is None or target.removed:
        raise Refused(f"{f.label} has no state `{to}`.")
    current = states.values_of(entity).get(field) or ""
    target = states.transitions(entity, f, current)[to]
    if not wanted(target):
        raise Refused(
            f"Setting {entity.hrid}’s {f.label} to {target.label} isn’t signed: set it as any "
            "state is."
        )
    if current == to:
        raise Refused(f"{entity.hrid}’s {f.label} is {target.label} already.")
    return _change(f, current, target)


def _change(f: Field, current: str, target: State) -> Change:
    now = f.states.get(current)
    return Change(
        field=f.name,
        field_label=f.label,
        current=current,
        current_label=now.label if now is not None else current,
        to=target.name,
        to_label=target.label,
        roles=target.set_by,
        signatures=wanted(target),
        reason=target.reason,
        how=f,
    )


def make(
    locked: Entity, change: Change, user: User, reason: str, signatures: tuple[Any, ...]
) -> Revision:
    """Sets the state, once its last signature is given, on a record whose row this
    transaction has locked: the revision carries the signatures. Checked as any save is
    (ADR 0127), though its conditions refused it already for a rule it would break."""
    from iki_server.entities import pipeline

    assert change.how is not None  # noqa: S101 - find made it
    made = states.plan(locked, change.how, change.to, user, reason, signatures=signatures)
    return states.write(made, pipeline.evaluate(made))


def carried(revision: Revision) -> tuple[str, str, str]:
    """The field, and the states from and to, of a revision that set a state."""
    return revision.state_field, revision.state_from, revision.state_to

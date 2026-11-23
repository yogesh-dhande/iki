"""Type-declared conditions on taking material from a record (ADR 0139)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any

from iki_server.accounts.models import User
from iki_server.amounts.models import DrawPurpose
from iki_server.entities import checking, guards, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import shape
from iki_server.org import lab_roles, permissions
from iki_server.org.models import Project
from iki_spec import rules
from iki_spec.model import Guard


@cache
def draw_guards(type_name: str) -> tuple[Guard, ...]:
    """The type's draw guards, including every ancestor's."""
    entity_type = registry.types()[type_name]
    chain = [*reversed(registry.ancestors(entity_type)), entity_type]
    return tuple(g for t in chain for g in t.draw_guards.values())


@cache
def purpose_roles(type_name: str) -> dict[str, tuple[str, ...]]:
    """For each declared draw purpose, the roles on the nearest declaring type."""
    entity_type = registry.types()[type_name]
    chain = [*reversed(registry.ancestors(entity_type)), entity_type]
    return {purpose: by for t in chain for purpose, by in t.draw_purposes.items()}


def purpose_refusal(
    user: User, type_name: str, project: Project | None, purpose: DrawPurpose
) -> str | None:
    """Whether an editor may draw for this purpose in the source record's owning project."""
    roles = purpose_roles(type_name).get(purpose)
    if roles is None:
        return (
            None
            if purpose == DrawPurpose.ROUTINE
            else f"{type_name} does not allow {purpose.label}."
        )
    limits = permissions.token_limits(user)
    if limits is not None and (
        project is None or not limits.allows(permissions.Action.PROJECT_EDIT, project)
    ):
        return f"This token cannot use {purpose.label} in the record's owning project."
    principals = (user, limits.alongside) if limits is not None and limits.alongside else (user,)
    if project is not None and all(
        not permissions.lab_roles(principal, project).isdisjoint(roles) for principal in principals
    ):
        return None
    named = " or ".join(lab_roles.label(role) for role in roles)
    return f"{purpose.label} requires the {named} role in the record's owning project."


@cache
def _variables(type_name: str) -> dict[str, Any]:
    return rules.draw_variables(registry.types()[type_name].label, shape(type_name).fields())


def follows(type_name: str) -> tuple[str, ...]:
    """The references whose records a draw from ``type_name``'s also meets (ADR 0180)."""
    from iki_server.amounts.services import spec

    found = spec(type_name)
    return found.follows if found is not None else ()


def followed(entity: Entity, values: Mapping[str, Any] | None = None) -> list[tuple[str, Entity]]:
    """The records ``entity``'s material came from, as its type's ``follows`` names them,
    each with its field's label: its lot, its sample."""
    names = follows(entity.type)
    if not names:
        return []
    if values is None:
        from iki_server.entities.services import current_values_of

        values = current_values_of([entity])[entity.pk]
    fields = shape(entity.type).fields()
    ids = {n: str(values[n]) for n in names if values.get(n)}
    found = {str(e.pk): e for e in Entity.objects.filter(pk__in=list(ids.values()))}
    return [
        (fields[n].label.lower(), found[pk])
        for n, pk in ids.items()
        if pk in found and found[pk].pk != entity.pk
    ]


def source_of(entity: Entity, values: Mapping[str, Any] | None = None) -> Entity | None:
    """The draw's source: the record the first of its type's ``follows`` names."""
    found = followed(entity, values)
    return found[0][1] if found else None


def purpose_refusals(
    user: User, entity: Entity, purpose: DrawPurpose, values: Mapping[str, Any] | None = None
) -> str | None:
    """Whether ``user`` may draw from ``entity`` for ``purpose``: its type's roles in its
    project, and each followed record's in that record's, where its type names any. A
    purpose other than routine use is refused unless one of them declares it."""
    sources = followed(entity, values)
    own = purpose_roles(entity.type).get(purpose)
    theirs = [(n, s) for n, s in sources if purpose_roles(s.type).get(purpose) is not None]
    if own is None and not theirs:
        return purpose_refusal(user, entity.type, entity.owner_project, purpose)
    if own is not None:
        refused = purpose_refusal(user, entity.type, entity.owner_project, purpose)
        if refused is not None:
            return refused
    for name, source in theirs:
        refused = purpose_refusal(user, source.type, source.owner_project, purpose)
        if refused is not None:
            return f"Its {name}, {source.hrid}: {refused}"
    return None


def refusals(
    entity: Entity,
    purposes: Sequence[str],
    values: Mapping[str, Any] | None = None,
    *,
    operation: str = "draw",
    depth: int = 0,
) -> dict[str, str]:
    """The reason each purpose is refused, using the same checks for display and withdrawal:
    its own draw guards, then those of what its material came from (ADR 0180), which also
    refuse it while a state holds their amount."""
    conditions = draw_guards(entity.type)
    names = follows(entity.type) if depth < 3 else ()
    if not conditions and not names:
        return {}
    if values is None:
        from iki_server.entities.services import current_values_of

        values = current_values_of([entity])[entity.pk]
    own = _own_refusals(entity, purposes, values, operation) if conditions else {}
    if not names:
        return own
    from iki_server.amounts.services import held
    from iki_server.entities import states

    for name, source in followed(entity, values):
        from iki_server.entities.services import current_values_of

        theirs = current_values_of([source])[source.pk]
        holding = held(source, theirs)
        if holding is not None:
            state = states.state_fields(source.type)[holding]
            current = state.states.get(theirs.get(holding) or "")
            said = current.label if current is not None else state.label
            for p in purposes:
                own.setdefault(
                    p,
                    f"Its {name}, {source.hrid}, is {said}: none of it can be taken until that "
                    "changes.",
                )
            continue
        for p, message in refusals(
            source, purposes, theirs, operation=operation, depth=depth + 1
        ).items():
            own.setdefault(p, f"Its {name}, {source.hrid}: {message}")
    return own


def _own_refusals(
    entity: Entity, purposes: Sequence[str], values: Mapping[str, Any], operation: str
) -> dict[str, str]:
    conditions = draw_guards(entity.type)
    fields = shape(entity.type).fields()
    reads = {n for g in conditions for n in g.reads}
    record = {
        "id": str(entity.pk),
        "hrid": entity.hrid,
        **{n: checking.cel_value(fields[n], values.get(n)) for n in reads},
    }
    base = {"record": record, "operation": operation, **checking.now_values()}
    found = guards.evaluate(
        entity.type,
        _variables(entity.type),
        dict.fromkeys(purposes, conditions),
        {p: {**base, "purpose": p} for p in purposes},
        {},
    )
    return {p: refused.message for p, refused in found.items()}

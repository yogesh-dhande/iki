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


def refusals(
    entity: Entity,
    purposes: Sequence[str],
    values: Mapping[str, Any] | None = None,
    *,
    operation: str = "draw",
) -> dict[str, str]:
    """The reason each purpose is refused, using the same checks for display and withdrawal."""
    conditions = draw_guards(entity.type)
    if not conditions:
        return {}
    if values is None:
        from iki_server.entities.services import current_values_of

        values = current_values_of([entity])[entity.pk]
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

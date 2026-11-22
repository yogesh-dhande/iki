"""Type-declared conditions on taking material from a record (ADR 0139)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any

from iki_server.entities import checking, guards, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import shape
from iki_spec import rules
from iki_spec.model import Guard


@cache
def draw_guards(type_name: str) -> tuple[Guard, ...]:
    """The type's draw guards, including every ancestor's."""
    entity_type = registry.types()[type_name]
    chain = [*reversed(registry.ancestors(entity_type)), entity_type]
    return tuple(g for t in chain for g in t.draw_guards.values())


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

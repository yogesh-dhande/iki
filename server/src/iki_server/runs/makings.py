"""How entities were made, and who says so (ADR 0106).

A run of an action that lists ``software`` records each entity it creates as made with it,
or with the tools its output names in ``made_with``: Iki's own record, which the run and
its environment build prove. An import's origin may carry ``made``, what the record says
about how its source made the entity: kept as that source's statement, never as Iki's.

Both are rows of :class:`EntityMaking`, one per tool, so a search's condition matches one
row: "made with AlphaFold, recorded by Iki" can't match a source's AlphaFold and Iki's
ImmuneBuilder.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from iki_server.runs.models import EntityMaking, ExternalOrigin, MadeBy, Making
from iki_spec.model import Software


def recorded(run_id: UUID, entity_id: UUID, software: Iterable[Software]) -> list[EntityMaking]:
    """Iki's record: ``entity_id`` was computed by the run, with ``software``."""
    return [
        EntityMaking(
            entity_id=entity_id,
            run_id=run_id,
            by=MadeBy.IKI,
            kind=Making.COMPUTATIONAL,
            software=tool.name,
            version=tool.version,
        )
        for tool in software
    ]


def stated(origin: ExternalOrigin) -> list[EntityMaking]:
    """What ``origin``'s record says about how its source made the entity, if anything."""
    made: Mapping[str, Any] | None = origin.made
    if not made:
        return []
    tools = made.get("software") or [{}]
    return [
        EntityMaking(
            entity_id=origin.entity_id,
            run_id=origin.run_id,
            origin=origin,
            by=MadeBy.SOURCE,
            kind=made["kind"],
            method=made.get("method", ""),
            software=tool.get("name", ""),
            version=tool.get("version", ""),
        )
        for tool in tools
    ]


def record(
    run_id: UUID,
    entity_id: UUID,
    software: Iterable[Software],
    origin: ExternalOrigin | None,
) -> None:
    """Keep how one created (or found again) entity was made."""
    rows = recorded(run_id, entity_id, software)
    if origin is not None:
        rows += stated(origin)
    if rows:
        EntityMaking.objects.bulk_create(rows)

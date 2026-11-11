"""Collections (core.Collection, ADR 0038) from searches: fixed lists of what a search finds.

- **Freezing** a dynamic collection replaces its search with the members it
  finds now, in its order, as one revision. A run or a paper can then cite
  exactly those entities.
- **Making one from a search** keeps either the search (dynamic) or what it
  finds now (static), in one step, so nothing half-made is left behind.

Both run the search as the caller, so a fixed list only holds entities they
could read. A static collection holds at most :data:`MAX_MEMBERS`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal

from iki_server.accounts.models import User
from iki_server.entities import registry, rules, search, services
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid, shape
from iki_server.org.models import Project

COLLECTION = "core.Collection"


def max_members() -> int:
    """The most members a static collection can list: its manifest's ``max_items``."""
    members = shape(COLLECTION).fields()["members"]
    return members.max_items or 10_000


class TooMany(Exception):  # noqa: N818 - reads as "the search finds too many"
    def __init__(self, limit: int) -> None:
        super().__init__(
            f"Its search finds more than {limit:,} entities, the most a fixed list can hold. "
            "Narrow the search, or keep the collection dynamic."
        )
        self.limit = limit


class NotDynamic(Exception):  # noqa: N818 - reads as "it isn't dynamic"
    pass


def is_collection(type_name: str) -> bool:
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return False
    return any(
        t.qualified_name == COLLECTION for t in [entity_type, *registry.ancestors(entity_type)]
    )


def found_now(user: User, data: Mapping[str, Any]) -> list[uuid.UUID]:
    """What the search finds for ``user`` now, in its order. Raises Invalid or TooMany."""
    errors = rules.search_errors(user, data)
    if errors:
        raise Invalid({"search": errors})
    wanted = search.stored(data)
    limit = max_members()
    found = search.queryset(user, wanted).select_related(None).only("pk")
    with search.time_limit(search.TIME_LIMIT_MS):
        rows, more = search.page(found, wanted, None, limit)
    if more is not None:
        raise TooMany(limit)
    return [row.pk for row in rows]


def create_from_search(
    user: User,
    project: Project,
    *,
    name: str,
    description: str | None,
    data: Mapping[str, Any],
    keep: Literal["search", "members"],
    reason: str = "",
) -> services.Saved:
    """A new collection in ``project``: dynamic (``keep="search"``) or a fixed list of what
    the search finds now (``keep="members"``)."""
    values: dict[str, Any] = {"name": name}
    if description:
        values["description"] = description
    if keep == "search":
        values["search"] = dict(data)
    else:
        members = found_now(user, data)
        values["members"] = [str(m) for m in members]
        reason = reason or _frozen(len(members))
    return services.create(user, COLLECTION, project, values, reason=reason)


def freeze(
    user: User, entity: Entity, *, expected_revision: int, reason: str = ""
) -> services.Saved:
    """Replaces a dynamic collection's search with the members it finds now."""
    if not is_collection(entity.type):
        raise NotDynamic("Only a collection can be frozen.")
    data = registry.model(entity.type)._default_manager.get(pk=entity.pk).search  # type: ignore[attr-defined]
    if data is None:
        raise NotDynamic("It lists its members already: it has no search to freeze.")
    members = found_now(user, data)
    # If it changed since ``expected_revision`` (its search too), this is refused as stale.
    return services.update(
        user,
        entity,
        {"members": [str(m) for m in members], "search": None},
        expected_revision=expected_revision,
        reason=reason or _frozen(len(members)),
    )


def _frozen(count: int) -> str:
    return f"Frozen: the {count:,} {'entity' if count == 1 else 'entities'} its search found."

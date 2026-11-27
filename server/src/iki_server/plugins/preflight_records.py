"""Read a bounded set of records from the schema the last successful deploy installed.

The candidate's Django models may already name columns that do not exist. Only the
base entity query uses the ORM. Typed and list tables are addressed by the deployed
manifests, under the same naming and entry-storage conventions as code generation.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import connection
from psycopg import sql

from iki_server.entities import checking
from iki_server.entities.models import Entity
from iki_server.entities.values import FOREIGN_KEYS, is_list_table, row_columns, to_json
from iki_server.plugins.preflight_rules import projected_changes
from iki_server.plugins.preflight_types import Record
from iki_spec import naming
from iki_spec.model import (
    BUILTIN_TYPES,
    EntityType,
    Field,
    Plugin,
    as_refined,
    split_entry,
)

#: Item/row budget per normalized list table, across the sample. A partial list
#: is never passed on to a rule as if it were the stored value.
LIST_ROWS = 4096


@dataclass(frozen=True)
class Table:
    table: str
    fields: dict[str, Field]
    entry: bool
    owner: str
    plugin: str
    extension: bool


def lineage(qualified: str, plugins: Mapping[str, Plugin]) -> list[EntityType]:
    """The type and its ancestors, nearest first, as described by this build."""
    types = dict(BUILTIN_TYPES)
    for plugin in plugins.values():
        types.update(plugin.types)
    chain: list[EntityType] = []
    seen = set()
    while qualified != naming.ENTITY:
        if qualified in seen or qualified not in types:
            raise ValueError(f"Invalid type ancestry at {qualified}.")
        seen.add(qualified)
        item = types[qualified]
        chain.append(item)
        qualified = item.extends or naming.ENTITY
    return chain


def layout(
    qualified: str, plugins: Mapping[str, Plugin], *, chain: Sequence[EntityType] | None = None
) -> list[Table]:
    """The effective current fields grouped by the physical table holding them."""
    if chain is None:
        chain = lineage(qualified, plugins)
    names = {t.qualified_name for t in chain}
    parts = []
    for t in reversed(chain):
        fields = {n: as_refined(chain, n, f) for n, f in t.current_fields.items()}
        parts.append((t.table, t.qualified_name, t.plugin, False, split_entry(fields, t.identity)))
    parts.extend(
        (ext.table, ext.extends_type, ext.plugin, True, split_entry(ext.current_fields))
        for plugin in plugins.values()
        for ext in plugin.extensions
        if ext.extends_type in names
    )
    return [
        Table(naming.entry_table(table) if entry else table, fields, entry, owner, plugin_name, ext)
        for table, owner, plugin_name, ext, split in parts
        for entry, fields in zip((False, True), split, strict=True)
        if fields
    ]


def _record(
    pk: UUID, hrid: str, project: UUID | None, registry: UUID | None, entry: UUID | None
) -> Record:
    return Record(
        id=str(pk),
        hrid=hrid,
        project=str(project) if project else None,
        registry=str(registry) if registry else None,
        entry=str(entry) if entry else None,
        values={},
    )


def records(
    qualified: str,
    current: Mapping[str, Plugin],
    limit: int,
    *,
    fields: Collection[str] | None = None,
) -> tuple[list[Record], bool]:
    """At most ``limit`` project records of one exact type and whether the page was capped.

    (type, hrid, id) is an existing ordered index. Only the selected keys fetch values;
    when a field is newly declared it needs no read from a plugin table at all.
    """
    keys = Entity.objects.filter(type=qualified, registry__isnull=True, purged_at__isnull=True)
    keys = keys.order_by("hrid", "id")
    selected = list(
        keys.values_list(
            "id", "hrid", "owner_project_id", "owner_project__registry_id", "entry_id"
        )[: limit + 1]
    )
    page = [_record(*row) for row in selected[:limit]]
    incomplete = set()
    if page and (fields is None or fields):
        incomplete = _fill(page, layout(qualified, current), fields, cel=False)
    return [r for r in page if r.id not in incomplete], len(selected) > limit or bool(incomplete)


def _stored_fields(tables: Sequence[Table], *, entry: bool) -> dict[str, Field]:
    return {n: f for t in tables if t.entry == entry for n, f in t.fields.items()}


def referenced(
    ids: Collection[str],
    current: Mapping[str, Plugin],
    proposed: Mapping[str, Plugin] | None = None,
    *,
    limit: int = 1024,
) -> dict[str, dict[str, Any]]:
    """One-hop CEL values for referenced records and entries. A missing target is unread."""
    wanted = []
    for value in sorted(set(ids))[:limit]:
        # A new reference field may previously have been plain text.
        try:
            wanted.append(UUID(value))
        except ValueError:
            continue
    if not wanted:
        return {}
    found = Entity.objects.filter(pk__in=wanted, purged_at__isnull=True).order_by()
    grouped: dict[str, list[Record]] = defaultdict(list)
    for kind, pk, hrid, project, registry, entry in found.values_list(
        "type", "id", "hrid", "owner_project_id", "registry_id", "entry_id"
    ):
        if kind in BUILTIN_TYPES or any(kind in p.types for p in current.values()):
            grouped[kind].append(_record(pk, hrid, project, registry, entry))
    out = {}
    for kind, page in grouped.items():
        tables = layout(kind, current)
        if proposed is not None:
            candidate_types = {n for p in proposed.values() for n in p.types}
            if kind not in BUILTIN_TYPES and kind not in candidate_types:
                continue
            after = layout(kind, proposed)
            # An entry exposes only shared fields. A project record exposes both.
            # CEL can still read their deployed values when the only change is a
            # label or a tighter constraint. A migration changing a value itself
            # would make the old value unreliable for evaluating the proposed rule.
            if projected_changes(
                _stored_fields(tables, entry=True), _stored_fields(after, entry=True)
            ):
                continue
            if projected_changes(
                _stored_fields(tables, entry=False), _stored_fields(after, entry=False)
            ):
                page = [r for r in page if r.project is None]
        incomplete = _fill(page, tables, None, cel=True)
        out.update(
            {r.id: {"id": r.id, "hrid": r.hrid, **r.values} for r in page if r.id not in incomplete}
        )
    return out


def historical(
    keys: set[tuple[str, int]],
    projected: Mapping[str, dict[str, Any]],
    deployed: Mapping[str, Plugin],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Read pins only when the deployed tables match the runtime models and projection."""
    from iki_server.entities import registry

    pairs = sorted((pk, rev) for pk, rev in keys if pk in projected)[:1024]
    if not pairs:
        return {}
    kinds = {
        str(pk): kind
        for pk, kind in Entity.objects.filter(pk__in={pk for pk, _ in pairs}).values_list(
            "pk", "type"
        )
    }
    installed = registry.plugins()
    compatible = {
        kind
        for kind in set(kinds.values())
        if kind in registry.types() and layout(kind, deployed) == layout(kind, installed)
    }
    return {
        (pk, number): record
        for pk, number in pairs
        if kinds.get(pk) in compatible
        and (record := checking.historical_reference(UUID(pk), number)) is not None
    }


def _fill(
    page: Sequence[Record], tables: Sequence[Table], fields: Collection[str] | None, *, cel: bool
) -> set[str]:
    incomplete: set[str] = set()
    convert = checking.cel_value if cel else to_json
    for table in tables:
        selected = {n: f for n, f in table.fields.items() if fields is None or n in fields}
        if not selected:
            continue
        # An entry can have records in several projects. Fetch its values once.
        owners: dict[str, list[Record]] = defaultdict(list)
        for r in page:
            if r.project is None and not table.entry:
                continue
            pk = r.entry if table.entry and r.project else r.id
            if pk:
                owners[pk].append(r)
        if not owners:
            continue
        scalars = {n: f for n, f in selected.items() if not is_list_table(f)}
        if scalars:
            columns = [f"{n}_id" if f.type in FOREIGN_KEYS else n for n, f in scalars.items()]
            scalar_rows = {
                str(pk): values for pk, *values in _rows(table.table, columns, owners.keys())
            }
            for pk, copies in owners.items():
                values = scalar_rows.get(pk, [None] * len(columns))
                mapped = {
                    n: convert(f, _decoded(f, v))
                    for (n, f), v in zip(scalars.items(), values, strict=True)
                }
                for r in copies:
                    r.values.update(mapped)
        for name, f in selected.items():
            if not is_list_table(f):
                continue
            columns_by_name = row_columns(f) if f.is_rows else {"item": "item_id"}
            items: dict[str, list[Any]] = defaultdict(list)
            rows = _rows(
                naming.list_table(table.table, name),
                list(columns_by_name.values()),
                owners.keys(),
                ordered=True,
            )
            if len(rows) > LIST_ROWS:
                # The extra row's key and everything ordered after it may have
                # unread items. With no rows for those keys we cannot assume [].
                first_unread = str(rows.pop()[0])
                incomplete.update(
                    r.id for pk, copies in owners.items() if pk >= first_unread for r in copies
                )
            for pk, *values in rows:
                value = dict(zip(columns_by_name, values, strict=True)) if f.is_rows else values[0]
                items[str(pk)].append(value)
            for pk, copies in owners.items():
                ordered = convert(f, items.get(pk, []))
                for r in copies:
                    r.values[name] = ordered
    return incomplete


def _rows(
    table: str, columns: Sequence[str], ids: Collection[str], *, ordered: bool = False
) -> list[tuple[Any, ...]]:
    query = sql.SQL("SELECT entity_id, {} FROM {} WHERE entity_id = ANY(%s::uuid[])").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table)
    )
    if ordered:
        query += sql.SQL(" ORDER BY entity_id, position LIMIT {}").format(
            sql.Literal(LIST_ROWS + 1)
        )
    with connection.cursor() as cursor:
        cursor.execute(query, [list(ids)])
        result: list[tuple[Any, ...]] = cursor.fetchall()
        return result


def _decoded(field: Field, raw: Any) -> Any:  # noqa: ANN401 - raw database values
    # Django's psycopg connection returns JSONB text for JSONField.from_db_value.
    if field.type == "json" and isinstance(raw, str):
        return json.loads(raw)
    if field.type == "list" and field.items and field.items.type == "json" and raw is not None:
        return [_decoded(field.items, value) for value in raw]
    return raw

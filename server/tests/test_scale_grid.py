"""Load test (ADR 0103): the grid stays responsive at 100M rows.

A project holds ``IKI_SCALE_GRID_ROWS`` proteins (100,000,000 by default): one made
through Iki, then copied in bulk, as a restore loads a database, its indexes and unique
constraints built afterwards. Then a viewer opens the grid's pages: the first, the next,
one deep into the table, newest first, sorted and filtered by indexed fields, and
searched. Each must answer within the grid's budget. ``make check-scale`` runs it.
"""

import os
import statistics
import time
import uuid
from typing import Any

import pytest
from api_client import Api
from django.db import connection, models
from org_world import World, build_world

from iki_server.entities import search
from iki_server.entities import services as entities
from iki_server.entities.models import Entity, ProjectLink

pytestmark = [pytest.mark.scale, pytest.mark.django_db(transaction=True)]

ROWS = int(os.environ.get("IKI_SCALE_GRID_ROWS", "100000000"))
CHUNK = 5_000_000
TYPE = "acme/protein.Protein"
#: The grid's page, as the web app asks for it.
LIMIT = 100
#: What a page may take at the server, at the median of a few tries.
BUDGET = 1.0
#: The copies' IDs are UUIDv7s from 2020 on, one a millisecond: older than the one made
#: through Iki, so in ID order they come first.
BASE_MS = 1_577_836_800_000


def entity_id(n: int) -> uuid.UUID:
    """The n-th copy's ID: a UUIDv7, its time, then n."""
    return uuid.UUID(f"{BASE_MS + n:012x}70008000{n:012x}")


def _uuid(variant: str) -> str:
    return (
        f"(lpad(to_hex({BASE_MS} + n), 12, '0') || '{variant}' || lpad(to_hex(n), 12, '0'))::uuid"
    )


ID, LINK_ID = _uuid("70008000"), _uuid("70018000")

#: What each copy has of its own, by column; the rest is the template's.
OWN = {
    "hrid": "'PRT-' || lpad(n::text, 9, '0')",
    "identity_hash": "encode(sha256(n::text::bytea), 'hex')",
    "sequence": "'M' || translate(lpad(n::text, 9, '0'), '0123456789', 'ACDEFGHIKL')",
    "name": "'P' || n",
    "molecular_weight": "1100 + (n * 7919) %% 100000",
    "lims_id": "'LIMS-' || n",
    "temperature_optimum": "20 + (n * 104729) %% 6000 / 100.0",
}


def _columns(model: type[models.Model], key: str) -> dict[str, str]:
    """Each of the model's own columns, and what a copy puts in it."""
    found: dict[str, str] = {}
    for field in model._meta.local_concrete_fields:
        if getattr(field, "generated", False):
            continue
        column = str(field.column)
        found[column] = ID if column == key else OWN.get(column, f't."{column}"')
    return found


def _sql(statement: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def _secondary(tables: list[str]) -> tuple[list[str], list[str]]:
    """The tables' indexes and unique constraints, all but primary keys: (drop, create)."""
    drop, create = [], []
    for table, name, definition in _sql(
        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE contype = 'u' AND conrelid::regclass::text = ANY(%s)",
        [tables],
    ):
        drop.append(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
        create.append(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" {definition}')
    for name, definition in _sql(
        "SELECT i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid) FROM pg_index i "
        "WHERE i.indrelid::regclass::text = ANY(%s) AND NOT i.indisprimary "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)",
        [tables],
    ):
        drop.append(f"DROP INDEX {name}")
        create.append(definition)
    return drop, create


def fill(template: Entity, rows: int) -> None:
    """``rows`` copies of ``template``, each with its own ID, HRID and values, in each
    of its tables and its project's."""
    form = search.form_of(TYPE)
    assert form is not None
    model = form.model
    chain = [*reversed(model._meta.get_parent_list()), model]
    copies: list[tuple[str, dict[str, str], str]] = []
    for each in chain:
        key = str(each._meta.pk.column)
        copies.append((each._meta.db_table, _columns(each, key), f't."{key}" = %s'))
    link = _columns(ProjectLink, "id") | {"id": LINK_ID, "entity_id": ID}
    copies.append((ProjectLink._meta.db_table, link, 't."entity_id" = %s'))
    tables = [table for table, _, _ in copies]
    drop, create = _secondary(tables)
    for statement in drop:
        _sql(statement)
    # Foreign keys and triggers aren't checked while copying: the copies point where the
    # template does, which they're checked against.
    _sql("SET session_replication_role = replica")
    began = time.monotonic()
    try:
        for lo in range(1, rows + 1, CHUNK):
            hi = min(lo + CHUNK - 1, rows)
            for table, columns, where in copies:
                names = ", ".join(f'"{c}"' for c in columns)
                _sql(
                    f"INSERT INTO {table} ({names}) SELECT {', '.join(columns.values())} "  # noqa: S608
                    f"FROM {table} t, generate_series(%s::bigint, %s::bigint) n WHERE {where}",
                    [lo, hi, template.pk],
                )
            print(f"copied {hi:,} in {time.monotonic() - began:.0f}s", flush=True)  # noqa: T201
    finally:
        _sql("SET session_replication_role = DEFAULT")
    began = time.monotonic()
    _sql("SET max_parallel_maintenance_workers = 4")
    _sql("SET maintenance_work_mem = '1GB'")
    for statement in create:
        _sql(statement)
    print(f"indexed in {time.monotonic() - began:.0f}s", flush=True)  # noqa: T201
    began = time.monotonic()
    for table in tables:
        _sql(f"VACUUM (ANALYZE) {table}")
    print(f"vacuumed in {time.monotonic() - began:.0f}s", flush=True)  # noqa: T201
    ((size,),) = _sql("SELECT pg_size_pretty(pg_database_size(current_database()))")
    print(f"the database is {size}", flush=True)  # noqa: T201


def _timed(client: Api, body: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """The median of five tries, after one to warm up; and what it answered."""
    answer = client.call("POST", "/entities/search", body)
    assert answer.status_code == 200, (body, answer.content[:300])
    took = []
    for _ in range(5):
        began = time.perf_counter()
        again = client.call("POST", "/entities/search", body)
        took.append(time.perf_counter() - began)
        assert again.status_code == 200, (body, again.content[:300])
    return statistics.median(took), answer.json()


def test_the_grid_stays_responsive_at_100m_rows() -> None:
    world: World = build_world()
    made = entities.create(
        world.users["admin"],
        TYPE,
        world.project,
        {"sequence": "MLEICLKLVG", "name": "ABL1"},
    )
    template = Entity.objects.get(pk=made.entity.pk)
    fill(template, ROWS)
    client = Api(world.browser("viewer"))
    project = str(world.project.pk)
    grid = {"type": TYPE, "project": project, "limit": LIMIT}

    def cursor_at(n: int) -> str:
        return search._encode((None, False), None, entity_id(n))

    first_took, first = _timed(client, grid)
    assert len(first["items"]) == LIMIT
    pages: dict[str, dict[str, Any]] = {
        "the next page": grid | {"cursor": first["next_cursor"]},
        "a page 90% of the way in": grid | {"cursor": cursor_at(int(ROWS * 0.9))},
        "newest first": grid | {"sort": {"field": "created_at", "descending": True}},
        "by ID": grid | {"sort": {"field": "hrid"}},
        "by ID, last first": grid | {"sort": {"field": "hrid", "descending": True}},
        "last changed first": grid | {"sort": {"field": "updated_at", "descending": True}},
        "heaviest first": grid | {"sort": {"field": "molecular_weight", "descending": True}},
        "lightest first": grid | {"sort": {"field": "molecular_weight"}},
        "a LIMS ID": grid
        | {"where": {"field": "lims_id", "op": "eq", "value": f"LIMS-{ROWS // 2}"}},
        "a narrow weight range": grid
        | {
            "where": {
                "all": [
                    {"field": "molecular_weight", "op": "gte", "value": 50_000},
                    {"field": "molecular_weight", "op": "lt", "value": 50_010},
                ]
            }
        },
        "an HRID": grid | {"q": f"PRT-{ROWS // 3:09d}"},
        "names containing": grid
        | {"where": {"field": "name", "op": "contains", "value": f"P{ROWS // 7}"}},
    }
    took = {"the first page": first_took}
    for name, body in pages.items():
        took[name], answer = _timed(client, body)
        assert answer["items"], name
        # And the page after: a sorted grid's cursor carries the value it stopped at.
        if "sort" in body and answer["next_cursor"]:
            took[f"{name}, the next page"], after = _timed(
                client, body | {"cursor": answer["next_cursor"]}
            )
            assert after["items"], name
    for name, seconds in took.items():
        print(f"{name}: {seconds * 1000:.0f} ms")  # noqa: T201 - the load test's report
    # A field without an index: sorting this many by it would take too long, which the
    # grid hears at once, not when its time is up.
    began = time.perf_counter()
    unindexed = client.call(
        "POST", "/entities/search", grid | {"sort": {"field": "temperature_optimum"}}
    )
    took["by an unindexed field"] = time.perf_counter() - began
    if ROWS > search.UNINDEXED_SORT_MAX:
        assert unindexed.status_code == 422, unindexed.content[:300]
        said = unindexed.json()
        assert said["code"] == "search_too_slow"
        assert "by Temperature optimum would take too long: it isn't indexed" in said["detail"]
    else:
        assert unindexed.status_code == 200, unindexed.content[:300]
    print(f"by an unindexed field: {took['by an unindexed field'] * 1000:.0f} ms")  # noqa: T201
    slow = {name: f"{s:.2f}s" for name, s in took.items() if s > BUDGET}
    assert not slow, slow

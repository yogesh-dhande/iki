"""Load test (ADR 0103): the grid stays responsive at 100M rows.

A project holds ``IKI_SCALE_GRID_ROWS`` proteins (100,000,000 by default): one made
through Iki, then copied in bulk, as a restore loads a database, its indexes and unique
constraints built afterwards. Each copy is an identity of its own (ADR 0115): a registry
entry, with its sequence and identity hash, and the project's record of it, linked to the
project. Then a viewer opens the grid's pages: the first, the next, one deep into the
table, newest first, sorted and filtered by indexed fields, and searched. Each must answer
within the grid's budget. ``make check-scale`` runs it; a thousand rows run with every
check, so the copies are known to load before the nightly run copies millions.
"""

import datetime as dt
import os
import statistics
import time
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from api_client import Api
from django.db import connection, models, transaction
from django.test.utils import CaptureQueriesContext
from org_world import World, build_world

from iki_server.entities import identity, registry, search
from iki_server.entities import services as entities
from iki_server.entities.models import Entity, ProjectLink
from iki_server.org.models import Project, ProjectMembership, ProjectRole, registry_of
from iki_spec.identity import encode

pytestmark = pytest.mark.django_db(transaction=True)

ROWS = int(os.environ.get("IKI_SCALE_GRID_ROWS", "100000000"))
#: Enough for every page the test opens, few enough for every check.
SMALL = 1_000
CHUNK = 5_000_000
TYPE = "acme/protein.Protein"
#: The grid's page, as the web app asks for it.
LIMIT = 100
#: What a page may take at the server, at the median of a few tries.
BUDGET = 1.0
#: The copies' IDs are UUIDv7s from 2020 on, one a millisecond: older than the one made
#: through Iki, so in ID order they come first.
BASE_MS = 1_577_836_800_000
RESIDUES = str.maketrans("0123456789", "ACDEFGHIKL")


def entity_id(n: int) -> uuid.UUID:
    """The n-th copy's ID, its record's: a UUIDv7, its time, then n."""
    return uuid.UUID(f"{BASE_MS + n:012x}70008000{n:012x}")


def sequence(n: int) -> str:
    """The n-th copy's sequence, as ``SEQUENCE`` writes it."""
    return "M" + f"{n:09d}".translate(RESIDUES)


def _uuid(variant: str) -> str:
    return (
        f"(lpad(to_hex({BASE_MS} + n), 12, '0') || '{variant}' || lpad(to_hex(n), 12, '0'))::uuid"
    )


#: Each copy's three rows of its own: its record, its registry entry, and the record's link
#: to the project. Their IDs differ in the variant's digits.
ID, ENTRY_ID, LINK_ID = _uuid("70008000"), _uuid("70028000"), _uuid("70018000")
HRID = "'PRT-' || lpad(n::text, 9, '0')"
SEQUENCE = "'M' || translate(lpad(n::text, 9, '0'), '0123456789', 'ACDEFGHIKL')"


def _literal(text: str) -> str:
    """``text`` as an SQL string, among the copies' statements' parameters."""
    return "'" + text.replace("'", "''").replace("%", "%%") + "'"


def _identity_hash() -> str:
    """Each copy's identity hash, as Iki would compute it from its sequence: the SHA-256
    of the identity's canonical encoding, which the sequence is spelled in already."""
    owner = registry.identity_owner(registry.types()[TYPE])
    assert owner is not None
    assert owner.identity == ("sequence",), owner.identity
    marker = "MARKER"
    before, after = (
        encode(owner.qualified_name, {"sequence": ("string", marker)}).decode().split(marker)
    )
    return (
        f"encode(sha256(convert_to({_literal(before)} || {SEQUENCE} || {_literal(after)}, "
        "'UTF8')), 'hex')"
    )


#: When each copy changed, a second apart, as records edited over years are: its entry with
#: its record.
UPDATED_AT = "timestamptz '2020-01-01 00:00:00+00' + n * interval '1 second'"
#: What each copy's entry has of its own, by column, with its identity hash; the rest is the
#: template's entry's.
ENTRY_OWN = {"hrid": HRID, "sequence": SEQUENCE, "updated_at": UPDATED_AT}
#: What each copy's record has of its own; the rest is the template's. Its HRID is its
#: entry's, and its identity hash stays the template's, which a record hasn't got.
RECORD_OWN = {
    "hrid": HRID,
    "entry_id": ENTRY_ID,
    "name": "'P' || n",
    "molecular_weight": "1100 + (n * 7919) %% 100000",
    "lims_id": "'LIMS-' || n",
    "temperature_optimum": "20 + (n * 104729) %% 6000 / 100.0",
    "updated_at": UPDATED_AT,
}


def weight(n: int) -> int:
    """The n-th copy's molecular weight, as ``RECORD_OWN`` writes it."""
    return 1100 + (n * 7919) % 100000


def _columns(model: type[models.Model], key: str, own: dict[str, str]) -> dict[str, str]:
    """Each of the model's own columns, and what a copy puts in it: ``key`` for its primary
    key, what ``own`` says, or the template's."""
    found: dict[str, str] = {}
    pk = str(model._meta.pk.column)
    for field in model._meta.local_concrete_fields:
        if getattr(field, "generated", False):
            continue
        column = str(field.column)
        found[column] = key if column == pk else own.get(column, f't."{column}"')
    return found


def _chain(model: type[models.Model]) -> list[type[models.Model]]:
    """The model's tables, from ``iki_entity`` down to its own."""
    return [*reversed(model._meta.get_parent_list()), model]


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
    """``rows`` copies of ``template``, a project's record of a registry entry: for each, an
    entry of its own, in each of the entry's tables, and the project's record of it, in
    each of the record's tables and its project's.

    One transaction loads them, as a restore can: the database checks the rules
    restoring its indexes and unique constraints, and if any is broken, the copies and
    the dropped indexes are rolled back together."""
    form = search.form_of(TYPE)
    assert form is not None and form.entry_model is not None
    assert template.entry_id is not None, "a protein's record has its registry entry"
    # Each table's copies: its columns', and the column and value that pick the template's
    # row in it. Entries first: records name theirs.
    copies: list[tuple[str, dict[str, str], str, uuid.UUID]] = []
    entry_own = ENTRY_OWN | {"identity_hash": _identity_hash()}
    for each in _chain(form.entry_model):
        columns = _columns(each, ENTRY_ID, entry_own)
        copies.append((each._meta.db_table, columns, str(each._meta.pk.column), template.entry_id))
    for each in _chain(form.model):
        columns = _columns(each, ID, RECORD_OWN)
        copies.append((each._meta.db_table, columns, str(each._meta.pk.column), template.pk))
    link = _columns(ProjectLink, LINK_ID, {"entity_id": ID})
    copies.append((ProjectLink._meta.db_table, link, "entity_id", template.pk))
    tables = list(dict.fromkeys(table for table, _, _, _ in copies))
    with transaction.atomic():
        drop, create = _secondary(tables)
        for statement in drop:
            _sql(statement)
        # Foreign keys and triggers aren't checked while copying: the copies point where
        # the template does, which they're checked against, and records at their entries.
        _sql("SET LOCAL session_replication_role = replica")
        began = time.monotonic()
        for lo in range(1, rows + 1, CHUNK):
            hi = min(lo + CHUNK - 1, rows)
            for table, columns, key, source in copies:
                names = ", ".join(f'"{c}"' for c in columns)
                _sql(
                    f"INSERT INTO {table} ({names}) SELECT {', '.join(columns.values())} "  # noqa: S608
                    f"FROM {table} t, generate_series(%s::bigint, %s::bigint) n "
                    f'WHERE t."{key}" = %s',
                    [lo, hi, source],
                )
            print(f"copied {hi:,} in {time.monotonic() - began:.0f}s", flush=True)  # noqa: T201
        _sql("SET LOCAL session_replication_role = DEFAULT")
        began = time.monotonic()
        _sql("SET LOCAL max_parallel_maintenance_workers = 4")
        _sql("SET LOCAL maintenance_work_mem = '1GB'")
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


def _plan_of(client: Api, body: dict[str, Any], order: str) -> dict[str, Any]:
    """How Postgres plans the page ``body`` asks for, as the search runs it: the statement
    that reads its rows (ordered by ``order``), explained in the search's own settings
    (ADR 0103)."""
    with CaptureQueriesContext(connection) as ran:
        answer = client.call("POST", "/entities/search", body)
    assert answer.status_code == 200, answer.content[:300]
    (statement,) = [q["sql"] for q in ran.captured_queries if f"ORDER BY {order}" in q["sql"]]
    with search.time_limit(), connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (FORMAT JSON) {statement}")
        plan: dict[str, Any] = cursor.fetchone()[0][0]["Plan"]
        # And as it ran, for the load test's report.
        cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {statement}")
        print("\n".join(row[0] for row in cursor.fetchall()))  # noqa: T201 - the report
    return plan


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def _check_copies(world: World, rows: int) -> None:
    """Each copy is an identity of the project's registry, which Iki finds as it would
    one registered through it: a registration of its sequence is the project's record."""
    entity_type = registry.types()[TYPE]
    for n in sorted({1, rows // 2, rows}):
        ((hrid, registry_id, project, record_hash, entry_hash),) = _sql(
            "SELECT r.hrid, e.registry_id, r.owner_project_id, r.identity_hash, "
            "e.identity_hash FROM iki_entity r JOIN iki_entity e ON e.id = r.entry_id "
            "WHERE r.id = %s AND e.hrid = r.hrid AND e.owner_project_id IS NULL",
            [entity_id(n)],
        )
        assert hrid == f"PRT-{n:09d}"
        assert (registry_id, project) == (registry_of(world.project), world.project.pk)
        assert record_hash is None
        assert entry_hash == identity.hash_of({"sequence": sequence(n)}, entity_type)
    again = entities.create(
        world.users["admin"], TYPE, world.project, {"sequence": sequence(rows // 2)}
    )
    assert again.reused
    assert again.entity.pk == entity_id(rows // 2)


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(SMALL, id="small"),
        pytest.param(ROWS, id="scale", marks=pytest.mark.scale),
    ],
)
def test_the_grid_stays_responsive(rows: int) -> None:
    world: World = build_world()
    made = entities.create(
        world.users["admin"],
        TYPE,
        world.project,
        {"sequence": "MLEICLKLVG", "name": "ABL1"},
    )
    template = Entity.objects.get(pk=made.entity.pk)
    fill(template, rows)
    _check_copies(world, rows)
    client = Api(world.browser("viewer"))
    project = str(world.project.pk)
    grid = {"type": TYPE, "project": project, "limit": LIMIT}

    def cursor_at(n: int) -> str:
        return search._encode((None, False), None, entity_id(n))

    first_took, first = _timed(client, grid)
    assert len(first["items"]) == LIMIT
    pages: dict[str, dict[str, Any]] = {
        "the next page": grid | {"cursor": first["next_cursor"]},
        "a page 90% of the way in": grid | {"cursor": cursor_at(int(rows * 0.9))},
        "newest first": grid | {"sort": {"field": "created_at", "descending": True}},
        "by ID": grid | {"sort": {"field": "hrid"}},
        "by ID, last first": grid | {"sort": {"field": "hrid", "descending": True}},
        "last changed first": grid | {"sort": {"field": "updated_at", "descending": True}},
        "heaviest first": grid | {"sort": {"field": "molecular_weight", "descending": True}},
        "lightest first": grid | {"sort": {"field": "molecular_weight"}},
        "a LIMS ID": grid
        | {"where": {"field": "lims_id", "op": "eq", "value": f"LIMS-{rows // 2}"}},
        "a narrow weight range": grid
        | {
            "where": {
                "all": [
                    {"field": "molecular_weight", "op": "gte", "value": weight(rows // 5)},
                    {"field": "molecular_weight", "op": "lt", "value": weight(rows // 5) + 10},
                ]
            }
        },
        "an HRID": grid | {"q": f"PRT-{rows // 3:09d}"},
        "names containing": grid
        | {"where": {"field": "name", "op": "contains", "value": f"P{rows // 7}"}},
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
    # Timed as the pages are, the median of a few tries after one to warm up; it answers
    # 422 at scale, so not through _timed.
    by_unindexed = grid | {"sort": {"field": "temperature_optimum"}}
    unindexed = client.call("POST", "/entities/search", by_unindexed)
    tries = []
    for _ in range(3):
        began = time.perf_counter()
        client.call("POST", "/entities/search", by_unindexed)
        tries.append(time.perf_counter() - began)
    took["by an unindexed field"] = statistics.median(tries)
    if rows > search.UNINDEXED_SORT_MAX:
        assert unindexed.status_code == 422, unindexed.content[:300]
        said = unindexed.json()
        assert said["code"] == "search_too_slow"
        assert "by Temperature optimum would take too long: it isn't indexed" in said["detail"]
    else:
        assert unindexed.status_code == 200, unindexed.content[:300]
    print(f"by an unindexed field: {took['by an unindexed field'] * 1000:.0f} ms")  # noqa: T201

    # The lists' default order (ADR 0176): every type the reader can read, last changed
    # first. No type is named, so the index on when records changed reads the page, backwards,
    # and nothing is sorted.
    newest = {"limit": LIMIT, "sort": {"field": "updated_at", "descending": True}}
    took["every type's, last changed first"], answer = _timed(client, newest)
    assert answer["items"][0]["hrid"] == template.hrid
    took["every type's, last changed first, the next page"], after = _timed(
        client, newest | {"cursor": answer["next_cursor"]}
    )
    assert len(after["items"]) == LIMIT
    plan = _plan_of(client, newest, '"iki_entity"."updated_at" DESC')
    used = {n.get("Index Name") for n in _nodes(plan)}
    # Records sorted, that is: a reader's role in each project, picked from its few
    # memberships, is sorted too, and costs nothing.
    sorts = [
        n
        for n in _nodes(plan)
        if n["Node Type"] in ("Sort", "Incremental Sort")
        and any("iki_entity." in key for key in n.get("Sort Key", []))
    ]
    print(  # noqa: T201 - the load test's report
        f"every type's, last changed first, planned as {plan['Node Type']} "
        f"by {sorted(i for i in used if i)}; sorts: {len(sorts)}"
    )
    if rows > SMALL:
        # At a handful of rows, reading them all and sorting is as good.
        assert "iki_entity_changed" in used, plan
        assert not sorts, plan
    # Someone in a small project alone: their few records, newest first, though millions of
    # others changed since. Postgres finds theirs through their project, not by reading
    # everyone's newest first.
    small = Project.objects.create(team=world.other_team, name="Assay Development")
    ProjectMembership.objects.create(
        project=small, user=world.users["team_member"], role=ProjectRole.EDITOR
    )
    theirs = [
        cast(
            "Entity",
            entities.create(
                world.users["team_member"], TYPE, small, {"sequence": f"MKV{s}", "name": f"S{s}"}
            ).entity,
        ).hrid
        for s in ("A", "C", "D", "E", "F")
    ]
    Entity.objects.filter(hrid__in=theirs).update(updated_at=dt.datetime(2019, 1, 1, tzinfo=dt.UTC))
    outsider = Api(world.browser("team_member"))
    took["a small project's, last changed first"], answer = _timed(outsider, newest)
    assert sorted(e["hrid"] for e in answer["items"]) == sorted(theirs)
    for name in ("every type's, last changed first", "a small project's, last changed first"):
        print(f"{name}: {took[name] * 1000:.0f} ms")  # noqa: T201 - the load test's report
    slow = {name: f"{s:.2f}s" for name, s in took.items() if s > BUDGET}
    assert not slow, slow

"""The graph at size (ADR 0123): walks over 100,000 entities use the edges' indexes and
read only what they reach.

A lab's graph, made in bulk: 1,000 donors (the first 50 also stand for reagent lots),
10,000 samples from them, 40,000 aliquots of those and 49,000 results of those, each
result also naming a lot. A tenth is in a project the viewer can't read. Each walk must
read the edges by their indexes, never the whole table.

That's checked in what Postgres did, not in seconds: every query a walk makes is run
again under EXPLAIN ANALYZE, and the rows its recursion read from the edges and the
project links must grow with what the walk reached, not with the tables. A clock
measures the machine as much as the query, and other checks share both.
"""

# ruff: noqa: S608 - SQL made here, of the test's own literals

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import connection, models
from django.test.utils import CaptureQueriesContext
from org_world import World, build_world
from test_api_entities import api_as, create

from iki_server.entities import registry, search
from iki_server.entities.models import Entity, ProjectLink
from iki_server.graph import queries
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

SAMPLE = "acme/protein.Sample"
ROWS = 100_000
DONORS, SAMPLES, ALIQUOTS = 1_000, 11_000, 51_000
LOTS = 50
#: The tables a walk must read by their indexes: 100,000 rows each, and a lab's millions.
INDEXED = ("iki_graph_edge", "iki_entity_project")
#: Rows a walk may read from those tables for each node it reaches: an edge to it and its
#: project link, a step back to it when its path is drawn. A whole table is 100,000.
PER_NODE = 5


def _sql(statement: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-7000-8000-{n:012x}")


_ID = "('00000000-0000-7000-8000-' || lpad(to_hex(n), 12, '0'))::uuid"
_LINK = "('00000000-0000-7001-8000-' || lpad(to_hex(n), 12, '0'))::uuid"


def _columns(model: type[models.Model], key: str, own: dict[str, str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for field in model._meta.local_concrete_fields:
        if getattr(field, "generated", False):
            continue
        column = str(field.column)
        found[column] = _ID if column == key else own.get(column, f't."{column}"')
    return found


def fill(template: Entity, hidden: Project) -> None:
    """``ROWS`` copies of the sample, linked into its project, a tenth into ``hidden``; and
    the edges between them, as their references would make."""
    model = registry.model(SAMPLE)
    chain = [*reversed(model._meta.get_parent_list()), model]
    own = {"hrid": "'SMX-' || lpad(n::text, 6, '0')", "label": "'S-' || n"}
    _sql("SET LOCAL session_replication_role = replica")
    for each in chain:
        key = str(each._meta.pk.column)
        columns = _columns(each, key, own)
        names = ", ".join(f'"{c}"' for c in columns)
        _sql(
            f"INSERT INTO {each._meta.db_table} ({names}) "
            f"SELECT {', '.join(columns.values())} FROM {each._meta.db_table} t, "
            f'generate_series(1, %s) n WHERE t."{key}" = %s',
            [ROWS, template.pk],
        )
    link = _columns(ProjectLink, "id", {"id": _LINK, "entity_id": _ID})
    link["project_id"] = f"CASE WHEN n %% 10 = 0 THEN '{hidden.pk}'::uuid ELSE t.project_id END"
    names = ", ".join(f'"{c}"' for c in link)
    _sql(
        f"INSERT INTO iki_entity_project ({names}) "
        f"SELECT {', '.join(link.values())} FROM iki_entity_project t, "
        "generate_series(1, %s) n WHERE t.entity_id = %s",
        [ROWS, template.pk],
    )
    edge = "INSERT INTO iki_graph_edge (node_id, on_id, kind, field, source) "
    parent = (
        f"CASE WHEN n <= {SAMPLES} THEN 1 + n %% {DONORS} "
        f"WHEN n <= {ALIQUOTS} THEN {DONORS + 1} + n %% {SAMPLES - DONORS} "
        f"ELSE {SAMPLES + 1} + n %% {ALIQUOTS - SAMPLES} END"
    )
    _sql(
        edge + f"SELECT {_ID}, ('00000000-0000-7000-8000-' || lpad(to_hex({parent}), 12, '0'))"
        "::uuid, 'reference', 'parent', 'synthetic.parent' "
        f"FROM generate_series({DONORS + 1}, %s) n",
        [ROWS],
    )
    _sql(
        edge + f"SELECT {_ID}, ('00000000-0000-7000-8000-' || lpad(to_hex(1 + n %% {LOTS}), "
        "12, '0'))::uuid, 'reference', 'lot', 'synthetic.lot' "
        f"FROM generate_series({ALIQUOTS + 1}, %s) n",
        [ROWS],
    )
    _sql("SET LOCAL session_replication_role = DEFAULT")
    for table in ("iki_entity", "iki_entity_project", "iki_graph_edge", model._meta.db_table):
        _sql(f"ANALYZE {table}")


@pytest.fixture
def lab() -> tuple[World, Project]:
    world = build_world()
    hidden = Project.objects.create(team=world.team, name="Hidden")
    ProjectMembership.objects.create(
        project=hidden, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    made = create(api_as(world), world.project, {"label": "template"}, SAMPLE)
    assert made.status_code == 201, made.content
    fill(Entity.objects.get(pk=made.json()["id"]), hidden)
    return world, hidden


def _walking(plan: Any) -> Iterator[dict[str, Any]]:
    """Each node of the plan that walks: those under its recursive union, which steps from
    what's reached to what's next. What the query does with the walk's answer is the
    planner's to choose by size."""
    stack: list[tuple[Any, bool]] = [(plan, False)]
    while stack:
        node, walking = stack.pop()
        if isinstance(node, dict):
            walking = walking or node.get("Node Type") == "Recursive Union"
            if walking and "Node Type" in node:
                yield node
            stack.extend((v, walking) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, walking) for v in node)


#: How the walk's table is made: what makes it is a query of its own, to explain.
_MADE = "CREATE TEMPORARY TABLE iki_reach ON COMMIT DROP AS "


@contextmanager
def _reads() -> Iterator[dict[str, float]]:
    """Each (node type, table) of `INDEXED` that the block's walks read, and how many rows:
    what each found there and what it read past (removed by a filter), for each loop. The
    queries are run again under EXPLAIN ANALYZE once the block ends, in its transaction,
    which its temporary tables last for."""
    read: dict[str, float] = {}
    with CaptureQueriesContext(connection) as ran:
        yield read
    for query in ran.captured_queries:
        sql = query["sql"].removeprefix(_MADE)
        if not sql.startswith(("SELECT", "WITH")) or not any(t in sql for t in INDEXED):
            continue
        (plan,) = _sql(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")[0]
        for node in _walking(json.loads(plan) if isinstance(plan, str) else plan):
            if node.get("Relation Name") in INDEXED:
                rows = sum(
                    node.get(k, 0)
                    for k in (
                        "Actual Rows",
                        "Rows Removed by Filter",
                        "Rows Removed by Index Recheck",
                    )
                )
                key = f"{node['Node Type']} {node['Relation Name']}"
                read[key] = read.get(key, 0) + rows * node.get("Actual Loops", 1)


def _read_by_index(read: dict[str, float], reached: int) -> None:
    assert read, "no walk was run"
    assert not [k for k in read if k.startswith("Seq Scan")], read
    assert sum(read.values()) <= PER_NODE * (reached + 1), (reached, read)


def _walked(world: World, root: int, direction: str, **kw: Any) -> queries.Reach:
    """Walks as the graph's API does (ADR 0123): the walk, its count, its first page, what
    it couldn't show, whether there's more, and the paths to what it found."""
    user = world.users["viewer"]
    q = queries.Query(_id(root), direction, **kw)
    with search.time_limit(), _reads() as read:
        found = queries.reach(user, q)
        found.total(), found.page(None, 100), found.hidden(), found.beyond()
        found.paths([n for n, _, _ in found.page(None, 100)])
        ((reached,),) = _sql("SELECT count(*) FROM iki_reach")
    _read_by_index(read, reached)
    return found


def test_walks_over_100k_entities_read_the_indexes_and_only_what_they_reach(
    lab: tuple[World, Project],
) -> None:
    world, _ = lab
    # A donor's consent: its 10 samples, their aliquots, their results.
    found = _walked(world, 500, "downstream")
    assert 60 <= found.total() <= 110
    assert found.hidden() == 0
    # A recalled lot: about a thousand results, and nothing past them.
    found = _walked(world, 7, "downstream")
    assert 800 <= found.total() <= 1100
    assert found.beyond() is False
    # What a result depends on: an aliquot, a sample, a donor and a lot, unless hidden.
    found = _walked(world, 99_999, "upstream")
    assert 1 <= found.total() + found.hidden() <= 4
    # Everything a lot touched, as far as it goes.
    _walked(world, 3, "downstream", depth=30)


def test_a_search_across_100k_entities_reads_only_what_it_finds(
    lab: tuple[World, Project],
) -> None:
    world, _ = lab
    api = api_as(world, "viewer")
    # A donor the viewer reads, as all that comes of it (a tenth of each layer is hidden,
    # by its number's last digit: here none of it).
    where = {"field": "downstream", "op": "of", "value": str(_id(505))}
    body = {"type": SAMPLE, "where": where, "limit": 100}
    with _reads() as read:
        answer = api.post("/entities/search", body)
    assert answer.status_code == 200, answer.content[:300]
    found = len(answer.json()["items"])
    assert 40 <= found <= 100
    _read_by_index(read, found)

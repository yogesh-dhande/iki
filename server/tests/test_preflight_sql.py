"""Exact counts share a deployed, project-record scope and bound the examples per group."""

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import build_world
from psycopg import sql
from test_api_entities import api_as, create
from test_preflight_records import PROTEIN, SAMPLE, nodes

from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.plugins import preflight_sql
from iki_server.plugins.preflight_records import layout

pytestmark = pytest.mark.django_db


def test_scope_reads_parent_entry_and_extension_columns_without_counting_the_entry() -> None:
    world = build_world()
    response = create(
        api_as(world),
        world.project,
        {"sequence": "MKV", "name": "Reference", "hazard": "bsl1", "predicted_by": "model 2"},
        PROTEIN,
    )
    assert response.status_code == 201, response.content
    rec = response.json()
    scope = preflight_sql.Scope([PROTEIN], layout(PROTEIN, registry.plugins()))
    assert scope.population() == 1
    checks = [
        sql.SQL("{} = {}").format(scope.column(name), sql.Literal(value))
        for name, value in [
            ("sequence", "MKV"),
            ("name", "Reference"),
            ("hazard", "bsl1"),
            ("predicted_by", "model 2"),
        ]
    ]
    checks.append(sql.SQL("{} IS NULL").format(scope.column("new_field")))
    with CaptureQueriesContext(connection) as queries:
        assert scope.counts(checks) == [1] * len(checks)
    assert len(queries) == 1
    assert queries[0]["sql"].count("FILTER") == len(checks)
    assert scope.examples(checks[0]) == [f"{rec['hrid']} ({rec['id']})"]
    # Registry uniqueness includes the entry itself, even without a project record.
    entries = preflight_sql.Scope([PROTEIN], layout(PROTEIN, registry.plugins()), entries=True)
    assert entries.population() == 1
    with connection.cursor() as cursor:
        cursor.execute(entries.select(sql.SQL("e.id::text, {}").format(entries.column("hazard"))))
        assert cursor.fetchall() == [(rec["entry"]["id"], "bsl1")]


def test_matched_rows_are_counted_in_postgres_with_five_examples_per_group() -> None:
    world = build_world()
    Entity.objects.bulk_create(
        [
            Entity(type=kind, hrid=f"{prefix}-{i:06d}", owner_project=world.project)
            for kind, prefix in [(PROTEIN, "PRT"), (SAMPLE, "SMP")]
            for i in reversed(range(1, 553))
        ]
    )
    scope = preflight_sql.Scope([PROTEIN, SAMPLE], [])
    with CaptureQueriesContext(connection) as queries:
        groups = preflight_sql.summary(scope.select(scope.keys))
    assert len(queries) == 1
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + queries[0]["sql"])
        (plan,) = cursor.fetchone()
    if isinstance(plan, str):
        plan = json.loads(plan)
    steps = list(nodes(plan[0]["Plan"]))
    assert any(n["Node Type"] == "Aggregate" and n["Actual Rows"] == 2 for n in steps)
    assert any(n["Node Type"] == "WindowAgg" and n["Actual Rows"] == 1104 for n in steps)
    assert groups == {
        kind: (552, [f"{prefix}-{i:06d}" for i in range(1, 6)])
        for kind, prefix in [(PROTEIN, "PRT"), (SAMPLE, "SMP")]
    }
    assert scope.count_examples() == (1104, groups[PROTEIN][1])


def test_wide_schema_counts_more_predicates_than_postgres_target_columns() -> None:
    world = build_world()
    Entity.objects.bulk_create(
        [Entity(type=SAMPLE, hrid="SMP-001201", owner_project=world.project)]
    )
    scope = preflight_sql.Scope([SAMPLE], [])
    checks = [sql.SQL("e.hrid = {}").format(sql.Literal(f"SMP-{n:06d}")) for n in range(1_700)]
    with CaptureQueriesContext(connection) as queries:
        counts = scope.counts(checks)
    assert len(queries) == 1
    assert len(counts) == len(checks)
    assert counts[1201] == sum(counts) == 1

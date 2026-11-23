"""Primitive lists have typed PostgreSQL columns, so their impact counts are exact."""

import json
from dataclasses import replace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import build_world
from test_preflight_records import nodes

from iki_server.entities.models import Entity
from iki_server.plugins import preflight
from iki_spec.model import EntityType, Field, Plugin


@pytest.mark.django_db
def test_array_predicates_count_records_and_convert_before_testing_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = build_world().project
    fields = {
        "labels": Field.from_manifest(
            "labels", {"type": "list", "items": {"type": "string"}}, plugin="acme/meter"
        ),
        "mass": Field.from_manifest(
            "mass",
            {"type": "list", "items": {"type": "quantity", "unit": "mg"}},
            plugin="acme/meter",
        ),
    }
    sample = EntityType("acme/meter", "Sample", 1, fields=fields)
    lab = Plugin(sample.plugin, "1", types={sample.qualified_name: sample})
    records = Entity.objects.bulk_create(
        [
            Entity(type=sample.qualified_name, hrid=f"MTR-{i:06d}", owner_project=project)
            for i in range(4)
        ]
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE acme__meter_sample "
            "(entity_id uuid PRIMARY KEY, labels text[], mass float8[])"
        )
        cursor.executemany(
            "INSERT INTO acme__meter_sample VALUES (%s, %s, %s)",
            [
                (records[0].pk, ["kept"], [0.5]),
                (records[1].pk, [], []),
                (records[2].pk, ["gone", "gone"], [3, 4]),
                (records[3].pk, None, None),
            ],
        )
    new_fields = {
        "labels": replace(
            fields["labels"], min_items=1, items=Field("labels", "enum", values=("kept",))
        ),
        "mass": replace(fields["mass"], items=Field("mass", "quantity", unit="ug", max=1000)),
    }
    candidate = replace(
        lab, version="2", types={sample.qualified_name: replace(sample, fields=new_fields)}
    )
    monkeypatch.setattr(preflight, "SAMPLE_LIMIT", 1)
    with CaptureQueriesContext(connection) as queries:
        [report] = preflight.compare({lab.name: lab}, {lab.name: candidate}, "abc", candidate).types
    counts = [q["sql"] for q in queries if "FILTER" in q["sql"] and "unnest(" in q["sql"]]
    assert len(counts) == 1
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + counts[0])
        (measured,) = cursor.fetchone()
    if isinstance(measured, str):
        measured = json.loads(measured)
    plan = measured[0]["Plan"]
    assert plan["Node Type"] == "Aggregate" and plan["Actual Rows"] == 1
    assert any(node.get("Function Name") == "unnest" for node in nodes(plan))
    assert not report.sampled
    assert report.total == report.checked == 4
    findings = {(f.subject, f.kind): f for f in report.findings}
    assert findings["labels", "value"].examples == [records[1].hrid]
    assert findings["labels", "enum"].examples == [records[2].hrid]
    assert findings["mass", "unit"].count == 2
    assert findings["mass", "value"].examples == [records[2].hrid]
    assert all(not f.sampled for f in report.findings)


@pytest.mark.django_db
def test_array_defaults_wrong_types_and_patterns() -> None:
    project = build_world().project
    fields = {
        "tags": Field("tags", "list", items=Field("item", "string")),
        "mass": Field("mass", "list", items=Field("item", "quantity", unit="mg")),
        "note": Field("note", "string"),
    }
    sample = EntityType("acme/meter", "Sample", 1, fields=fields)
    lab = Plugin(sample.plugin, "1", types={sample.qualified_name: sample})
    records = Entity.objects.bulk_create(
        [
            Entity(type=sample.qualified_name, hrid=f"MTR-{i:06d}", owner_project=project)
            for i in range(3)
        ]
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE acme__meter_sample "
            "(entity_id uuid PRIMARY KEY, tags text[], mass float8[], note text)"
        )
        cursor.executemany(
            "INSERT INTO acme__meter_sample VALUES (%s, %s, %s, %s)",
            [
                (records[0].pk, ["ok", "not ok", "bad again"], [1e308], "scalar"),
                (records[1].pk, [], [], None),
                (records[2].pk, None, None, None),
            ],
        )
    after = {
        "tags": replace(fields["tags"], items=Field("item", "string", pattern="[a-z]+")),
        "mass": replace(
            fields["mass"],
            required=True,
            has_default=True,
            default=[2],
            items=Field("item", "quantity", unit="ug", max=1000),
        ),
        "note": Field("note", "list", items=Field("item", "int")),
        "added": Field("added", "list", items=Field("item", "int"), has_default=True, default=[]),
    }
    candidate = replace(
        lab, version="2", types={sample.qualified_name: replace(sample, fields=after)}
    )
    [report] = preflight.compare({lab.name: lab}, {lab.name: candidate}, "abc", candidate).types
    findings = {(f.subject, f.kind): f for f in report.findings}
    assert not report.sampled
    assert findings["tags", "pattern"].examples == [records[0].hrid]
    assert findings["mass", "backfill"].examples == [records[2].hrid]
    assert findings["mass", "unit"].count == 2
    assert findings["mass", "value"].examples == [records[0].hrid, records[2].hrid]
    assert findings["note", "value"].examples == [records[0].hrid]
    assert findings["added", "backfill"].count == 3

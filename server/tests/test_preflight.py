"""Promotion checks inheritance, existing data and the revision supplying its schema."""

from dataclasses import replace

import pytest
from django.db import connection
from org_world import build_world

from iki_server.entities.models import Entity
from iki_server.plugins.preflight_types import Report
from iki_spec.model import EntityType, Extension, Field, Plugin, Rule, unique_rules


def uniquely(t: EntityType, name: str) -> EntityType:
    """``t`` with its field ``name`` marked unique, and the rule that makes it (ADR 0082)."""
    f = replace(t.fields[name], unique=True)
    made = unique_rules({name: f}, t.plugin, identity=t.identity)
    return replace(t, fields={**t.fields, name: f}, rules={**t.rules, **made})


def no_sample(*args: object, **kwargs: object) -> None:
    pytest.fail("An exact SQL check fetched a Python sample")


@pytest.mark.django_db
def test_parent_unique_checks_records_of_sibling_types(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.plugins import preflight

    name = "acme/lab"
    batch = Field("batch", "string")
    parent = EntityType(name, "Material", 1, abstract=True, fields={"batch": batch})
    stock = EntityType(name, "Stock", 1, extends=parent.qualified_name)
    item = EntityType("acme/stock", "Item", 1, extends=parent.qualified_name)
    old = Plugin(name, "1.0", types={t.qualified_name: t for t in (parent, stock)})
    sibling = Plugin(item.plugin, "1.0", types={item.qualified_name: item})
    current = {name: old, sibling.name: sibling}
    candidate = replace(
        old,
        version="2.0",
        types={
            **old.types,
            parent.qualified_name: uniquely(parent, "batch"),
        },
    )
    project = build_world().project
    entities = Entity.objects.bulk_create(
        [
            Entity(type=t.qualified_name, hrid=f"MAT-{i:06d}", owner_project=project)
            for i, t in enumerate((stock, item), 1)
        ]
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE acme__lab_material (entity_id uuid PRIMARY KEY, batch text)"
        )
        cursor.executemany(
            "INSERT INTO acme__lab_material VALUES (%s, 'B-1')", [(e.pk,) for e in entities]
        )
    monkeypatch.setattr(preflight, "records", no_sample)
    report = preflight.compare(current, {**current, name: candidate}, "lock-before", candidate)
    assert report.blocking
    assert report.configuration_revision == "lock-before"
    assert len(report.candidate_revision) == 64
    assert {t.type for t in report.types} == {stock.qualified_name, item.qualified_name}
    for result in report.types:
        assert result.total == result.checked == 1
        assert not result.sampled
        expected = next(e.hrid for e in entities if e.type == result.type)
        assert [(f.count, f.examples, f.sampled) for f in result.findings] == [
            (1, [expected], False)
        ]


@pytest.mark.django_db
def test_new_extension_counts_all_backfills_and_only_cel_is_sampled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iki_server.plugins import preflight

    sample = EntityType("acme/lab", "Sample", 1)
    lab = Plugin(sample.plugin, "1", types={sample.qualified_name: sample})
    extension = Extension(
        "acme/qc", sample.qualified_name, {"method": Field("method", "string", required=True)}
    )
    qc = Plugin(extension.plugin, "1", extensions=(extension,))
    project = build_world().project
    Entity.objects.bulk_create(
        [
            Entity(type=sample.qualified_name, hrid=f"SMP-{i:06d}", owner_project=project)
            for i in range(2)
        ]
    )
    monkeypatch.setattr(preflight, "SAMPLE_LIMIT", 1)

    def check(plugin: Plugin) -> Report:
        return preflight.compare({lab.name: lab}, {lab.name: lab, qc.name: plugin}, "abc", plugin)

    report = check(qc)
    assert report.blocking and not report.types[0].sampled
    assert report.types[0].total == report.types[0].checked == 2
    assert [(f.subject, f.count, f.sampled) for f in report.types[0].findings] == [
        ("method", 2, False)
    ]
    rule = Rule(
        "labelled", "Labelled sample", qc.name, check='record.hrid == "no-match"', extension=True
    )
    with_rule = replace(qc, extensions=(replace(extension, rules={rule.name: rule}),))
    mixed = check(with_rule).types[0]
    assert mixed.sampled and mixed.total == 2 and mixed.checked == 1
    assert any(rule.key in label for label in mixed.sampled_checks)
    assert {(f.subject, f.count, f.sampled) for f in mixed.findings} == {
        ("method", 2, False),
        (rule.key, 1, True),
    }

    optional = replace(
        qc, extensions=(replace(extension, fields={"method": Field("method", "string")}),)
    )
    clear = check(optional)
    assert not clear.blocking and not clear.types[0].sampled_checks
    assert clear.candidate_revision != report.candidate_revision


def test_same_schema_does_not_read_any_records(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.plugins import preflight

    sample = EntityType("acme/lab", "Sample", 1)
    lab = Plugin(sample.plugin, "1", types={sample.qualified_name: sample})
    update = replace(lab, version="2")

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("unchanged schema was scanned")

    monkeypatch.setattr(preflight, "records", unexpected)
    report = preflight.compare({lab.name: lab}, {lab.name: update}, "revision", update)
    assert not report.blocking
    assert not report.types


@pytest.mark.django_db(transaction=True)
def test_preflight_uses_postgres_read_only_repeatable_read() -> None:
    from django.db import connection

    from iki_server.plugins import preflight

    with preflight.read_snapshot(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('transaction_read_only'), "
            "current_setting('transaction_isolation')"
        )
        assert cursor.fetchone() == ("on", "repeatable read")
    with connection.cursor() as cursor:
        cursor.execute("SHOW transaction_read_only")
        assert cursor.fetchone() == ("off",)

"""Promotion checks inheritance, existing data and the revision supplying its schema."""

from dataclasses import replace

import pytest

from iki_spec.model import EntityType, Extension, Field, Plugin


def test_parent_unique_checks_records_of_sibling_types(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.plugins import preflight
    from iki_server.plugins.preflight_types import Record

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
            parent.qualified_name: replace(parent, fields={"batch": replace(batch, unique=True)}),
        },
    )

    clipped: set[str] = set()

    def read(qualified: str, *args: object, **kwargs: object) -> tuple[list[Record], bool]:
        return [
            Record(qualified, qualified, "project", None, None, {"batch": "B-1"})
        ], qualified in clipped

    monkeypatch.setattr(preflight, "records", read)
    report = preflight.compare(current, {**current, name: candidate}, "lock-before", candidate)
    assert report.blocking
    assert report.configuration_revision == "lock-before"
    assert len(report.candidate_revision) == 64
    assert {t.type for t in report.types} == {stock.qualified_name, item.qualified_name}
    for result in report.types:
        assert result.checked == 1
        assert not result.sampled
        assert [(f.count, f.examples) for f in result.findings] == [(1, [result.type])]

    # A sibling outside the sample could collide even with a fully enumerated type.
    clipped.add(stock.qualified_name)
    report = preflight.compare(current, {**current, name: candidate}, "lock-before", candidate)
    assert all(t.sampled for t in report.types)


def test_new_extension_requires_backfill_and_partial_coverage_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iki_server.plugins import preflight
    from iki_server.plugins.preflight_types import Record

    sample = EntityType("acme/lab", "Sample", 1)
    lab = Plugin(sample.plugin, "1", types={sample.qualified_name: sample})
    extension = Extension(
        "acme/qc", sample.qualified_name, {"method": Field("method", "string", required=True)}
    )
    qc = Plugin(extension.plugin, "1", extensions=(extension,))
    sample_row = Record("id", "SMP-1", "p", None, None, {})
    monkeypatch.setattr(preflight, "records", lambda *a, **kw: ([sample_row], True))
    report = preflight.compare({lab.name: lab}, {lab.name: lab, qc.name: qc}, "abc", qc)
    assert report.blocking
    assert report.types[0].sampled
    assert any(f.subject == "method" and f.count == 1 for f in report.types[0].findings)

    optional = replace(
        qc, extensions=(replace(extension, fields={"method": Field("method", "string")}),)
    )
    report = preflight.compare({lab.name: lab}, {lab.name: lab, qc.name: optional}, "abc", optional)
    assert report.blocking == any(t.sampled for t in report.types)
    assert (
        report.candidate_revision
        != preflight.compare(
            {lab.name: lab}, {lab.name: lab, qc.name: qc}, "abc", qc
        ).candidate_revision
    )


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

"""Exact promotion counts, measured on 200,000 project records (ADR 0181)."""

import json
import time
from dataclasses import replace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import build_world
from test_preflight_records import nodes

from iki_server.entities import registry
from iki_server.plugins import preflight
from iki_server.plugins.preflight_types import Report
from iki_spec.model import EntityType, Field, Rule

pytestmark = pytest.mark.django_db

SAMPLE = "acme/protein.Sample"
ROWS = 200_000


def fill_samples() -> None:
    """Bulk-load real generated Sample tables with sparse failures beyond the first 500 records."""
    project = build_world().project
    with connection.cursor() as cur:
        # As with the graph plan fixture, omit the unrelated audit/graph writes while seeding.
        cur.execute("SET LOCAL session_replication_role = replica")
        cur.execute(
            """
            INSERT INTO iki_entity (id, type, hrid, owner_project_id, revision)
            SELECT md5(n::text)::uuid, %s, 'SMP-' || lpad(n::text, 6, '0'), %s, 1
            FROM generate_series(1, %s) AS n
            """,
            [SAMPLE, project.pk, ROWS],
        )
        cur.execute(
            """
            INSERT INTO acme__protein_sample
                (entity_id, label, notes, purity, concentration, custody, due_on)
            SELECT md5(n::text)::uuid, 'Lot ' || n,
                CASE WHEN n > 199990 THEN NULL WHEN n > 199980 THEN 'legacy' ELSE 'confirmed' END,
                CASE WHEN n > 199993 THEN 70 ELSE 98 END,
                2,
                CASE WHEN n > 199997 THEN 'disposed' ELSE 'in_lab' END,
                CASE WHEN n < 8000 THEN DATE '2000-01-01' + n
                     WHEN n > 199998 THEN DATE '2026-11-01' END
            FROM generate_series(1, %s) AS n
            """,
            [ROWS],
        )
        cur.execute("SET LOCAL session_replication_role = DEFAULT")
        cur.execute("ANALYZE iki_entity, acme__protein_sample")


def test_exact_preflight_counts_and_plans_at_200k_records() -> None:
    fill_samples()
    current = registry.plugins()
    plugin = current["acme/protein"]
    sample = plugin.types[SAMPLE]

    def report_for(t: EntityType) -> Report:
        candidate = replace(plugin, version="2.0.0", types={**plugin.types, SAMPLE: t})
        with preflight.read_snapshot():
            return preflight.compare(
                current, {**current, plugin.name: candidate}, "live", candidate
            )

    fields = sample.fields
    converted = replace(fields["concentration"], unit="ug/mL")
    clean = report_for(replace(sample, fields={**fields, "concentration": converted}))
    assert not clean.blocking, "an exact, compatible unit change needs no acknowledgement"
    (row,) = clean.types
    assert row.checked == ROWS and not row.sampled
    assert [(f.kind, f.count, f.blocking) for f in row.findings] == [("unit", ROWS, False)]

    custody = fields["custody"]
    slot = Rule(
        "custody_slot",
        "One sample per custody slot",
        plugin.name,
        unique=("due_on", "custody"),
        reads=("due_on", "custody"),
    )
    changed = replace(
        sample,
        rules={**sample.rules, slot.name: slot},
        fields={
            **fields,
            "method": Field("method", "string", required=True),
            "notes": Field("notes", "enum", values=("confirmed",), required=True),
            "purity": replace(fields["purity"], min=90),
            "concentration": converted,
            "custody": replace(
                custody,
                states={
                    **custody.states,
                    "disposed": replace(
                        custody.states["disposed"], removed=True, moved_to="in_lab"
                    ),
                },
            ),
        },
    )
    started = time.monotonic()
    with CaptureQueriesContext(connection) as queries:
        report = report_for(changed)
    elapsed = time.monotonic() - started
    assert report.blocking
    (row,) = report.types
    assert row.checked == ROWS and not row.sampled
    findings = {(f.kind, f.subject): f for f in row.findings}
    assert {k: f.count for k, f in findings.items()} == {
        ("backfill", "method"): ROWS,
        ("backfill", "notes"): 10,
        ("enum", "notes"): 10,
        ("value", "purity"): 7,
        ("unit", "concentration"): ROWS,
        ("unique", "custody_slot"): 2,
        ("state", "custody"): 3,
    }
    assert all(0 < len(f.examples) <= 5 for f in findings.values())
    assert set(findings["unique", "custody_slot"].examples) == {"SMP-199999", "SMP-200000"}

    # Explain actual preflight statements. PostgreSQL can scan a whole type for an
    # exact count, but it must aggregate in the database and return bounded results.
    # Limit table-row visits rather than wall time on shared CI machines.
    selects = [
        q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(selects) <= 30
    print(f"Exact preflight over {ROWS:,} records: {len(selects)} selects in {elapsed:.2f}s")  # noqa: T201
    counted = next(s for s in selects if "FILTER" in s.upper() and "purity" in s.lower())
    grouped = next(s for s in selects if "HAVING" in s.upper())
    with connection.cursor() as cur:
        for label, statement in (("field counts", counted), ("duplicate groups", grouped)):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + statement)
            (measured,) = cur.fetchone()
            if isinstance(measured, str):
                measured = json.loads(measured)
            plan = measured[0]["Plan"]
            operators = list(nodes(plan))
            assert any(n["Node Type"] == "Aggregate" for n in operators), plan
            assert plan["Actual Rows"] <= 5, plan
            table_visits = sum(
                n["Actual Loops"] * (n["Actual Rows"] + n.get("Rows Removed by Filter", 0))
                for n in operators
                if "Relation Name" in n
            )
            assert 0 < table_visits <= ROWS * 12, plan
            # With pytest -s the report shows the measured work and operators on this machine.
            names = ", ".join(n["Node Type"] for n in operators)
            print(  # noqa: T201
                f"{label}: {measured[0]['Execution Time']:.0f} ms, "
                f"{table_visits:,.0f} table-row visits ({names})"
            )

    # An independent CEL rule still needs sampling. Its lower bound and its sample
    # size must not overwrite the exact number of records lacking a method.
    check = Rule("retest", "Retest", plugin.name, check="record.purity < 90.0", reads=("purity",))
    mixed = report_for(
        replace(
            sample, fields={**fields, "method": changed.fields["method"]}, rules={"retest": check}
        )
    )
    (row,) = mixed.types
    assert row.total == ROWS and row.checked == 500 and row.sampled
    assert row.sampled_checks and any("retest" in label for label in row.sampled_checks)
    assert {(f.kind, f.count, f.sampled) for f in row.findings} == {
        ("backfill", ROWS, False),
        ("rule", 500, True),
    }

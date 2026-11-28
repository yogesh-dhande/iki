"""Exact preflight counts for the rows that participate in a database constraint."""

import json
from dataclasses import replace
from typing import Any

import pytest
from django.db import connection
from django.db.models.functions import Now
from django.test.utils import CaptureQueriesContext
from org_world import World
from psycopg import sql
from test_preflight import uniquely
from test_preflight_records import nodes
from test_registries import PROTEIN, api_as, create, rival
from test_rules import BATCH, made, world

from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Registry
from iki_server.plugins import preflight
from iki_server.plugins.preflight_types import Finding
from iki_spec.model import EntityType, Field, Plugin, Rule, State, StateMove

__all__ = ["rival", "world"]
pytestmark = pytest.mark.django_db


def findings(proposed: EntityType) -> dict[str, Finding]:
    plugins = registry.plugins()
    plugin = plugins[proposed.plugin]
    candidate = replace(plugin, types={**plugin.types, proposed.qualified_name: proposed})
    report = preflight.compare(plugins, {**plugins, plugin.name: candidate}, "revision", candidate)
    checked = next(t for t in report.types if t.type == proposed.qualified_name)
    return {f.subject: f for f in checked.findings}


def test_compound_unique_counts_all_conflicting_records_under_its_while_clause(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "SAMPLE_LIMIT", 1)
    old = registry.types()[BATCH]
    extra = Rule(
        "one_plate",
        "One stored batch per plate and status",
        old.plugin,
        unique=("plate", "status"),
        only_while=(("stored", ("true",)),),
        reads=("plate", "status", "stored"),
    )
    first = made(world, {"plate": "P1", "well": "A1"})
    second = made(world, {"plate": "P1", "well": "A2"})
    made(world, {"plate": "P1", "well": "A1", "stored": False})
    made(world, {})
    with CaptureQueriesContext(connection) as queries:
        checked = findings(replace(old, rules={**old.rules, extra.name: extra}))
    assert checked[extra.name].count == 2
    assert sorted(checked[extra.name].examples) == sorted([first["hrid"], second["hrid"]])
    query = next(q["sql"] for q in queries if "HAVING" in q["sql"])
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query)
        plan = cursor.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    assert any(
        n.get("Node Type") == "Aggregate" and "count(*) > 1" in n.get("Filter", "")
        for n in nodes(plan[0]["Plan"])
    )


def test_date_overlap_counts_nested_and_touching_records_once_within_the_project(
    world: World,
    rival: Project,
) -> None:
    old = registry.types()[BATCH]
    date_range = Rule(
        "made_ranges",
        "Production dates",
        old.plugin,
        no_overlap=("made_on", "expires_on"),
        per=("plate",),
        reads=("made_on", "expires_on", "plate"),
    )

    def dates(lo: str, hi: str) -> dict[str, str]:
        return {"made_on": f"2026-03-{lo}", "expires_on": f"2026-03-{hi}", "plate": "P"}

    first = made(world, dates("01", "30"))
    inside = made(world, dates("03", "04"))
    last = made(world, dates("30", "31"))
    other = create(api_as(world, "outsider"), rival, {"name": "L-1", **dates("30", "31")}, BATCH)
    assert other.status_code == 201, other.content
    proposed = uniquely(old, "name")
    proposed = replace(proposed, rules={**proposed.rules, date_range.name: date_range})
    with CaptureQueriesContext(connection) as queries:
        checked = findings(proposed)
    expected = sorted(row["hrid"] for row in (first, inside, last))
    for key in ("name", date_range.name):
        assert checked[key].count == 3
        assert sorted(checked[key].examples) == expected
    sweep = next(q["sql"] for q in queries if "LEAD(" in q["sql"].upper())
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + sweep)
        plan = cursor.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    windows = [n for n in nodes(plan[0]["Plan"]) if n["Node Type"] == "WindowAgg"]
    for function in ("lead(", "max("):
        assert any(function in output for n in windows for output in n.get("Output", []))


def test_registry_counts_distinct_entries_then_reports_every_holder(
    world: World, rival: Project
) -> None:
    old = registry.types()[PROTEIN]
    proposed = uniquely(old, "ec_number")
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")

    def register(
        sequence: str, project: Project = world.project, other: bool = False
    ) -> dict[str, Any]:
        response = create(
            outsider if other else editor,
            project,
            {"sequence": sequence, "ec_number": "1.1.1.1"},
        )
        assert response.status_code == 201, response.content
        data: dict[str, Any] = response.json()
        return data

    first = register("MKV")
    holder = register("MKV", rival, other=True)
    assert first["entry"]["id"] == holder["entry"]["id"]
    assert "ec_number" not in findings(proposed)
    second = register("MKVL")
    separate = Registry.objects.create(name="Private registry", all_teams=True)
    project = Project.objects.create(team=world.team, name="Separate scope", registry=separate)
    ProjectMembership.objects.create(
        project=project, role=ProjectRole.EDITOR, user=world.users["editor"]
    )
    register("MKVLA", project)
    finding = findings(proposed)["ec_number"]
    assert finding.count == 3
    assert sorted(finding.examples) == sorted(
        f"{r['hrid']} ({r['id']})" for r in (first, holder, second)
    )
    # The registry keeps an entry when no project currently holds it. Its value
    # still conflicts with the entry held by both projects.
    Entity.objects.filter(pk=second["id"]).update(purged_at=Now())
    finding = findings(proposed)["ec_number"]
    assert finding.count == 2
    assert sorted(finding.examples) == sorted(f"{r['hrid']} ({r['id']})" for r in (first, holder))


def test_datetime_overlap_uses_instants_and_skips_empty_and_reversed_ranges(world: World) -> None:
    old = registry.types()[BATCH]
    rule = Rule(
        "times",
        "Across plates",
        old.plugin,
        no_overlap=("starts_at", "ends_at"),
        reads=("starts_at", "ends_at"),
    )

    def row(plate: str, lo: str, hi: str) -> dict[str, Any]:
        prefix = "2026-03-12T04:00:"
        return made(
            world, {"plate": plate, "starts_at": prefix + lo + "Z", "ends_at": prefix + hi + "Z"}
        )

    first = row("A", "00", "00.5")
    second = row("B", "00.1", "00.6")
    row("adjacent", "00.6", "01")
    row("empty", "00.2", "00.2")
    row("reversed", "00.5", "00.12")
    checked = findings(replace(old, rules={**old.rules, rule.name: rule}))
    assert checked[rule.name].count == 2
    assert sorted(checked[rule.name].examples) == sorted([first["hrid"], second["hrid"]])


def test_compound_datetime_keys_use_the_same_instant(world: World) -> None:
    old = registry.types()[BATCH]
    rule = Rule(
        "time_per_name",
        "Unique start per name",
        old.plugin,
        unique=("starts_at", "name"),
        reads=("starts_at", "name"),
    )
    first = made(world, {"starts_at": "2026-03-01T01:00:00+01:00"})
    second = made(world, {"starts_at": "2026-03-01T00:00:00Z"})
    checked = findings(replace(old, rules={**old.rules, rule.name: rule}))
    assert checked[rule.name].count == 2
    assert sorted(checked[rule.name].examples) == sorted([first["hrid"], second["hrid"]])


def test_unrepresentable_range_fields_are_counted(world: World) -> None:
    old = registry.types()[BATCH]
    bad = made(world, {"plate": "unknown", "well": "bad"})
    made(world, {"plate": "unknown", "well": "bad", "stored": False})
    rule = Rule(
        "future_span",
        "Proposed numeric range",
        old.plugin,
        no_overlap=("plate", "well"),
        only_while=(("stored", ("true",)),),
        reads=("plate", "well", "stored"),
    )
    checked = findings(
        replace(
            old,
            fields={
                **old.fields,
                "plate": replace(old.fields["plate"], type="int"),
                "well": replace(old.fields["well"], type="int"),
            },
            rules={rule.name: rule},
        )
    )
    issue = checked[rule.name]
    assert issue.kind == "unchecked" and issue.count == 1 and issue.examples == [bad["hrid"]]


@pytest.mark.parametrize("move_declaration", [False, True], ids=["changed-sibling", "wider-scope"])
def test_inherited_constraint_reports_all_conflicting_siblings(
    world: World, move_declaration: bool
) -> None:
    owner = "acme/materials"
    phase = Field(
        "phase", "state", states={n: State(n, n) for n in ("raw", "ready")}, initial="raw"
    )
    fields = {"phase": phase, "batch": Field("batch", "string")}
    rule = Rule("one_batch", "One per phase", owner, unique=("phase", "batch"))
    rules = {rule.key: rule}
    parent = EntityType(owner, "Item", 1, abstract=True)
    stock = EntityType(owner, "Stock", 1, extends=parent.qualified_name)
    spare = replace(stock, name="Spare")
    with connection.cursor() as cursor:
        if move_declaration:
            # Each child's own project constraint previously admitted the same key once.
            stock = replace(stock, fields=fields, rules=rules)
            spare = replace(spare, fields=fields, rules=rules)
            current = (parent, stock, spare)
            future = (
                replace(parent, fields=fields, rules=rules),
                replace(stock, fields={}, rules={}),
                replace(spare, fields={}, rules={}),
            )
        else:
            # Only Stock's declaration changes. Its retirement changes the value of
            # the inherited phase column and conflicts with an untouched Spare.
            parent = replace(parent, fields=fields, rules=rules)
            former = replace(phase, name="former")
            stock = replace(stock, fields={"former": former})
            current = (parent, stock, spare)
            moved = replace(former, removed=True, moved_to=StateMove("phase", (("raw", "ready"),)))
            future = (parent, replace(stock, fields={"former": moved}), spare)
            cursor.execute(
                sql.SQL("CREATE TEMP TABLE {} (entity_id uuid PRIMARY KEY, former text)").format(
                    sql.Identifier(stock.table)
                )
            )
        tables = (stock.table, spare.table) if move_declaration else (parent.table,)
        for table in tables:
            cursor.execute(
                sql.SQL(
                    "CREATE TEMP TABLE {} (entity_id uuid PRIMARY KEY, phase text, batch text)"
                ).format(sql.Identifier(table))
            )
        for subtype in (stock, spare):
            entity = Entity.objects.create(
                type=subtype.qualified_name,
                hrid=f"{subtype.name.upper()}-000001",
                owner_project=world.project,
            )
            phase_value = "ready" if move_declaration or subtype == spare else "raw"
            cursor.execute(
                sql.SQL("INSERT INTO {} VALUES (%s, %s, 'lot')").format(
                    sql.Identifier(subtype.table if move_declaration else parent.table)
                ),
                [entity.pk, phase_value],
            )
            if subtype == stock and not move_declaration:
                cursor.execute(
                    sql.SQL("INSERT INTO {} VALUES (%s, 'raw')").format(
                        sql.Identifier(stock.table)
                    ),
                    [entity.pk],
                )

    old = Plugin(owner, "1.0", types={t.qualified_name: t for t in current})
    new = Plugin(owner, "2.0", types={t.qualified_name: t for t in future})
    report = preflight.compare({owner: old}, {owner: new}, "revision", new)
    assert {
        t.type: (f.count, f.examples)
        for t in report.types
        for f in t.findings
        if f.subject == rule.name
    } == {t.qualified_name: (1, [f"{t.name.upper()}-000001"]) for t in (stock, spare)}

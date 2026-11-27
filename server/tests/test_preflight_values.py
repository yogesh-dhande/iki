"""A proposed plugin's effects on the current values of records (ADR 0181)."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace

import pytest
from org_world import World
from test_api_entities import api_as, create
from test_runs import world

from iki_server.entities import registry
from iki_server.plugins.preflight_types import Record
from iki_server.plugins.preflight_values import field_findings
from iki_spec.model import Field

__all__ = ["world"]

SAMPLE = "acme/protein.Sample"


@pytest.mark.django_db
def test_sample_values_are_checked_in_the_units_the_migration_will_store(world: World) -> None:
    current = registry.types()[SAMPLE].fields
    proposed = {
        **current,
        "concentration": replace(current["concentration"], unit="ug/mL", max=1200),
        "notes": replace(current["notes"], required=True),
        "grade": Field("grade", "enum", values=("A", "B"), required=True),
        "origin": Field("origin", "string", required=True, has_default=True, default="lab"),
        "tags": Field("tags", "list", items=Field("", "enum", values=("fresh",))),
    }
    rows: list[Record] = []
    for label, concentration, tags in [("sample-A", 1.0, ["fresh"]), ("sample-B", 2.0, ["old"])]:
        values = {"label": label, "concentration": concentration, "tags": tags}
        result = create(api_as(world), world.project, values, SAMPLE)
        assert result.status_code == 201, result.content
        record = result.json()
        rows.append(
            Record(
                record["id"], record["hrid"], str(world.project.pk), None, None, record["values"]
            )
        )
    report, projected = field_findings(current, proposed, rows)
    assert projected[0].values["concentration"] == pytest.approx(1000)
    assert projected[0].values["origin"] == "lab"
    assert rows[0].values["concentration"] == 1.0
    found = {(item.kind, item.subject): item for item in report}
    assert found["backfill", "grade"].count == found["backfill", "notes"].count == 2
    assert found["backfill", "grade"].blocking
    assert not found["backfill", "origin"].blocking
    unit = found["unit", "concentration"]
    assert unit.count == 2 and not unit.blocking and "1000" in unit.message
    assert found["value", "concentration"].examples == [rows[1].hrid]
    assert found["enum", "tags"].examples == [rows[1].hrid]


def test_removed_states_report_each_occupied_state_and_its_destination() -> None:
    status = Field.from_manifest(
        "status",
        {"type": "state", "states": {"received": {}, "released": {}, "retained": {}}},
        plugin="acme/lab",
    )
    changed = replace(
        status,
        states={
            "received": replace(status.states["received"], removed=True, moved_to="retained"),
            "retained": status.states["retained"],
        },
    )
    rows = [
        Record(str(n), f"record-{n}", "project", None, None, {"status": v})
        for n, v in enumerate(["received", "released", "retained"])
    ]
    report, projected = field_findings({"status": status}, {"status": changed}, rows)
    assert projected[0].values["status"] == "retained"
    assert len(report) == 2
    mapped, stranded = report
    assert not mapped.blocking and "retained" in mapped.message and mapped.examples == ["record-0"]
    assert (
        stranded.blocking and "released" in stranded.message and stranded.examples == ["record-1"]
    )


def test_incompatible_units_and_nonfinite_conversions_need_attention() -> None:
    field = Field("mass", "quantity", unit="g")
    rows = [
        Record(name, name, "project", None, None, {"mass": v})
        for name, v in [("full", 1.5e308), ("empty", None)]
    ]
    incompatible, _ = field_findings({"mass": field}, {"mass": replace(field, unit="s")}, rows)
    assert len(incompatible) == 1
    assert incompatible[0].blocking and incompatible[0].examples == ["full"]
    overflowing, _ = field_findings({"mass": field}, {"mass": replace(field, unit="mg")}, rows)
    assert any(item.blocking and item.examples == ["full"] for item in overflowing)
    small = [
        Record(name, name, "project", None, None, {"mass": v})
        for name, v in [("zero", 0.0), ("tiny", float.fromhex("0x0.0000000000001p-1022"))]
    ]
    underflowing, projected = field_findings(
        {"mass": field}, {"mass": replace(field, unit="kg")}, small
    )
    assert [r.values["mass"] for r in projected] == [0, None]
    assert any(item.blocking and item.examples == ["tiny"] for item in underflowing)


def test_retiring_a_whole_state_field_projects_into_the_destination_after_its_default() -> None:
    status = Field.from_manifest(
        "status",
        {"type": "state", "initial": "ready", "states": {"ready": {}, "review": {}}},
        plugin="acme/lab",
    )
    retired = Field.from_manifest(
        "status",
        {
            "type": "state",
            "removed": True,
            "moved_to": {"field": "review", "states": {"ready": "done"}},
        },
        plugin="acme/lab",
    )
    review = Field.from_manifest(
        "review",
        {"type": "state", "initial": "pending", "states": {"pending": {}, "done": {}}},
        plugin="acme/lab",
    )
    rows = [Record("1", "A-1", "project", None, None, {"status": "ready"})]
    findings, projected = field_findings(
        {"status": status}, {"status": retired, "review": review}, rows
    )
    assert projected[0].values == {"review": "done"}
    assert rows[0].values == {"status": "ready"}
    assert [(f.kind, f.blocking) for f in findings] == [("backfill", False), ("state", False)]


def test_a_backfill_and_a_unit_change_inside_list_rows_count_the_owning_records() -> None:
    def additions(unit: str, extra: dict[str, object]) -> Field:
        return Field.from_manifest(
            "additions",
            {
                "type": "list",
                "items": {
                    "type": "row",
                    "fields": {"amount": {"type": "quantity", "unit": unit}, **extra},
                },
            },
            plugin="acme/lab",
        )

    old = additions("mg", {})
    new = additions("ug", {"lot": {"type": "string", "required": True}})
    original = [{"amount": 0.25}, {"amount": 0.75}]
    rows = [Record("1", "A-1", "project", None, None, {"additions": original})]
    findings, projected = field_findings({"additions": old}, {"additions": new}, rows)
    assert [(f.subject, f.count, f.examples) for f in findings] == [
        ("additions.amount", 1, ["A-1"]),
        ("additions.lot", 1, ["A-1"]),
    ]
    assert projected[0].values["additions"] == [{"amount": 250}, {"amount": 750}]
    assert rows[0].values["additions"] == [{"amount": 0.25}, {"amount": 0.75}]


@pytest.mark.django_db
def test_a_changed_pattern_checks_the_sample_in_one_parameterized_query(
    django_assert_num_queries: Callable[[int], AbstractContextManager[object]],
) -> None:
    old = Field("label", "string")
    new = replace(old, pattern="LOT-[0-9]+")
    rows = [
        Record("1", "A-1", "project", None, None, {"label": "LOT-123"}),
        Record("2", "A-2", "project", None, None, {"label": "X-LOT-123"}),
    ]
    with django_assert_num_queries(1):
        findings, _ = field_findings({"label": old}, {"label": new}, rows)
    assert [(f.kind, f.count, f.examples) for f in findings] == [("pattern", 1, ["A-2"])]


def test_changing_a_state_field_to_plain_text_needs_a_new_field() -> None:
    status = Field.from_manifest(
        "status", {"type": "state", "states": {"ready": {}}}, plugin="acme/lab"
    )
    text = Field("status", "string")
    rows = [Record("1", "A-1", "p", None, None, {"status": "ready"})]
    findings, _ = field_findings({"status": status}, {"status": text}, rows)
    assert [(f.kind, f.blocking, f.examples) for f in findings] == [("state", True, ["A-1"])]


@pytest.mark.django_db
def test_an_inherited_amount_unit_change_needs_a_migration_for_records_with_ledgers(
    world: World,
) -> None:
    from test_amounts import sample

    from iki_server.amounts.models import Amount as Balance
    from iki_server.plugins.preflight_values import amount_findings
    from iki_spec.effective import resolve_one
    from iki_spec.model import Amount, EntityType

    stock = EntityType("acme/lab", "Stock", 1, amount=Amount("uL"))
    batch = EntityType("acme/lab", "Batch", 1, extends=stock.qualified_name)

    def unit(*types: EntityType) -> str | None:
        found = resolve_one({t.qualified_name: t for t in types}, batch.qualified_name).amount
        return found.unit if found is not None else None

    assert unit(batch, stock) == "uL"
    new = unit(replace(batch, amount=Amount("mL")), stock)
    assert new == "mL"
    with_ledger = sample(world, "filled", {"value": 1500, "unit": "uL"})
    sample(world, "empty")
    findings = amount_findings("uL", new, SAMPLE)
    assert len(findings) == 1
    assert (
        findings[0].count == 1
        and findings[0].blocking
        and findings[0].examples == [with_ledger["hrid"]]
    )
    assert "0.001" in findings[0].message and "data migration" in findings[0].message
    stored = Balance.objects.get(pk=with_ledger["id"])
    assert stored.value == 1500 and stored.unit == "uL"
    assert amount_findings("uL", "uL", SAMPLE) == []
    assert amount_findings("uL", None, SAMPLE)[0].blocking


@pytest.mark.django_db
def test_ledger_units_have_exact_counts_and_bounded_examples(
    world: World, django_assert_num_queries: Callable[[int], AbstractContextManager[object]]
) -> None:
    from django.utils import timezone

    from iki_server.amounts.models import Amount
    from iki_server.entities.models import Entity
    from iki_server.plugins.preflight_values import amount_findings

    # Balances keep their own unit even when an earlier deployment changed the declaration.
    entities = Entity.objects.bulk_create(
        [Entity(type=SAMPLE, owner_project=world.project, hrid=f"BAL-{i:06d}") for i in range(511)]
    )
    Amount.objects.bulk_create(
        [Amount(entity_id=e.id, unit="uL", value=1) for e in entities[:504]]
        + [Amount(entity_id=e.id, unit="nL", value=1) for e in entities[504:510]]
        + [Amount(entity_id=entities[-1].id, unit="mL", value=1)]
    )
    Entity.objects.filter(pk=entities[504].pk).update(purged_at=timezone.now())
    with django_assert_num_queries(1):
        findings = amount_findings("uL", "mL", SAMPLE)
    assert [f.count for f in findings] == [5, 504]  # nL and uL, excluding purged records
    assert findings[0].examples == [e.hrid for e in entities[505:510]]
    assert findings[1].examples == [e.hrid for e in entities[:5]]
    assert all(f.blocking for f in findings)
    assert sum(f.count for f in amount_findings("uL", None, SAMPLE)) == 510
    empty = amount_findings("uL", "mL", "acme/protein.Protein")
    assert [(f.count, f.blocking, f.examples) for f in empty] == [(0, False, [])]

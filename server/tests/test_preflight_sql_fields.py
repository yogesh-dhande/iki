"""Exact scalar checks over a real typed schema, including records beyond the sample."""

from dataclasses import replace

import pytest
from org_world import World
from test_api_entities import api_as, create
from test_runs import world

from iki_server.entities import registry
from iki_server.plugins import preflight
from iki_spec.model import Field, StateMove

__all__ = ["world"]
pytestmark = pytest.mark.django_db
SAMPLE = "acme/protein.Sample"


def test_scalar_counts_cover_every_record_without_sampling(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = registry.plugins()
    plugin = current["acme/protein"]
    sample = plugin.types[SAMPLE]
    for label, concentration in (("A", 1.0), ("B", 2.0), ("CC", 3.0)):
        response = create(
            api_as(world), world.project, {"label": label, "concentration": concentration}, SAMPLE
        )
        assert response.status_code == 201, response.content
    fields = {
        **sample.fields,
        "protein": replace(sample.fields["protein"], required=True),
        "grade": Field("grade", "string", required=True),
        "origin": Field("origin", "string", required=True, has_default=True, default="lab"),
        "concentration": replace(sample.fields["concentration"], unit="ug/mL", max=1500),
        "label": replace(sample.fields["label"], min_length=2, pattern="[BC]+"),
        "status": replace(
            sample.fields["status"],
            initial="in_testing",
            states={
                **sample.fields["status"].states,
                "received": replace(
                    sample.fields["status"].states["received"], removed=True, moved_to="in_testing"
                ),
            },
        ),
    }
    proposed = replace(plugin, types={**plugin.types, SAMPLE: replace(sample, fields=fields)})
    monkeypatch.setattr(preflight, "SAMPLE_LIMIT", 1)
    report = preflight.compare(current, {**current, plugin.name: proposed}, "baseline", proposed)
    [impact] = report.types
    assert not impact.sampled
    assert impact.checked == 3
    found = {(f.kind, f.subject): f for f in impact.findings}
    assert found["backfill", "protein"].count == found["backfill", "grade"].count == 3
    assert found["backfill", "origin"].count == 3 and not found["backfill", "origin"].blocking
    assert found["unit", "concentration"].count == 3 and not found["unit", "concentration"].blocking
    assert found["value", "concentration"].count == found["value", "label"].count == 2
    assert found["pattern", "label"].count == 1
    assert found["state", "status"].count == 3 and not found["state", "status"].blocking


def test_exact_state_retirement_and_incompatible_quantity_on_deployed_columns(world: World) -> None:
    from iki_server.plugins.preflight_records import layout
    from iki_server.plugins.preflight_sql import Scope
    from iki_server.plugins.preflight_sql_fields import field_checks

    deployed = registry.plugins()
    sample = registry.types()[SAMPLE]
    scope = Scope([SAMPLE], layout(SAMPLE, deployed))
    record = create(api_as(world), world.project, {"label": "kept", "concentration": 3.0}, SAMPLE)
    assert record.status_code == 201, record.content
    old = sample.fields
    new = {
        **old,
        "status": replace(
            old["status"],
            removed=True,
            moved_to=StateMove(
                "custody", tuple((name, "in_lab") for name in old["status"].live_states)
            ),
        ),
        "concentration": replace(old["concentration"], unit="s"),
    }

    found = {f.kind: f for f in field_checks(scope, old, new)}
    expected = [record.json()["hrid"]]
    assert found["state"].examples == expected and found["state"].count == 1
    assert "custody.in_lab" in found["state"].message and not found["state"].blocking
    assert found["unit"].examples == expected and found["unit"].blocking


def test_type_without_a_matching_extension_row_still_needs_its_required_value(world: World) -> None:
    from django.db import connection
    from psycopg import sql

    from iki_server.plugins.preflight_records import layout
    from iki_server.plugins.preflight_sql import Scope
    from iki_server.plugins.preflight_sql_fields import field_checks

    kind = "acme/protein.Protein"
    protein = create(api_as(world), world.project, {"sequence": "MKV", "name": "Reference"}, kind)
    assert protein.status_code == 201, protein.content
    tables = layout(kind, registry.plugins())
    extension = next(t for t in tables if "predicted_by" in t.fields)
    # Existing records can predate an extension, so a row is optional.
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("DELETE FROM {} WHERE entity_id=%s").format(sql.Identifier(extension.table)),
            [protein.json()["id"]],
        )
    old = extension.fields
    new = {**old, "predicted_by": replace(old["predicted_by"], required=True)}
    [finding] = field_checks(Scope([kind], tables), old, new)
    assert (finding.subject, finding.kind, finding.count) == ("predicted_by", "backfill", 1)
    assert finding.examples == [f"{protein.json()['hrid']} ({protein.json()['id']})"]


def test_sample_normalized_list_items_only_when_their_data_needs_checking() -> None:
    from iki_server.plugins.preflight_sql_fields import sampled_fields

    unit = Field("volume", "quantity", unit="mL")
    row = Field("item", "row", fields={"volume": unit})
    table = Field("parts", "list", items=row)
    old = {"parts": table}
    presentation = replace(unit, description="Read from pipette", display_unit="uL")
    updated = replace(row, fields={"volume": presentation, "comment": Field("comment", "string")})
    assert sampled_fields(old, {"parts": replace(table, items=updated)}) == set()
    narrower = replace(row, fields={"volume": replace(unit, max=1.0)})
    assert sampled_fields(old, {"parts": replace(table, items=narrower)}) == {"parts"}


def test_new_normalized_list_has_empty_storage_for_existing_records(world: World) -> None:
    from iki_server.plugins.preflight_records import layout
    from iki_server.plugins.preflight_sql import Scope
    from iki_server.plugins.preflight_sql_fields import field_checks

    stored = create(api_as(world), world.project, {"label": "stored"}, SAMPLE)
    assert stored.status_code == 201, stored.content
    new = Field(
        "parts",
        "list",
        required=True,
        min_items=1,
        items=Field("parts", "row", fields={"v": Field("v", "int")}),
        has_default=True,
        default=[{"v": 1}],
    )
    [finding] = field_checks(
        Scope([SAMPLE], layout(SAMPLE, registry.plugins())), {}, {"parts": new}
    )
    assert (finding.kind, finding.count, finding.blocking) == ("backfill", 1, True)
    assert "empty" in finding.message.lower() and finding.examples == [stored.json()["hrid"]]

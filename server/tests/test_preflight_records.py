"""The promotion check reads deployed fields and bounds the work for a large type (ADR 0181)."""

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import build_world
from test_api_entities import api_as, create

from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.plugins import preflight_records
from iki_spec.model import Plugin

pytestmark = pytest.mark.django_db
PROTEIN = "acme/protein.Protein"
SAMPLE = "acme/protein.Sample"


def test_sample_joins_old_parent_extension_and_entry_and_preserves_list_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world()
    api = api_as(world)
    protein = create(
        api,
        world.project,
        {"sequence": "MKV", "name": "Reference", "hazard": "bsl1", "predicted_by": "model 2"},
        PROTEIN,
    )
    assert protein.status_code == 201, protein.content
    entry = protein.json()["entry"]["id"]
    sample = create(
        api,
        world.project,
        {
            "label": "S-2",
            "additions": [
                {"material": entry, "amount": 2.5, "added_on": "2026-01-10"},
                {"material": entry, "amount": 1.0},
            ],
        },
        SAMPLE,
    )
    assert sample.status_code == 201, sample.content
    deployed = registry.plugins()
    rows, more = preflight_records.records(PROTEIN, deployed, 1)
    assert not more
    (record,) = rows
    assert record.id == protein.json()["id"] and record.entry == entry
    assert record.project == str(world.project.pk) and record.registry == str(
        world.project.registry_id
    )
    assert {n: record.values[n] for n in ("name", "sequence", "hazard", "predicted_by")} == {
        "name": "Reference",
        "sequence": "MKV",
        "hazard": "bsl1",
        "predicted_by": "model 2",
    }
    with CaptureQueriesContext(connection) as queries:
        rows, more = preflight_records.records(SAMPLE, deployed, 5, fields={"additions"})
    assert not more and rows[0].values == {"additions": sample.json()["values"]["additions"]}
    assert len(queries) == 2  # one bounded key lookup and one read of the list's rows
    assert all(q["sql"].startswith("SELECT") for q in queries)
    by_ref = preflight_records.referenced({entry, rows[0].id, "legacy accession"}, deployed)
    assert "legacy accession" not in by_ref
    assert by_ref[entry]["id"] == entry and by_ref[entry]["hazard"] == "bsl1"
    assert "predicted_by" not in by_ref[entry]  # project-local fields aren't on the entry
    assert by_ref[rows[0].id]["additions"][0]["added_on"] == "2026-01-10T00:00:00Z"
    # The other side of a rule can change in the same promotion. An entry is
    # unread if its own fields will change; an unrelated project field can change
    # without changing what the reference holds.
    plugin = deployed["acme/protein"]
    old_type = plugin.types[PROTEIN]

    def changing(field: str) -> dict[str, Plugin]:
        old_field = old_type.fields[field]
        changed = replace(
            old_type, fields={**old_type.fields, field: replace(old_field, required=True)}
        )
        return {**deployed, plugin.name: replace(plugin, types={**plugin.types, PROTEIN: changed})}

    assert entry not in preflight_records.referenced({entry}, deployed, changing("hazard"))
    assert entry in preflight_records.referenced({entry}, deployed, changing("host"))
    # A long list must not exhaust the preview or be checked as if its partial value
    # were complete. Referenced records are subject to the same limit.
    monkeypatch.setattr(preflight_records, "LIST_ROWS", 1)
    assert preflight_records.records(SAMPLE, deployed, 5, fields={"additions"}) == ([], True)
    assert preflight_records.referenced({rows[0].id}, deployed) == {}


def nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def test_first_page_of_large_type_uses_the_type_and_hrid_index() -> None:
    world = build_world()
    # Only the base columns are needed for a new required field. Existing values of
    # unrelated fields can include large files' metadata, sequences and lists of rows.
    Entity.objects.bulk_create(
        [Entity(type=SAMPLE, hrid=f"SMX-{i:06d}", owner_project=world.project) for i in range(4096)]
    )
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE iki_entity")
    with CaptureQueriesContext(connection) as queries:
        rows, more = preflight_records.records(SAMPLE, registry.plugins(), 10, fields=set())
    assert len(rows) == 10 and more and all(r.values == {} for r in rows)
    assert len(queries) == 1
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + queries[0]["sql"])
        (plan,) = cursor.fetchone()
    if isinstance(plan, str):
        plan = json.loads(plan)
    reads = [n for n in nodes(plan[0]["Plan"]) if n.get("Relation Name") == "iki_entity"]
    assert len(reads) == 1
    assert reads[0]["Index Name"] == "iki_entity_hrid_sorted", reads
    assert reads[0]["Actual Rows"] <= 11, reads

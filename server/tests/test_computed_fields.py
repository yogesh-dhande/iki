"""Computed fields worked out by an expression (ADR 0297): at every save, from the record's
own values, before its rules; never set by a person or a run; and again by a deploy whose
expressions changed, except on a locked record."""

import datetime as dt
from typing import Any

import pytest
from api_client import problem
from django.db import connection
from org_world import World
from test_api_entities import api_as, create, patch
from test_runs import world
from test_states import ANALYST, QA, REVIEWER, roles, set_

from iki_server.audit.models import AuditEvent
from iki_server.entities import breaches, registry
from iki_server.entities.models import Entity, Revision, RulesChecked
from iki_server.plugins.operations import UNLOCKED_SETTING
from iki_server.runs import imports

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # a fixture, from test_runs

BATCH = "acme/protein.Batch"


def made(world: World, values: dict[str, Any], who: str = "editor") -> dict[str, Any]:
    answer = create(api_as(world, who), world.project, {"name": "L-7", **values}, BATCH)
    assert answer.status_code == 201, answer.content
    body: dict[str, Any] = answer.json()
    return body


def change(world: World, ref: str, values: dict[str, Any], who: str = "editor") -> Any:
    api = api_as(world, who)
    return patch(api, ref, {"values": values}, api.get(f"/entities/{ref}")["ETag"])


def _set_column(entity_id: str, column: str, value: Any) -> None:
    """As if worked out by an earlier expression: straight to its table, locked or not,
    as a plugin's migration writes (ADR 0125)."""
    table = registry.model(BATCH)._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, 'on', true)", [UNLOCKED_SETTING])
        cursor.execute(
            f'UPDATE "{table}" SET "{column}" = %s WHERE entity_id = %s',  # noqa: S608 - ours
            [value, entity_id],
        )


def test_a_type_says_what_its_computed_fields_are_worked_out_by(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{BATCH}").json()
    fields = {f["name"]: f for f in found["fields"]}
    use_by = fields["use_by"]
    assert use_by["computed"] is True
    assert use_by["cel"].startswith("record.made_on + duration(")
    assert use_by["cel_reads"] == ["made_on", "shelf_life_days"]
    assert fields["made_on"]["cel"] is None and fields["made_on"]["cel_reads"] == []


def test_a_new_record_is_saved_with_what_its_values_work_out_to(world: World) -> None:
    body = made(world, {"made_on": "2026-11-27", "shelf_life_days": 30, "purity": 96.5})
    values = body["values"]
    assert values["use_by"] == "2026-12-27"
    assert values["purity_fraction"] == pytest.approx(0.965)
    # One computed value read from another, worked out after it.
    assert values["use_by_label"] == "L-7 until 2026-12-27T00:00:00Z"
    first = Revision.objects.get(entity_id=body["id"], number=1)
    assert {"use_by", "purity_fraction", "use_by_label"} <= set(first.changed_fields)


def test_an_edit_works_its_computed_values_out_again_in_its_revision(world: World) -> None:
    body = made(world, {"made_on": "2026-11-27", "shelf_life_days": 30})
    answer = change(world, body["id"], {"shelf_life_days": 7})
    assert answer.status_code == 200, answer.content
    assert answer.json()["values"]["use_by"] == "2026-12-04"
    second = Revision.objects.get(entity_id=body["id"], number=2)
    assert second.changed_fields == ["shelf_life_days", "use_by", "use_by_label"]
    # Its history keeps each revision's, as it was worked out then.
    api = api_as(world)
    first = api.get(f"/entities/{body['id']}/revisions/1").json()
    assert first["values"]["use_by"] == "2026-12-27"


def test_no_one_sets_a_computed_value(world: World) -> None:
    body = problem(
        create(api_as(world), world.project, {"name": "L-8", "use_by": "2027-01-01"}, BATCH),
        422,
        "validation_failed",
    )
    assert [e["pointer"] for e in body["errors"]] == ["#/values/use_by"]
    assert "no one sets it" in body["errors"][0]["detail"]
    saved = made(world, {"made_on": "2026-11-27", "shelf_life_days": 30})
    problem(change(world, saved["id"], {"use_by": "2027-01-01"}), 422, "validation_failed")


def test_what_cant_be_worked_out_is_empty(world: World) -> None:
    body = made(world, {"made_on": "2026-11-27"})
    assert body["values"]["use_by"] is None
    assert body["values"]["use_by_label"] is None
    # Taking away what it's worked out from empties it.
    saved = made(world, {"made_on": "2026-11-27", "shelf_life_days": 30})
    answer = change(world, saved["id"], {"shelf_life_days": None})
    assert answer.json()["values"]["use_by"] is None


def test_a_computed_value_its_field_refuses_refuses_the_save(world: World) -> None:
    body = problem(
        create(api_as(world), world.project, {"name": "L-9", "purity": 40.0}, BATCH),
        422,
        "validation_failed",
    )
    found = {e["pointer"]: e["detail"] for e in body["errors"]}
    assert found["#/values/purity_fraction"].startswith(
        "is worked out as 0.4, which must be at least 0.5"
    )
    # Said with what its rules say of what it's worked out from.
    assert "#/values/purity" in found


def test_a_chunk_of_imported_lines_is_worked_out_before_its_rules(world: World) -> None:
    # As stored: what an import's lines hold once they're read.
    good: dict[str, Any] = {"name": "L-10", "made_on": dt.date(2026, 11, 27), "shelf_life_days": 1}
    bad: dict[str, Any] = {"name": "L-11", "purity": 10.0}
    found = imports.breaking([(BATCH, good), (BATCH, bad)])
    assert good["use_by"] == dt.date(2026, 11, 28)
    broken = found[1][0]
    assert broken.fields == ("purity_fraction",) and not broken.held
    assert "is worked out as 0.1" in broken.message
    # And its rules, checked as well.
    assert [b.rule.name for b in found[1][1:]] == ["purity_in_specification"]
    assert 0 not in found


def test_a_deploy_works_out_again_what_an_expression_gives_except_on_a_locked_record(
    world: World,
) -> None:
    roles(world, editor=(ANALYST,), admin=(REVIEWER, QA))
    open_ = made(world, {"made_on": "2026-11-27", "shelf_life_days": 30})
    locked = made(world, {"made_on": "2026-11-27", "shelf_life_days": 30, "purity": 99.0})
    set_(world, "editor", locked["id"], "status", "in_review")
    assert set_(world, "admin", locked["id"], "status", "approved")["locked"]
    # As an earlier expression worked them out.
    for ref in (open_["id"], locked["id"]):
        _set_column(ref, "use_by", "2026-12-01")
    RulesChecked.objects.all().delete()
    breaches.recheck()
    entity = Entity.objects.get(pk=open_["id"])
    latest = Revision.objects.get(entity=entity, number=entity.revision)
    assert (latest.kind, latest.via, latest.user_id) == ("migrated", "system", None)
    assert latest.changed_fields == ["use_by"]
    assert latest.reason.startswith("Worked out by acme/protein ")
    api = api_as(world)
    assert api.get(f"/entities/{open_['id']}").json()["values"]["use_by"] == "2026-12-27"
    # What was locked stays as it was locked.
    assert api.get(f"/entities/{locked['id']}").json()["values"]["use_by"] == "2026-12-01"
    event = AuditEvent.objects.get(action="plugin.computed_worked_out", data__type=BATCH)
    assert event.data["changed"] == {"use_by": 1}
    assert event.data["locked"] == {"use_by": 1}

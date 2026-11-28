"""Inventory hold flags keep the reason for a second session's edit."""

import json
import os
import runpy
import sys
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from api_client import Api, problem
from django.db import transaction
from django.utils import timezone
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import build_world
from test_plugin_migrations import database, django, iki, migrate, sync

from iki_server.amounts import services as amounts
from iki_server.amounts.models import Amount
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.runs import catalog, queue, results, triggers
from iki_server.runs.models import ActionRun, FiringState, Job, TriggerFiring
from iki_spec.compute import Binding

__all__ = ["database"]

PLUGIN = PLUGINS / "inventory"


def test_shipping_hold_flags_require_the_clearing_editors_reason(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); "
        "from test_inventory_hold_reasons import exercise_hold_reasons; "
        "exercise_hold_reasons()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise_hold_reasons() -> None:
    world = build_world()
    editor, admin = world.users["editor"], world.users["admin"]
    first, second = Api(world.browser("editor")), Api(world.browser("admin"))

    def make(kind: str, fields: Mapping[str, Any]) -> Entity:
        made = services.create(editor, kind, world.project, fields).entity
        return Entity.objects.get(pk=made.pk)

    original_reason = "The label needs a second check."
    clear_reason = "The stock and recorded identifier match."
    material = make("iki/core.Collection", {"name": "Training reference"})
    fields = {"name": "Training stock", "contents": str(material.pk)}
    destination = make("iki/inventory.Tube", fields)
    portion = amounts.Given.of({"value": 1, "unit": "uL"})
    empty = make("iki/inventory.Tube", {"name": "Unassigned"})
    with pytest.raises(amounts.Wrong, match="contents"):
        amounts.record(editor, empty, portion)
    assert not Amount.objects.filter(entity=empty).exists()

    for cleared in (False, None):
        record = make("iki/inventory.Tube", fields)
        amounts.record(editor, record, amounts.Given.of({"value": 10, "unit": "uL"}))
        field = next(
            f
            for f in first.get(f"/entity-types/{record.type}").json()["fields"]
            if f["name"] == "on_hold"
        )
        assert field["reason_on_clear"] is True

        path = f"/entities/{record.pk}"
        held = first.patch(
            path,
            {"values": {"on_hold": True, "hold_reason": original_reason}},
            headers={"If-Match": first.get(path)["ETag"]},
        )
        assert held.status_code == 200, held.content
        with pytest.raises(amounts.Wrong, match="on hold"):
            amounts.draw(editor, record, portion, confirm=record.hrid)
        with pytest.raises(amounts.Wrong, match="on hold"):
            amounts.transfer(editor, record, destination, portion)
        with pytest.raises(amounts.Wrong, match="on hold"):
            amounts.split(editor, record, 1, portion)
        assert Amount.objects.get(entity=record).value == 10
        etag = second.get(path)["ETag"]
        assert etag == held["ETag"]
        headers = {"If-Match": etag}
        held_revision = held.json()["revision"]
        changes = {"values": {"on_hold": cleared, "hold_reason": None}}

        for extra in ({}, {"reason": " \t", "hold_reason": "Reason for a rule exception."}):
            refused = problem(
                second.patch(path, {**changes, **extra}, headers=headers),
                422,
                "clear_reason_required",
            )
            assert [error["pointer"] for error in refused["errors"]] == ["#/reason"]
            unchanged = first.get(path)
            assert unchanged["ETag"] == etag
            assert unchanged.json()["values"]["on_hold"] is True
            assert unchanged.json()["values"]["hold_reason"] == original_reason

        edited = second.patch(path, {**changes, "reason": f"  {clear_reason}  "}, headers=headers)
        assert edited.status_code == 200, edited.content
        assert edited.json()["revision"] == held_revision + 1
        assert edited.json()["values"].get("on_hold") is cleared
        assert edited.json()["values"].get("hold_reason") is None
        problem(first.patch(path, changes, headers=headers), 412, "stale")

        original = second.get(f"{path}/revisions/{held_revision}").json()
        correction = first.get(f"{path}/revisions/{held_revision + 1}").json()
        assert original["user"]["id"] == str(editor.pk)
        assert original["created_at"]
        assert original["values"]["on_hold"] is True
        assert original["values"]["hold_reason"] == original_reason
        assert correction["user"]["id"] == str(admin.pk)
        assert correction["created_at"]
        assert correction["reason"] == clear_reason
        assert correction["changes"]["on_hold"] == {"before": True, "after": cleared}
        assert correction["changes"]["hold_reason"] == {"before": original_reason, "after": None}

    # A split and a transfer give the trigger two descendants. A sibling holding
    # the very same material has no physical derivation edge from this source.
    source = make("iki/inventory.Tube", fields)
    amounts.record(editor, source, amounts.Given.of({"value": 10, "unit": "uL"}))
    (child,) = amounts.split(editor, source, 1, portion).aliquots
    amounts.transfer(editor, source, destination, portion)
    sibling = make("iki/inventory.Tube", fields)
    env = "iki/inventory.stock-tools"
    catalog.compute().bindings[env] = Binding(environment=env, templates=(("backend-small", 0),))
    trigger = triggers.enable(admin, world.project, "iki/inventory.hold_derived_tubes")

    def hold_stock() -> tuple[ActionRun, dict[str, Any]]:
        source.refresh_from_db()
        services.update(
            editor,
            source,
            {"on_hold": True, "hold_reason": original_reason},
            expected_revision=source.revision,
        )
        source.refresh_from_db()
        firing = TriggerFiring.objects.get(trigger=trigger, entity=source, revision=source.revision)
        triggers.fire(str(firing.pk))
        firing.refresh_from_db()
        assert firing.state == FiringState.LAUNCHED, firing.error
        run = firing.run
        assert run is not None
        assert {(i.name, i.entity_id) for i in run.inputs.all()} == {
            ("source", source.pk),
            ("tubes", child.pk),
            ("tubes", destination.pk),
        }
        job = Job.objects.get(run=run)
        job.lease_token, job.lease_expires_at = uuid4(), timezone.now() + queue.LEASE_TTL
        with TemporaryDirectory() as tmp:
            input_file, output_file = Path(tmp) / "input.json", Path(tmp) / "output.json"
            input_file.write_text(json.dumps(queue.payload(job, run)))
            # As the worker runs it: its directory first, for the action helper beside it.
            scripts = str(PLUGIN / "scripts")
            with (
                patch.dict(os.environ, IKI_INPUT=str(input_file), IKI_OUTPUT=str(output_file)),
                patch.object(sys, "path", [scripts, *sys.path]),
            ):
                runpy.run_path(str(PLUGIN / "scripts" / "hold.py"), run_name="__main__")
            return run, json.loads(output_file.read_text())

    def commit(run: ActionRun, output: dict[str, Any]) -> None:
        with transaction.atomic():
            results.commit(ActionRun.objects.select_for_update().get(pk=run.pk), output)

    stale, output = hold_stock()
    services.update(
        admin, source, {"on_hold": False}, reason=clear_reason, expected_revision=source.revision
    )
    with pytest.raises(results.Rejected, match="changed after"):
        commit(stale, output)
    children = services.current_values_of([child, destination])
    assert not any(v["on_hold"] for v in children.values())

    fresh, output = hold_stock()
    commit(fresh, output)
    values = services.current_values_of([child, destination, sibling])
    assert not values[sibling.pk]["on_hold"]
    for record in [child, destination]:
        assert values[record.pk]["on_hold"]
        assert (
            values[record.pk]["hold_reason"] == f"Withdrawn stock {source.hrid}: {original_reason}"
        )

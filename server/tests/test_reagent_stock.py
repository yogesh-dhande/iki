"""Receive a supplier's partial deliveries through the installed action into real stock."""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from first_party_dependencies import PLUGINS, build_order
from org_world import World, build_world
from scan_fixtures import clean_blob
from test_api_entities import api_as
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import ready, signed

from iki_server.amounts import services as amounts
from iki_server.amounts import stock_alerts
from iki_server.amounts.models import Amount, Movement
from iki_server.entities import services, states
from iki_server.entities.models import Entity, Revision
from iki_server.files.models import File
from iki_server.notifications.models import Notification
from iki_server.org.models import LabRoleGrant
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, Job, RunEffect
from iki_server.storage.models import Place
from iki_spec.compute import Binding

__all__ = ["database"]

PLUGIN = PLUGINS / "reagents"
PREFIX = "iki/reagents."
Attempt = tuple[ActionRun, dict[str, Any]]


def test_supplier_receipts_keep_the_order_inventory_and_storage_consistent(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    exercised = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_reagent_stock import exercise; exercise()",
    )
    assert exercised.returncode == 0, exercised.stdout + exercised.stderr


def prepare(world: World, name: str, inputs: dict[str, Entity], params: dict[str, Any]) -> Attempt:
    """Run the plugin's actual command on the server's pinned inputs and inspect its output."""
    action = catalog.action(PREFIX + name)
    assert action is not None
    run = runs.launch(
        world.users["editor"],
        action,
        world.project,
        {key: [entity.pk] for key, entity in inputs.items()},
        params,
        reuse_results=False,
    )
    job = Job.objects.get(run=run)
    job.lease_token = uuid4()
    job.lease_expires_at = timezone.now() + queue.LEASE_TTL
    payload = queue.payload(job, run)
    with TemporaryDirectory() as stage:
        inp, out = Path(stage) / "input.json", Path(stage) / "output.json"
        inp.write_text(json.dumps(payload))
        command = subprocess.run(  # noqa: S603 - this installed plugin's command
            [sys.executable, *action.command[1:]],
            cwd=PLUGIN,
            env={**os.environ, "IKI_INPUT": str(inp), "IKI_OUTPUT": str(out)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if command.returncode:
            raise ValueError(command.stdout + command.stderr)
        return run, json.loads(out.read_text())


def commit(attempt: Attempt) -> None:
    run, document = attempt
    with transaction.atomic():
        locked = ActionRun.objects.select_for_update().get(pk=run.pk)
        results.commit(locked, document)


def race(first: Attempt, second: Attempt) -> None:
    """Two independent worker completions compete for the same remaining packs."""
    barrier = Barrier(2, timeout=20)

    def compete(attempt: Attempt) -> str:
        try:
            barrier.wait()
            commit(attempt)
        except results.Rejected as error:
            assert error.code == "conflict", error.message
            assert not RunEffect.objects.filter(run=attempt[0]).exists()
            return "stale"
        finally:
            connection.close()
        return "received"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(compete, (first, second))) == ["received", "stale"]


def exercise() -> None:
    world = build_world()
    ready(world)
    editor = world.users["editor"]
    for who, role in (("admin", "iki/lots.qa"), ("runner", PREFIX + "stock_manager")):
        LabRoleGrant.objects.create(
            membership=world.project.memberships.get(user=world.users[who]), role=role
        )
    env = PREFIX + "reagent-tools"
    catalog.compute().bindings[env] = Binding(environment=env, templates=(("backend-small", 0),))
    # Corning 430790 15 mL tubes come 500 per case. This order, its lots, the threshold,
    # and all stock movements are synthetic. Source: https://ecatalog.corning.com/life-sciences/b2b/us/en/p/430790
    product = {
        "name": "Corning 15 mL centrifuge tubes",
        "supplier": "Corning",
        "catalogue_number": "430790",
        "stock_unit": "1",
        "pack_size": 500,
        "low_stock_at": 100,
    }
    item = Entity.objects.get(
        pk=services.create(editor, PREFIX + "StockItem", world.project, product).entity.pk
    )

    def location(label: str) -> Entity:
        return Entity.objects.get(
            pk=services.create(
                editor,
                PREFIX + "StockLocation",
                world.project,
                {"name": label, "rows": 1, "columns": 2},
            ).entity.pk
        )

    east, west = location("East shelf"), location("West shelf")
    blob = clean_blob(
        sha256=hashlib.sha256(b"synthetic").hexdigest(),
        size=9,
        storage_uri="s3://test/stock-certificate",
    )
    coa = File.objects.create(
        blob=blob,
        project=world.project,
        name="synthetic-certificate.pdf",
        media_type="application/pdf",
    )
    api = api_as(world, "viewer")
    base = f"/projects/{world.project.pk}/stock"

    def summary(physical: int, stored: int, available: int, outstanding: int) -> None:
        page = api.get(base)
        assert page.status_code == 200, page.content
        (row,) = page.json()["items"]
        assert row["id"] == str(item.pk)
        assert [Decimal(row[key]) for key in ("physical", "stored", "available")] == [
            physical,
            stored,
            available,
        ]
        assert row["outstanding_packs"] == outstanding
        pending = api.get(base + "/orders")
        assert pending.status_code == 200, pending.content
        assert sum(line["outstanding_packs"] for line in pending.json()["items"]) == outstanding
        assert all(line["item_id"] == str(item.pk) for line in pending.json()["items"])
        assert row["low"] == (available <= 100)
        filtered = api.get(base, low_only="true").json()["items"]
        assert [i["id"] for i in filtered] == ([str(item.pk)] if available <= 100 else [])

    summary(0, 0, 0, 0)
    assert stock_alerts.send_alerts() == 1
    assert stock_alerts.send_alerts() == 0
    assert Notification.objects.filter(
        recipient=world.users["runner"], entity=item, project=world.project, kind="stock.low"
    ).exists()
    assert api_as(world, "outsider").get(base).status_code in (403, 404)
    commit(
        prepare(
            world,
            "place_order",
            {"item": item},
            {"order_reference": "Synthetic 001", "ordered_packs": 3},
        )
    )
    order = Entity.objects.get(type=PREFIX + "ReorderLine")
    with pytest.raises(services.Invalid):
        services.create(
            editor,
            PREFIX + "CountStockLot",
            world.project,
            {"item": str(item.pk), "order_line": str(order.pk), "lot_number": "Missing receipt"},
            amount={"value": 500, "unit": "1"},
        )
    assert not Entity.objects.filter(type=PREFIX + "CountStockLot").exists()
    summary(0, 0, 0, 3)
    today = timezone.localdate()

    def receive(
        line: str,
        packs: int,
        shelf: Entity,
        position: str,
        *,
        expiry: str | None = None,
        received: str | None = None,
    ) -> Attempt:
        return prepare(
            world,
            "receive_delivery",
            {"order": order, "storage": shelf},
            {
                "delivery_note": "Synthetic DN-001",
                "delivery_line": line,
                "packs": packs,
                "lot_number": f"Synthetic lot {line}",
                "received_on": received or today.isoformat(),
                "position": position,
                "expires_on": expiry or (today + timedelta(days=365)).isoformat(),
                "certificate": str(coa.pk),
            },
        )

    race(receive("1", 1, east, "A1"), receive("1", 1, east, "A1"))
    stock_lots = Entity.objects.filter(type=PREFIX + "CountStockLot")
    first = stock_lots.get()
    place = Place.objects.get(entity=first)
    assert (place.container_id, place.row, place.column) == (east.pk, 1, 1)
    recorded = Movement.objects.get(entity=first)
    assert (recorded.kind, recorded.balance, recorded.unit) == ("recorded", Decimal(500), "1")
    assert recorded.user_id == editor.pk
    assert services.current_values_of([first])[first.pk]["order_line"] == order.pk
    # Quarantine affects availability while the received packs are physically on the shelf.
    summary(500, 500, 0, 2)
    with pytest.raises(amounts.Wrong, match="quarantined"):
        amounts.draw(editor, first, amounts.Given(Decimal(1), "1"))

    def unchanged() -> None:
        assert stock_lots.count() == 1
        assert Amount.objects.get(entity=first).value == 500
        assert Movement.objects.filter(entity__in=stock_lots).count() == 1
        assert Place.objects.filter(entity__in=stock_lots).count() == 1
        order.refresh_from_db()
        assert services.current_values_of([order])[order.pk]["received_packs"] == 1

    # Same delivery after the winner commits, even at a different location.
    duplicate = receive("1", 1, west, "A1")
    with pytest.raises(results.InvalidOutput):
        commit(duplicate)
    assert not RunEffect.objects.filter(run=duplicate[0]).exists()
    unchanged()
    occupied = receive("2", 1, east, "A1")
    with pytest.raises(results.InvalidOutput) as refused:
        commit(occupied)
    assert "A1" in str(refused.value.errors)
    assert not RunEffect.objects.filter(run=occupied[0]).exists()
    unchanged()
    with pytest.raises((ValueError, results.Failed), match="outstanding"):
        commit(receive("over", 3, west, "A1"))
    unchanged()
    signed(world, "admin", str(first.pk), "released", field="disposition")
    summary(500, 500, 500, 2)
    assert stock_alerts.send_alerts() == 0  # Clears the initial low-stock episode.
    fraction = api_as(world).post(
        f"/entities/{first.pk}/amount/draw", {"amount": {"value": "0.5", "unit": "1"}}
    )
    assert fraction.status_code == 422, fraction.content
    assert "requires whole units" in fraction.json()["errors"][0]["detail"]
    assert Amount.objects.get(entity=first).value == 500
    amounts.draw(editor, first, amounts.Given(Decimal(400), "1"))
    summary(100, 100, 100, 2)
    assert stock_alerts.send_alerts() == 1  # The threshold itself is low.

    # The final delivery occupies another location and closes the remaining packs exactly.
    commit(receive("2", 2, west, "A2"))
    second = stock_lots.exclude(pk=first.pk).get()
    place = Place.objects.get(entity=second)
    assert (place.container_id, place.row, place.column) == (west.pk, 1, 2)
    assert Amount.objects.get(entity=second).value == 1000
    summary(1100, 1100, 100, 0)
    signed(world, "admin", str(second.pk), "released", field="disposition")
    summary(1100, 1100, 1100, 0)
    assert stock_alerts.send_alerts() == 0
    detail = api.get(base + f"/{item.pk}/lots")
    assert detail.status_code == 200, detail.content
    assert {r["container_id"] for r in detail.json()["items"]} == {str(east.pk), str(west.pk)}
    assert api_as(world, "outsider").get(base + f"/{item.pk}/lots").status_code in (403, 404)

    # Changing custody affects availability immediately. Checking an item out keeps its slot.
    checked = api_as(world).post(
        "/storage/check-out",
        {"records": [str(second.pk)], "to": str(editor.pk), "reason": "Synthetic use"},
    )
    assert checked.status_code == 200, checked.content
    summary(1100, 100, 100, 0)
    amounts.draw(editor, first, amounts.Given(Decimal(100), "1"))
    summary(1000, 0, 0, 0)

    # A delivery note and line cannot be posted against another order either.
    complete = order
    commit(
        prepare(
            world,
            "place_order",
            {"item": item},
            {
                "order_reference": "Synthetic 002",
                "ordered_packs": 1,
            },
        )
    )
    order = Entity.objects.filter(type=PREFIX + "ReorderLine").exclude(pk=complete.pk).get()
    duplicate = receive("1", 1, west, "A1")
    with pytest.raises(results.InvalidOutput):
        commit(duplicate)
    assert stock_lots.count() == 2 and not RunEffect.objects.filter(run=duplicate[0]).exists()
    summary(1000, 0, 0, 1)

    # Post a historical delivery, with stock still on the shelf on its expiry date.
    commit(
        receive(
            "3",
            1,
            east,
            "A2",
            received=(today - timedelta(days=8)).isoformat(),
            expiry=today.isoformat(),
        )
    )
    expired = stock_lots.exclude(pk__in=[first.pk, second.pk]).get()
    signed(world, "admin", str(expired.pk), "released", field="disposition")
    summary(1500, 500, 0, 0)
    with pytest.raises(amounts.Wrong, match="expiry"):
        amounts.draw(editor, expired, amounts.Given(Decimal(1), "1"))
    assert Amount.objects.get(entity=expired).value == 500
    by_id = {row["id"]: row for row in api.get(base + f"/{item.pk}/lots").json()["items"]}
    assert by_id[str(second.pk)]["checked_out"]
    assert by_id[str(expired.pk)]["expired"] and by_id[str(expired.pk)]["held"]
    assert by_id[str(expired.pk)]["expires_on"] == today.isoformat()
    assert by_id[str(first.pk)]["amount"] == "0"

    # A lab that receives stock without ordering it in Iki, the default procedure. Its
    # opening count goes to a cabinet without numbered positions, and an item it doesn't
    # reorder by level has no threshold, so it's never low and nobody is alerted.
    cabinet = Entity.objects.get(
        pk=services.create(
            editor, PREFIX + "StockArea", world.project, {"name": "Synthetic cabinet"}
        ).entity.pk
    )
    film = Entity.objects.get(
        pk=services.create(
            editor,
            PREFIX + "StockItem",
            world.project,
            {
                "name": "Synthetic sealing film",
                "supplier": "Synthetic supplier",
                "catalogue_number": "SYN-FILM-1",
                "stock_unit": "1",
                "pack_size": 100,
            },
        ).entity.pk
    )
    (row,) = [i for i in api.get(base).json()["items"] if i["id"] == str(film.pk)]
    assert (row["threshold"], row["low"]) == (None, False)
    # The tubes are low again after the draws above; the film is never low.
    assert stock_alerts.send_alerts() == 1
    assert not Notification.objects.filter(entity=film).exists()

    def unordered(on: Entity, kind: str, note: str, **more: str) -> Attempt:
        return prepare(
            world,
            "receive_stock",
            {"item": on, "storage": cabinet},
            {
                "received_as": kind,
                "delivery_note": note,
                "delivery_line": "1",
                "packs": 2,
                "lot_number": f"Synthetic {kind}",
                "received_on": today.isoformat(),
                **more,
            },
        )

    commit(unordered(film, "opening_balance", "Synthetic count 2026-11"))
    counted = (
        Entity.objects.filter(type=PREFIX + "CountStockLot")
        .exclude(pk__in=[first.pk, second.pk, expired.pk])
        .get()
    )
    values = services.current_values_of([counted])[counted.pk]
    assert (values["received_as"], values["order_line"], values["item"]) == (
        "opening_balance",
        None,
        film.pk,
    )
    place = Place.objects.get(entity=counted)
    assert (place.container_id, place.row, place.column) == (cabinet.pk, None, None)
    assert Amount.objects.get(entity=counted).value == 200
    assert Movement.objects.get(entity=counted).reason == (
        "Opening stock, count Synthetic count 2026-11, line 1."
    )
    # The supplier's delivery line already received against its order can't be received
    # again without one, nor can the count be posted twice.
    for kind, on, note in (
        ("delivery", item, "Synthetic DN-001"),
        ("opening_balance", film, "Synthetic count 2026-11"),
    ):
        again = unordered(on, kind, note)
        with pytest.raises(results.InvalidOutput):
            commit(again)
        assert not RunEffect.objects.filter(run=again[0]).exists()
    assert stock_lots.count() == 4
    # A lot is received only by a receipt, with or without its order.
    with pytest.raises(services.Invalid):
        services.create(
            editor,
            PREFIX + "CountStockLot",
            world.project,
            {"item": str(film.pk), "lot_number": "No receipt"},
            amount={"value": 100, "unit": "1"},
        )


def test_an_hourly_pass_over_a_large_catalogue_takes_as_many_queries_as_a_small_one(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    exercised = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_reagent_stock import catalogue; catalogue()",
    )
    assert exercised.returncode == 0, exercised.stdout + exercised.stderr


def catalogue() -> None:
    """The hourly pass with the items' real stock summaries: items with nothing in stock,
    whether the lab reorders them by level or not, cost no queries of their own once their
    stock manager has been told."""
    world = build_world()
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["runner"]),
        role=PREFIX + "stock_manager",
    )
    editor = world.users["editor"]

    def stocked(numbers: range, threshold: float | None) -> None:
        # Synthetic catalogue entries for a lab's buffers and plastics; none has stock yet.
        for n in numbers:
            services.create(
                editor,
                PREFIX + "StockItem",
                world.project,
                {
                    "name": f"Synthetic PBS pH 7.4, 500 mL, item {n}",
                    "supplier": "Synthetic supplier",
                    "catalogue_number": f"SYN-PBS-{n:03}",
                    "stock_unit": "1",
                    "pack_size": 1,
                    **({"low_stock_at": threshold} if threshold is not None else {}),
                },
            )

    def checked() -> tuple[int, int]:
        with CaptureQueriesContext(connection) as queries:
            sent = stock_alerts.send_alerts()
        return sent, len(queries)

    stocked(range(2), None)
    stocked(range(2, 4), 2)
    assert stock_alerts.send_alerts() == 2
    sent, few = checked()
    assert sent == 0
    stocked(range(4, 40), None)
    stocked(range(40, 80), 2)
    assert stock_alerts.send_alerts() == 40
    again = checked()
    assert again == (0, few), (again, few)
    assert (
        Notification.objects.filter(recipient=world.users["runner"], kind="stock.low").count() == 42
    )


def test_a_critical_reagents_lots_are_only_released_by_lot_qa(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    exercised = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_reagent_stock import critical; critical()",
    )
    assert exercised.returncode == 0, exercised.stdout + exercised.stderr


def critical() -> None:
    """A project with both routes (ADR 0242 and its amendment): Lot QA marks a stock item a
    critical reagent; each lot received of it needs Lot QA's signed release, which a stock
    keeper can't stand in for, while a consumable's lot is made available as before. Only
    Lot QA changes either requirement, saying why, and history keeps each change."""
    world = build_world()
    ready(world)
    qa, keeper = world.users["admin"], world.users["runner"]
    for who, role in (("admin", "iki/lots.qa"), ("runner", "iki/lots.stock_keeper")):
        LabRoleGrant.objects.create(
            membership=world.project.memberships.get(user=world.users[who]), role=role
        )
    env = PREFIX + "reagent-tools"
    catalog.compute().bindings[env] = Binding(environment=env, templates=(("backend-small", 0),))
    editor = world.users["editor"]

    def made(kind: str, values: dict[str, Any]) -> Entity:
        return Entity.objects.get(
            pk=services.create(editor, PREFIX + kind, world.project, values).entity.pk
        )

    def change(who: Any, entity: Entity, field: str, state: str, reason: str = "") -> str:
        """The state it's in after ``who`` sets it, or why they couldn't."""
        entity.refresh_from_db()
        try:
            states.set_state(
                who, entity, field, state, expected_revision=entity.revision, reason=reason
            )
        except (PermissionDenied, Refused, services.Invalid) as error:
            return f"refused: {error}"
        entity.refresh_from_db()
        return str(states.values_of(entity)[field])

    # The lab's ELISA detection antibody is a critical reagent; its tubes are consumables.
    # Products, lots, documents and amounts are synthetic.
    antibody = made(
        "StockItem",
        {
            "name": "Synthetic anti-human IgG HRP detection antibody",
            "supplier": "Synthetic Antibodies Inc.",
            "catalogue_number": "SYN-HRP-100",
            "stock_unit": "mL",
            "pack_size": 1,
        },
    )
    tubes = made(
        "StockItem",
        {
            "name": "Synthetic 1.5 mL tubes",
            "supplier": "Synthetic Plastics Inc.",
            "catalogue_number": "SYN-T15",
            "stock_unit": "1",
            "pack_size": 500,
        },
    )
    fridge = made("StockLocation", {"name": "Synthetic 4 °C fridge", "rows": 2, "columns": 4})
    blob = clean_blob(
        sha256=hashlib.sha256(b"synthetic coa").hexdigest(),
        size=13,
        storage_uri="s3://test/critical-certificate",
    )
    coa = File.objects.create(
        blob=blob, project=world.project, name="synthetic-coa.pdf", media_type="application/pdf"
    )

    # Only Lot QA marks it, and says why; history keeps who, when and why.
    assert change(keeper, antibody, "release_requirement", "required", "Critical").startswith(
        "refused"
    )
    assert change(qa, antibody, "release_requirement", "required").startswith("refused")
    why = "Critical reagent for the potency ELISA (synthetic SOP-QC-112)"
    assert change(qa, antibody, "release_requirement", "required", why) == "required"
    marked = Revision.objects.get(
        entity=antibody, state_field="release_requirement", state_to="required"
    )
    assert (marked.user_id, marked.reason, marked.state_from) == (qa.pk, why, "not_required")

    def receive(item: Entity, line: str) -> Entity:
        run, document = prepare(
            world,
            "receive_stock",
            {"item": item, "storage": fridge},
            {
                "received_as": "delivery",
                "delivery_note": "Synthetic DN-7731",
                "delivery_line": line,
                "packs": 1,
                "lot_number": f"Synthetic lot {line}",
                "received_on": timezone.localdate().isoformat(),
                "certificate": str(coa.pk),
            },
        )
        commit((run, document))
        (effect,) = RunEffect.objects.filter(run=run, kind="created")
        return Entity.objects.get(pk=effect.entity_id)

    first, second, consumable, released_too = (
        receive(antibody, "1"),
        receive(antibody, "2"),
        receive(tubes, "3"),
        receive(tubes, "4"),
    )
    flags = services.current_values_of([first, second, consumable])
    assert [flags[lot.pk]["requires_qa_release"] for lot in (first, second, consumable)] == [
        True,
        True,
        False,
    ]

    # A stock keeper makes the consumable available, but not the critical reagent.
    assert change(keeper, consumable, "disposition", "available") == "available"
    refused = change(keeper, first, "disposition", "available")
    assert "needs Lot QA's signed release" in refused, refused
    # Lot QA's signed release still makes either usable.
    for lot in (first, released_too):
        signed(world, "admin", str(lot.pk), "released", field="disposition")
        lot.refresh_from_db()
        assert states.values_of(lot)["disposition"] == "released"

    # Unmarking the item leaves the lots already received as they were.
    assert change(qa, antibody, "release_requirement", "not_required", "Reassessed") == (
        "not_required"
    )
    assert services.current_values_of([second])[second.pk]["requires_qa_release"] is True
    assert change(keeper, second, "disposition", "available").startswith("refused")
    # A stock keeper can't lift a lot's requirement; Lot QA can, saying why.
    assert change(keeper, second, "release_requirement", "not_required", "Mine").startswith(
        "refused"
    )
    assert change(qa, second, "release_requirement", "not_required").startswith("refused")
    lifted = "Research use only: the potency ELISA uses lot 1 (synthetic deviation DV-0042)"
    assert change(qa, second, "release_requirement", "not_required", lifted) == "not_required"
    assert change(keeper, second, "disposition", "available") == "available"
    # And Lot QA can require it of a lot recorded without it.
    assert change(qa, consumable, "release_requirement", "required", "Sterility") == "required"
    assert change(keeper, consumable, "disposition", "on_hold", "Check") == "on_hold"
    assert change(keeper, consumable, "disposition", "available").startswith("refused")
    history = Revision.objects.filter(entity=second, state_field="release_requirement")
    assert [(r.state_from, r.state_to, r.reason) for r in history] == [
        ("as_recorded", "not_required", lifted)
    ]

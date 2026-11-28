"""Low-stock episodes, team and project role grants, and overlapping scans (ADR 0190)."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import World, build_world
from test_amounts import SAMPLE, sample

from iki_server.amounts import stock, stock_alerts
from iki_server.entities.models import Entity, ProjectLink
from iki_server.notifications import kinds, services
from iki_server.notifications.models import Notification
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole

ROLE = "test/stock.manager"


@pytest.fixture
def stock_world(monkeypatch: pytest.MonkeyPatch) -> tuple[World, Entity, dict[UUID, Decimal]]:
    world = build_world()
    item = Entity.objects.get(pk=sample(world, "Stock item")["id"])
    available = {item.pk: Decimal(10)}
    monkeypatch.setattr(stock, "catalogues", lambda: {SAMPLE: SimpleNamespace(notify_roles=[ROLE])})
    monkeypatch.setattr(
        stock, "items", lambda project: Entity.objects.filter(type=SAMPLE, owner_project=project)
    )
    monkeypatch.setattr(
        stock,
        "summaries",
        lambda project, records: [
            SimpleNamespace(id=r.pk, low=available.get(r.pk, Decimal(11)) <= 10) for r in records
        ],
    )
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["viewer"]), role=ROLE
    )
    return world, item, available


@pytest.mark.django_db
def test_low_stock_reaches_eligible_managers_once_until_recovery(
    stock_world: tuple[World, Entity, dict[UUID, Decimal]],
) -> None:
    world, item, available = stock_world
    # A team grant, including someone also holding a direct grant, and a grant in
    # another project that owns no catalogue item. Only this item's project counts.
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(team=world.other_team), role=ROLE
    )
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["mixed"]), role=ROLE
    )
    other = Project.objects.create(team=world.team, name="Other inventory")
    grant = ProjectMembership.objects.create(
        project=other, user=world.users["outsider"], role=ProjectRole.EDITOR
    )
    LabRoleGrant.objects.create(membership=grant, role=ROLE)

    assert stock_alerts.send_alerts() == 3
    notes = Notification.objects.filter(kind=kinds.LOW_STOCK.key)
    assert set(notes.values_list("recipient_id", flat=True)) == {
        world.users[name].pk for name in ("viewer", "via_team", "mixed")
    }
    assert set(notes.values_list("project_id", "entity_id", "path")) == {
        (world.project.pk, item.pk, f"/entities/{item.pk}")
    }
    assert stock_alerts.send_alerts() == 0
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["admin"]), role=ROLE
    )
    assert stock_alerts.send_alerts() == 1
    available[item.pk] = Decimal("10.00001")
    assert stock_alerts.send_alerts() == 0
    available[item.pk] = Decimal(0)
    assert stock_alerts.send_alerts() == 4
    assert notes.count() == 8
    # A project grant alone cannot disclose a record that the person cannot read.
    ProjectLink.objects.filter(project=world.project, entity=item).delete()
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["editor"]), role=ROLE
    )
    assert stock_alerts.send_alerts() == 0
    assert not services.inbox(world.users["viewer"]).filter(kind=kinds.LOW_STOCK.key).exists()


@pytest.mark.django_db(transaction=True)
def test_overlapping_maintenance_passes_deliver_only_one_alert(
    stock_world: tuple[World, Entity, dict[UUID, Decimal]],
) -> None:
    barrier = Barrier(2, timeout=20)

    def scan() -> int:
        try:
            barrier.wait()
            return stock_alerts.send_alerts()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(scan) for _ in range(2)]
        assert sorted(job.result(timeout=30) for job in pending) == [0, 1]
    assert Notification.objects.filter(kind=kinds.LOW_STOCK.key).count() == 1


def _items(world: World, n: int) -> list[Entity]:
    return [Entity.objects.get(pk=sample(world, f"Stock item {i}")["id"]) for i in range(n)]


@pytest.mark.django_db
def test_a_pass_takes_as_many_queries_however_many_items_it_checks(
    stock_world: tuple[World, Entity, dict[UUID, Decimal]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Items are locked, summarized and told about a batch at a time: an item that isn't
    low, or is and its managers were told, costs no queries of its own."""
    world, _, available = stock_world
    monkeypatch.setattr(stock_alerts, "ALERT_BATCH", 50)

    def checked() -> tuple[int, int]:
        with CaptureQueriesContext(connection) as queries:
            sent = stock_alerts.send_alerts()
        return sent, len(queries)

    assert stock_alerts.send_alerts() == 1
    sent, few = checked()
    assert sent == 0
    more = _items(world, 40)
    for item in more[:20]:
        available[item.pk] = Decimal(0)
    assert stock_alerts.send_alerts() == 20
    assert checked() == (0, few)


@pytest.mark.django_db
def test_a_pass_checks_every_item_a_batch_at_a_time_in_id_order(
    stock_world: tuple[World, Entity, dict[UUID, Decimal]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each batch is locked and summarized in ID order, so overlapping passes queue rather
    than wait on each other; an episode ends for the item that recovers, not its batch."""
    world, item, available = stock_world
    monkeypatch.setattr(stock_alerts, "ALERT_BATCH", 3)
    summarized: list[list[UUID]] = []
    summaries = stock.summaries

    def recorded(project: Project, records: list[Entity]) -> list[stock.StockSummary]:
        summarized.append([r.pk for r in records])
        return summaries(project, records)

    monkeypatch.setattr(stock, "summaries", recorded)
    more = _items(world, 7)
    for each in more:
        available[each.pk] = Decimal(0)
    assert stock_alerts.send_alerts() == 8
    assert [len(batch) for batch in summarized] == [3, 3, 2]
    assert [pk for batch in summarized for pk in batch] == sorted([item.pk, *(e.pk for e in more)])
    assert stock_alerts.send_alerts() == 0
    available[more[3].pk] = Decimal(11)
    assert stock_alerts.send_alerts() == 0
    available[more[3].pk] = Decimal(1)
    assert stock_alerts.send_alerts() == 1
    assert Notification.objects.filter(kind=kinds.LOW_STOCK.key).count() == 9


@pytest.mark.django_db(transaction=True)
def test_overlapping_passes_over_many_batches_tell_each_manager_once(
    stock_world: tuple[World, Entity, dict[UUID, Decimal]], monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _, available = stock_world
    monkeypatch.setattr(stock_alerts, "ALERT_BATCH", 2)
    for each in _items(world, 6):
        available[each.pk] = Decimal(0)
    barrier = Barrier(2, timeout=20)

    def scan() -> int:
        try:
            barrier.wait()
            return stock_alerts.send_alerts()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(scan) for _ in range(2)]
        assert sum(job.result(timeout=30) for job in pending) == 7
    notes = Notification.objects.filter(kind=kinds.LOW_STOCK.key)
    assert notes.count() == notes.values("entity_id").distinct().count() == 7

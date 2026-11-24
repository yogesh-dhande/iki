"""Low-stock episodes, team and project role grants, and overlapping scans (ADR 0190)."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.db import connection
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
    monkeypatch.setattr(stock, "catalogues", lambda: {SAMPLE: SimpleNamespace(notify=[ROLE])})
    monkeypatch.setattr(
        stock, "items", lambda project: Entity.objects.filter(type=SAMPLE, owner_project=project)
    )
    monkeypatch.setattr(
        stock, "summary", lambda project, record: SimpleNamespace(low=available[record.pk] <= 10)
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

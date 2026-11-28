"""Asking for a detached plugin's purge (ADR 0092): a dry run, a typed confirmation, a wait."""

import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from org_world import World, build_world, post

from iki_server.audit.models import AuditEvent
from iki_server.entities import registry, services
from iki_server.entities.models import Entity, Tombstone
from iki_server.org import holds
from iki_server.org.services import Refused
from iki_server.plugins import purge
from iki_server.plugins.models import PluginPurge, PluginState

pytestmark = pytest.mark.django_db

PLUGINS = ("acme/dna", "acme/protein", "acme/prediction")


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Installs the fixture plugins as a lockfile would, some of them detached."""

    def deploy(*detached: str) -> None:
        found = {
            "plugins": [
                {
                    "name": name,
                    "state": "detached" if name in detached else "active",
                    "version": "1.0.0",
                    "sha": "a" * 40,
                }
                for name in PLUGINS
            ]
        }
        monkeypatch.setattr(registry, "installed", lambda: found)

    return deploy


def sequences(world: World) -> list[Any]:
    editor = world.users["editor"]
    made = [
        services.create(editor, "acme/dna.DNASequence", world.project, {"bases": bases}).entity
        for bases in ("ACGT", "GGCC")
    ]
    services.create(
        editor, "iki/core.Collection", world.project, {"name": "Plasmids", "members": [made[0].pk]}
    )
    return made


def test_the_dry_run_says_what_would_go_and_what_refers_to_it(
    world: World, deployed: Callable[..., None]
) -> None:
    sequences(world)
    deployed(*PLUGINS)
    found = purge.report("acme/dna")
    # The project's records, and their registry entries (ADR 0115).
    assert (found["entities"], found["entries"]) == (2, 2)
    assert found["types"] == [
        {"type": "acme/dna.DNASequence", "label": "DNA sequences", "count": 2}
    ]
    assert [(p["name"], p["held"]) for p in found["projects"]] == [(world.project.name, False)]
    assert [r["count"] for r in found["references"]] == [1]  # the collection's member


def test_only_a_detached_plugin_not_on_hold_can_be_purged(
    world: World, deployed: Callable[..., None]
) -> None:
    sequences(world)
    deployed()
    assert "isn’t detached" in (purge.why_not("acme/dna") or "")
    deployed(*PLUGINS)
    assert purge.why_not("acme/dna") is None
    hold = holds.place(world.users["org_admin"], world.project, "Case 24-117")
    assert purge.why_not("acme/dna") == (
        f"Some of its data is in {world.project.name}, which is on legal hold. It can be "
        "purged once the hold is released."
    )
    with pytest.raises(Refused, match="legal hold"):
        purge.request(world.users["org_admin"], "acme/dna", "Retention over", "acme/dna")
    holds.release(world.users["org_admin"], hold, reason="Matter resolved")
    assert purge.why_not("acme/dna") is None
    assert purge.why_not("nothing") == "There’s no plugin nothing installed."


def test_an_org_admin_schedules_it_with_a_reason_and_its_name(
    world: World, deployed: Callable[..., None], settings: Any
) -> None:
    sequences(world)
    deployed(*PLUGINS)
    admin = world.users["org_admin"]
    with pytest.raises(PermissionDenied):
        purge.request(world.users["admin"], "acme/dna", "Retention over", "acme/dna")
    with pytest.raises(Refused, match="Type acme/dna to confirm"):
        purge.request(admin, "acme/dna", "Retention over", "ACME/DNA")
    with pytest.raises(Refused, match="Say why"):
        purge.request(admin, "acme/dna", "  ", "acme/dna")

    settings.IKI_PURGE_WAIT = 30 * 86400
    scheduled = purge.request(
        admin, "acme/dna", "Retention of 10 years over; QA approved.", " acme/dna "
    )
    assert scheduled.run_after - timezone.now() > dt.timedelta(days=29, hours=23)
    assert scheduled.report["entities"] == 2
    with pytest.raises(Refused, match="already scheduled"):
        purge.request(admin, "acme/dna", "Again", "acme/dna")
    assert purge.pending("acme/dna") == scheduled

    purge.cancel(admin, scheduled)
    assert purge.pending("acme/dna") is None
    assert PluginPurge.objects.get().cancelled_by == admin
    events = AuditEvent.objects.filter(action__startswith="plugin.purge").order_by("occurred_at")
    assert [(e.action, e.actor, e.data["plugin"]) for e in events] == [
        ("plugin.purge_requested", admin, "acme/dna"),
        ("plugin.purge_cancelled", admin, "acme/dna"),
    ]
    assert events[0].data["entities"] == 2
    assert events[0].data["reason"] == "Retention of 10 years over; QA approved."


def test_the_plugins_page(world: World, deployed: Callable[..., None]) -> None:
    sequences(world)
    deployed(*PLUGINS)
    admin = world.browser("org_admin")
    listing = admin.get("/admin/plugins/").content.decode()
    assert "Detached" in listing
    page = admin.get("/admin/plugins/acme/dna/").content.decode()
    assert "<dt>Works with</dt><dd>Iki <code>&gt;=0.1</code></dd>" in page
    assert (
        '<dt><code>acme.com/owner</code></dt><dd class="preserve-lines">Molecular biology' in page
    )
    assert "x-acme-reviewed" not in page
    assert "What a purge would delete" in page
    assert "2 DNA sequences" in page
    assert "the 2 registry entries they’re records of" in page

    refused = post(
        admin, "/admin/plugins/acme/dna/", {"action": "purge", "reason": "Over", "confirm": "x"}
    )
    assert refused.status_code == 200
    assert "Type acme/dna to confirm." in refused.content.decode()
    made = post(
        admin,
        "/admin/plugins/acme/dna/",
        {"action": "purge", "reason": "Over", "confirm": "acme/dna"},
    )
    assert made.status_code == 302
    assert "Purge scheduled" in admin.get("/admin/plugins/acme/dna/").content.decode()
    assert "Purge on" in admin.get("/admin/plugins/").content.decode()
    cancelled = post(admin, "/admin/plugins/acme/dna/", {"action": "cancel"})
    assert cancelled.status_code == 302
    assert purge.pending("acme/dna") is None

    assert world.browser("auditor").get("/admin/plugins/").status_code == 403
    assert admin.get("/admin/plugins/nothing/").status_code == 404


# --- Once the wait is over -----------------------------------------------------------------


def due(world: World, deployed: Callable[..., None]) -> tuple[list[Any], PluginPurge]:
    made = sequences(world)
    deployed(*PLUGINS)
    scheduled = purge.request(world.users["org_admin"], "acme/dna", "Retention over", "acme/dna")
    PluginPurge.objects.filter(pk=scheduled.pk).update(run_after=timezone.now())
    return made, scheduled


def test_each_entity_leaves_a_tombstone_that_proves_what_it_was(
    world: World, deployed: Callable[..., None]
) -> None:
    made, scheduled = due(world, deployed)
    expected = {e.pk: purge.fingerprint(e, services.current_values(e)) for e in made}
    # Each record and its registry entry.
    assert purge.tombstone_due() == 4
    assert purge.tombstone_due() == 0
    stones = {t.entity_id: t for t in Tombstone.objects.filter(purge=scheduled)}
    assert set(stones) == set(expected) | {e.entry_id for e in made}
    assert {pk: t.content_sha256 for pk, t in stones.items() if pk in expected} == expected
    assert all(e.purged_at is not None for e in Entity.objects.filter(pk__in=expected))
    scheduled.refresh_from_db()
    assert scheduled.tombstoned_at is not None
    event = AuditEvent.objects.get(action="plugin.purge_tombstoned")
    assert event.data == {"plugin": "acme/dna", "entities": 4}
    with pytest.raises(Refused, match="already purged"):
        purge.cancel(world.users["org_admin"], scheduled)


def test_a_purged_entity_is_gone_but_named(world: World, deployed: Callable[..., None]) -> None:
    made, _ = due(world, deployed)
    purge.tombstone_due()
    api = world.browser("viewer")
    hrid = made[0].hrid
    gone = api.get(f"/api/v1/entities/{hrid}")
    assert gone.status_code == 410
    body = gone.json()
    assert body["code"] == "purged"
    assert body["detail"].startswith(f"{hrid} was purged on ")
    assert body["entity"]["hrid"] == hrid
    assert len(body["entity"]["content_sha256"]) == 64
    # It's in no list, and what referred to it still names it.
    listed = api.get("/api/v1/entities", {"project": str(world.project.pk)}).json()["items"]
    assert hrid not in {e["hrid"] for e in listed}
    (collection,) = [e for e in listed if e["type"] == "iki/core.Collection"]
    # Its member is the registry entry, purged with it.
    assert collection["refs"][str(made[0].entry_id)] == {
        "hrid": hrid,
        "title": hrid,
        "type": "acme/dna.DNASequence",
        "archived": False,
        "purged": True,
        "formerly": [],
        "code": None,
        "superseded": None,
    }
    # Outsiders aren't told it existed.
    assert world.browser("outsider").get(f"/api/v1/entities/{hrid}").status_code == 404
    with pytest.raises(Refused, match="was purged"):
        services.set_archived(world.users["editor"], Entity.objects.get(pk=made[0].pk), True)


def test_a_hold_placed_during_the_wait_stops_it(
    world: World, deployed: Callable[..., None]
) -> None:
    due(world, deployed)
    hold = holds.place(world.users["org_admin"], world.project, "Case 24-117")
    assert purge.tombstone_due() == 0
    assert not Tombstone.objects.exists()
    holds.release(world.users["org_admin"], hold, reason="Matter resolved")
    assert purge.tombstone_due() == 4


def test_a_purged_plugin_stays_on_record(world: World) -> None:
    admin = world.users["org_admin"]
    now = timezone.now()
    PluginState.objects.create(name="acme/lims", state="purged", version="2.1.0", sha="b" * 40)
    PluginPurge.objects.create(
        plugin="acme/lims",
        reason="Retention of 10 years over",
        report={
            "plugin": "acme/lims",
            "entities": 3,
            "types": [{"type": "acme/lims.Sample", "label": "Samples", "count": 3}],
            "projects": [],
            "references": [],
        },
        requested_by=admin,
        run_after=now,
        tombstoned_at=now,
        done_at=now,
    )
    browser = world.browser("org_admin")
    listing = browser.get("/admin/plugins/").content.decode()
    assert f"Purged on {now:%Y-%m-%d}" in listing
    page = browser.get("/admin/plugins/acme/lims/").content.decode()
    assert "What the purge deleted" in page
    assert "3 Samples" in page
    assert "Retention of 10 years over" in page
    assert "dropped its tables" in page
    assert "Schedule the purge" not in page
    assert "Cancel the purge" not in page
    again = post(browser, "/admin/plugins/acme/lims/", {"action": "cancel"})
    assert again.status_code == 200
    assert PluginPurge.objects.get().done_at == now

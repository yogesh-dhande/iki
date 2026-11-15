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
from iki_server.org import holds
from iki_server.org.services import Refused
from iki_server.plugins import purge
from iki_server.plugins.models import PluginPurge

pytestmark = pytest.mark.django_db

PLUGINS = ("dna", "protein", "structure_prediction")


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
        services.create(editor, "dna.DNASequence", world.project, {"bases": bases}).entity
        for bases in ("ACGT", "GGCC")
    ]
    services.create(
        editor, "core.Collection", world.project, {"name": "Plasmids", "members": [made[0].pk]}
    )
    return made


def test_the_dry_run_says_what_would_go_and_what_refers_to_it(
    world: World, deployed: Callable[..., None]
) -> None:
    sequences(world)
    deployed(*PLUGINS)
    found = purge.report("dna")
    assert found["entities"] == 2
    assert found["types"] == [{"type": "dna.DNASequence", "label": "DNA sequences", "count": 2}]
    assert [(p["name"], p["held"]) for p in found["projects"]] == [(world.project.name, False)]
    assert [r["count"] for r in found["references"]] == [1]  # the collection's member


def test_only_a_detached_plugin_not_on_hold_can_be_purged(
    world: World, deployed: Callable[..., None]
) -> None:
    sequences(world)
    deployed()
    assert "isn’t detached" in (purge.why_not("dna") or "")
    deployed(*PLUGINS)
    assert purge.why_not("dna") is None
    hold = holds.place(world.users["org_admin"], world.project, "Case 24-117")
    assert purge.why_not("dna") == (
        f"Some of its data is in {world.project.name}, which is on legal hold. It can be "
        "purged once the hold is released."
    )
    with pytest.raises(Refused, match="legal hold"):
        purge.request(world.users["org_admin"], "dna", "Retention over", "dna")
    holds.release(world.users["org_admin"], hold)
    assert purge.why_not("dna") is None
    assert purge.why_not("nothing") == "There’s no plugin nothing installed."


def test_an_org_admin_schedules_it_with_a_reason_and_its_name(
    world: World, deployed: Callable[..., None], settings: Any
) -> None:
    sequences(world)
    deployed(*PLUGINS)
    admin = world.users["org_admin"]
    with pytest.raises(PermissionDenied):
        purge.request(world.users["admin"], "dna", "Retention over", "dna")
    with pytest.raises(Refused, match="Type dna to confirm"):
        purge.request(admin, "dna", "Retention over", "DNA")
    with pytest.raises(Refused, match="Say why"):
        purge.request(admin, "dna", "  ", "dna")

    settings.IKI_PURGE_WAIT = 30 * 86400
    scheduled = purge.request(admin, "dna", "Retention of 10 years over; QA approved.", " dna ")
    assert scheduled.run_after - timezone.now() > dt.timedelta(days=29, hours=23)
    assert scheduled.report["entities"] == 2
    with pytest.raises(Refused, match="already scheduled"):
        purge.request(admin, "dna", "Again", "dna")
    assert purge.pending("dna") == scheduled

    purge.cancel(admin, scheduled)
    assert purge.pending("dna") is None
    assert PluginPurge.objects.get().cancelled_by == admin
    events = AuditEvent.objects.filter(action__startswith="plugin.purge").order_by("occurred_at")
    assert [(e.action, e.actor, e.data["plugin"]) for e in events] == [
        ("plugin.purge_requested", admin, "dna"),
        ("plugin.purge_cancelled", admin, "dna"),
    ]
    assert events[0].data["entities"] == 2
    assert events[0].data["reason"] == "Retention of 10 years over; QA approved."


def test_the_plugins_page(world: World, deployed: Callable[..., None]) -> None:
    sequences(world)
    deployed(*PLUGINS)
    admin = world.browser("org_admin")
    listing = admin.get("/settings/plugins/").content.decode()
    assert "Detached" in listing
    page = admin.get("/settings/plugins/dna/").content.decode()
    assert "What a purge would delete" in page
    assert "2 DNA sequences" in page

    refused = post(
        admin, "/settings/plugins/dna/", {"action": "purge", "reason": "Over", "confirm": "x"}
    )
    assert refused.status_code == 200
    assert "Type dna to confirm." in refused.content.decode()
    made = post(
        admin, "/settings/plugins/dna/", {"action": "purge", "reason": "Over", "confirm": "dna"}
    )
    assert made.status_code == 302
    assert "Purge scheduled" in admin.get("/settings/plugins/dna/").content.decode()
    assert "Purge on" in admin.get("/settings/plugins/").content.decode()
    cancelled = post(admin, "/settings/plugins/dna/", {"action": "cancel"})
    assert cancelled.status_code == 302
    assert purge.pending("dna") is None

    assert world.browser("auditor").get("/settings/plugins/").status_code == 403
    assert admin.get("/settings/plugins/nothing/").status_code == 404

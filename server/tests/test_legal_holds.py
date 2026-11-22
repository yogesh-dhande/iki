"""Legal holds (ADR 0092): a project kept whole for a matter, by Org Admins, in confidence."""

import pytest
from django.core.exceptions import PermissionDenied
from org_world import World, build_world, post

from iki_server.audit.models import AuditEvent
from iki_server.org import holds
from iki_server.org.models import LegalHold
from iki_server.org.services import Refused

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def test_org_admins_place_and_release_holds_and_each_is_audited(world: World) -> None:
    admin = world.users["org_admin"]
    hold = holds.place(admin, world.project, "  Case 24-117 ", "Legal asked, until the case ends.")
    assert (hold.matter, hold.placed_by) == ("Case 24-117", admin)
    assert holds.is_held(world.project)
    # Another matter can hold it too, but not the same one twice.
    other = holds.place(admin, world.project, "FDA inspection")
    with pytest.raises(Refused, match="already on hold for Case 24-117"):
        holds.place(admin, world.project, "Case 24-117")
    with pytest.raises(Refused, match="Say what the hold is for"):
        holds.place(admin, world.project, "   ")

    holds.release(admin, hold)
    assert holds.is_held(world.project)  # the inspection's hold is still on
    holds.release(admin, other)
    holds.release(admin, other)  # releasing twice changes nothing
    assert not holds.is_held(world.project)
    assert list(holds.held_projects()) == []

    events = AuditEvent.objects.filter(action__startswith="legal_hold.").order_by("occurred_at")
    assert [(e.action, e.actor, e.project, e.data) for e in events] == [
        ("legal_hold.placed", admin, world.project, {"matter": "Case 24-117"}),
        ("legal_hold.placed", admin, world.project, {"matter": "FDA inspection"}),
        ("legal_hold.released", admin, world.project, {"matter": "Case 24-117"}),
        ("legal_hold.released", admin, world.project, {"matter": "FDA inspection"}),
    ]


@pytest.mark.parametrize("who", ["admin", "auditor", "viewer"])
def test_only_org_admins_place_or_release_them(world: World, who: str) -> None:
    with pytest.raises(PermissionDenied):
        holds.place(world.users[who], world.project, "Case 24-117")
    hold = holds.place(world.users["org_admin"], world.project, "Case 24-117")
    with pytest.raises(PermissionDenied):
        holds.release(world.users[who], hold)


def test_the_page_lists_them_for_auditors_and_lets_org_admins_change_them(world: World) -> None:
    admin = world.browser("org_admin")
    placed = post(
        admin,
        "/admin/legal-holds/",
        {"action": "place", "project": str(world.project.pk), "matter": "Case 24-117"},
    )
    assert placed.status_code == 302
    hold = LegalHold.objects.get()
    page = admin.get("/admin/legal-holds/").content.decode()
    assert "Case 24-117" in page
    assert f"Release the hold on {world.project.name} for Case 24-117" in page

    auditor = world.browser("auditor").get("/admin/legal-holds/").content.decode()
    assert "Case 24-117" in auditor
    assert "Release" not in auditor
    assert "Put a project on hold" not in auditor
    refused = post(
        world.browser("auditor"),
        "/admin/legal-holds/",
        {"action": "release", "hold": str(hold.pk)},
    )
    assert refused.status_code == 403

    released = post(admin, "/admin/legal-holds/", {"action": "release", "hold": str(hold.pk)})
    assert released.status_code == 302
    assert not holds.is_held(world.project)


def test_people_in_the_project_arent_told(world: World) -> None:
    holds.place(world.users["org_admin"], world.project, "Case 24-117")
    for who in ("admin", "viewer"):
        api = world.browser(who)
        body = api.get(f"/api/v1/projects/{world.project.pk}").content.decode()
        assert "hold" not in body.lower()
        assert api.get("/admin/legal-holds/").status_code == 403

"""Instrument time booked for someone, and for the work it's for (iki/equipment).

A core's coordinator books its sequencer for the analyst who'll run a request: the
booking is on the analyst's home, among the coordinator's bookings made for others, and
on the request's lineage, and both hear when the instrument goes out of service. A
booking names only a record its project may use, and shows it only to those who can read
it, as Google Calendar and Outlook put a room booked for someone on their calendar and
show a delegate the meetings they arranged."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from api_client import Api
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import World, build_world
from service_reviews import serve
from test_plugin_migrations import database, django, iki, migrate, sync
from test_states import roles

from iki_server.audit.models import AuditEvent
from iki_server.entities import requests, services, states
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.notifications.models import Notification
from iki_server.org.models import Project, ProjectMembership, ProjectRole

__all__ = ["database"]

PLUGIN = PLUGINS / "equipment"
EQUIPMENT = "iki/equipment."
BOOKING = EQUIPMENT + "Booking"
PLAIN = "iki/requests.Request"
REQUESTER = "outsider"
#: The people of the core (the world's project): its coordinator, an analyst who runs
#: its instruments, an engineer who looks after them, and a member with no bookings.
COORDINATOR, ANALYST, ENGINEER, BYSTANDER = "admin", "editor", "runner", "viewer"


def test_a_booking_is_for_a_person_and_the_work_it_runs(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    # Equipment doesn't need requests; a core that books its instruments for requests has both.
    plugins = build_order(PLUGIN, PLUGIN.parent / "requests")
    built = iki("plugin", "build", *plugins, "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_equipment_bookings import exercise; exercise()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def _make(world: World, person: str, type_name: str, project: Project, **values: Any) -> Entity:
    saved = services.create(world.users[person], type_name, project, values)
    return Entity.objects.get(pk=saved.entity.pk)


def _shown(world: World, person: str, component: str, place: dict[str, Any]) -> list[Any]:
    path = f"/components/{EQUIPMENT}{component}/entities"
    answer = Api(world.browser(person)).post(path, place)
    assert answer.status_code == 200, answer.content
    items: list[dict[str, Any]] = answer.json()["items"]
    return items


def _hrids(items: list[dict[str, Any]]) -> set[str]:
    return {item["hrid"] for item in items}


def exercise() -> None:
    world = build_world()
    core = world.project
    users = world.users
    asking = Project.objects.create(team=world.team, name="Microbiome cohort")
    ProjectMembership.objects.create(project=asking, user=users[REQUESTER], role=ProjectRole.EDITOR)
    roles(
        world,
        admin=("iki/lab_roles.coordinator",),
        editor=("iki/lab_roles.analyst",),
        runner=("iki/lab_roles.engineer",),
    )
    requests.set_intake(
        core, types=[PLAIN], from_registry=False, sources=[asking], by=users[COORDINATOR]
    )
    serve(users["admin"], core)
    request = _make(world, REQUESTER, PLAIN, asking, title="16S rRNA V4 amplicon panel")
    requests.submit(users[REQUESTER], request, core, expected_revision=request.revision)
    request.refresh_from_db()

    sequencer = _make(world, COORDINATOR, EQUIPMENT + "Instrument", core, name="MiSeq")
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)

    def book(person: str, purpose: str, hours: int, **values: Any) -> Entity:
        return _make(
            world,
            person,
            BOOKING,
            core,
            purpose=purpose,
            instrument=str(sequencer.pk),
            starts_at=(start + timedelta(hours=hours)).isoformat(),
            ends_at=(start + timedelta(hours=hours + 2)).isoformat(),
            **values,
        )

    # The coordinator books the run for the analyst, for the request it runs; the time
    # she books for herself, and the analyst's own, say no one.
    for_analyst = book(
        COORDINATOR,
        "16S amplicon run",
        0,
        booked_by=str(users[ANALYST].pk),
        work=str(request.pk),
    )
    hers = book(COORDINATOR, "Flow cell wash", 3)
    theirs = book(ANALYST, "Library QC", 6)
    named_self = book(ANALYST, "Pooled library run", 9, booked_by=str(users[ANALYST].pk))

    _homes(world, request, for_analyst, hers, theirs, named_self, start)
    _what_it_names(world, core, request, sequencer, for_analyst, start)
    _lineage(world, request, for_analyst)
    called_off, rerun = _cancelled_frees_its_slot(world, book)
    _out_of_service(world, sequencer, for_analyst, hers, {theirs, named_self, rerun}, called_off)


def _homes(
    world: World,
    request: Entity,
    for_analyst: Entity,
    hers: Entity,
    theirs: Entity,
    named_self: Entity,
    start: datetime,
) -> None:
    home = {"home": True}
    # Your bookings: the time booked for you, whoever booked it.
    analysts = _shown(world, ANALYST, "booking_calendar", home)
    assert _hrids(analysts) == {for_analyst.hrid, theirs.hrid, named_self.hrid}
    assert _hrids(_shown(world, COORDINATOR, "booking_calendar", home)) == {hers.hrid}
    assert _shown(world, BYSTANDER, "booking_calendar", home) == []
    # Bookings you made for others: each with whom it's for, and what.
    (made,) = _shown(world, COORDINATOR, "bookings_made", home)
    analyst = str(world.users[ANALYST].pk)
    assert made["hrid"] == for_analyst.hrid
    assert made["values"]["booked_by"] == analyst
    assert made["people"] == {analyst: {"name": world.users[ANALYST].name}}
    assert made["values"]["work"] == str(request.pk)
    assert made["refs"][str(request.pk)]["hrid"] == request.hrid
    assert _shown(world, ANALYST, "bookings_made", home) == []
    # The analyst sees what theirs is for too: the request, linked into the core.
    (mine,) = [i for i in analysts if i["hrid"] == for_analyst.hrid]
    assert mine["refs"][str(request.pk)]["title"] == "16S rRNA V4 amplicon panel"
    # A month of it, as the calendar asks, only narrows it.
    later = (datetime.now(UTC) + timedelta(days=40)).isoformat()
    month = {"where": {"field": "starts_at", "op": "gte", "value": later}}
    assert _shown(world, ANALYST, "booking_calendar", {**home, "refine": month}) == []
    # The calendar's days find every booking on them: one that started before the first
    # and ends after it, and one that starts during them.
    days = {
        "where": {
            "all": [
                {
                    "field": "starts_at",
                    "op": "lt",
                    "value": (start + timedelta(hours=7)).isoformat(),
                },
                {"field": "ends_at", "op": "gt", "value": (start + timedelta(hours=1)).isoformat()},
            ]
        }
    }
    on_them = _shown(world, ANALYST, "booking_calendar", {**home, "refine": days})
    assert _hrids(on_them) == {for_analyst.hrid, theirs.hrid}


def _what_it_names(
    world: World,
    core: Project,
    request: Entity,
    sequencer: Entity,
    for_analyst: Entity,
    start: datetime,
) -> None:
    # A record the core may not use can't be named, though the coordinator reads it.
    blinded = Project.objects.create(team=world.team, name="Blinded study", allow_sharing=False)
    ProjectMembership.objects.create(
        project=blinded, user=world.users[COORDINATOR], role=ProjectRole.EDITOR
    )
    cohort = _make(world, COORDINATOR, "iki/core.Collection", blinded, name="Blinded cohort")
    with pytest.raises(Invalid) as refused:
        _make(
            world,
            COORDINATOR,
            BOOKING,
            core,
            purpose="Blinded cohort run",
            instrument=str(sequencer.pk),
            starts_at=(start + timedelta(days=2)).isoformat(),
            ends_at=(start + timedelta(days=2, hours=2)).isoformat(),
            work=str(cohort.pk),
        )
    assert "work" in refused.value.errors, refused.value.errors
    # Shared into another project, the booking shows there without the request, which
    # its readers there can't read: not its HRID, nor its title.
    methods = Project.objects.create(team=world.team, name="Methods review")
    ProjectMembership.objects.create(
        project=methods, user=world.users["team_member"], role=ProjectRole.VIEWER
    )
    assert services.link(world.users[COORDINATOR], for_analyst, methods)
    (shared,) = _shown(world, "team_member", "booking_calendar", {"project": str(methods.pk)})
    assert shared["hrid"] == for_analyst.hrid
    assert str(request.pk) not in shared["refs"]
    said = json.dumps(shared)
    assert request.hrid not in said and "amplicon panel" not in said
    page = Api(world.browser("team_member")).get(f"/entities/{for_analyst.pk}")
    assert page.status_code == 200, page.content
    assert str(request.pk) not in page.json()["refs"]
    assert request.hrid not in page.content.decode()
    assert _shown(world, "team_member", "booking_calendar", {"home": True}) == []


def _lineage(world: World, request: Entity, for_analyst: Entity) -> None:
    """The request's lineage lists its booking for the core's people, by the field "For";
    the project that asked can't read the core's bookings, and isn't shown them."""

    def downstream(person: str) -> list[Any]:
        answer = Api(world.browser(person)).get(f"/entities/{request.pk}/downstream")
        assert answer.status_code == 200, answer.content
        items: list[dict[str, Any]] = answer.json()["items"]
        return items

    (found,) = [i for i in downstream(ANALYST) if i["entity"]["id"] == str(for_analyst.pk)]
    assert [(s["kind"], s["field"]) for s in found["path"]] == [("reference", "work")]
    assert str(for_analyst.pk) not in {i["entity"]["id"] for i in downstream(REQUESTER)}


def _cancelled_frees_its_slot(world: World, book: Callable[..., Entity]) -> tuple[Entity, Entity]:
    """A cancelled booking is kept, holds nothing, and a calendar asks for it only when
    it's to be shown: the analyst's home leaves it out until then."""
    analyst = str(world.users[ANALYST].pk)
    called_off = book(COORDINATOR, "Shotgun metagenomics run", 12, booked_by=analyst)
    saved = services.update(
        world.users[COORDINATOR],
        called_off,
        {"cancelled": True},
        expected_revision=called_off.revision,
    )
    called_off = Entity.objects.get(pk=saved.entity.pk)
    # Its time is free: the analyst books it again.
    rerun = book(ANALYST, "Shotgun metagenomics rerun", 12)
    with pytest.raises(Invalid) as overlapping:
        book(ANALYST, "Overlapping run", 13)
    assert "booked then already" in str(overlapping.value), overlapping.value
    active = {"where": {"field": "cancelled", "op": "eq", "value": False}}
    home = {"home": True}
    everything = _hrids(_shown(world, ANALYST, "booking_calendar", home))
    assert {called_off.hrid, rerun.hrid} <= everything
    shown = _hrids(_shown(world, ANALYST, "booking_calendar", {**home, "refine": active}))
    assert shown == everything - {called_off.hrid}
    return called_off, rerun


def _out_of_service(
    world: World,
    sequencer: Entity,
    for_analyst: Entity,
    hers: Entity,
    theirs: set[Entity],
    called_off: Entity,
) -> None:
    """Whom a booking is for, and whoever booked it, hear of it: each once. A cancelled
    booking tells no one."""
    states.set_state(
        world.users[ENGINEER],
        sequencer,
        "service",
        "out_of_service",
        expected_revision=sequencer.revision,
        reason="Fluidics fault on the flow cell clamp",
    )
    kind = BOOKING + ".instrument_down"
    users = world.users

    def told(booking: Entity) -> set[str]:
        (event,) = AuditEvent.objects.filter(action="notification.sent", target_id=str(booking.pk))
        assert event.data["kind"] == kind, event.data
        return {person["id"] for person in event.data["told"]}

    expected = {
        for_analyst: {users[ANALYST], users[COORDINATOR]},
        hers: {users[COORDINATOR]},
        **{booking: {users[ANALYST]} for booking in theirs},
    }
    for booking, people in expected.items():
        assert told(booking) == {str(p.pk) for p in people}, (booking.hrid, told(booking))
    # Each finds it in their inbox; the engineer who flagged it, nothing.
    assert not AuditEvent.objects.filter(
        action="notification.sent", target_id=str(called_off.pk)
    ).exists()
    for person in (ANALYST, COORDINATOR):
        inbox = Notification.objects.filter(recipient=users[person], kind=kind)
        assert inbox.exists(), person
    assert not Notification.objects.filter(recipient=users[ENGINEER], kind=kind).exists()

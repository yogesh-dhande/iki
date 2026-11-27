"""A count identifies a shared vendor label by its recorded slot or a reviewed choice."""

from dataclasses import dataclass
from typing import Any

import pytest
from api_client import Api, problem, seen
from org_world import World
from test_storage import box, contents, moved, tube, world

from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.storage.models import Event

pytestmark = pytest.mark.django_db

__all__ = ["world"]

LABEL = "VEND-SHARED-07"
UNAVAILABLE = (
    "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer."
)


@dataclass
class Count:
    api: Api
    path: str
    box: dict[str, Any]
    first: dict[str, Any]
    second: dict[str, Any]
    owners: list[Project]


@pytest.fixture
def count(world: World) -> Count:
    registrar = Api(world.browser("editor"))
    counter = Api(world.browser("admin"))
    container = box(registrar, world.project, "Service box", rows=1, columns=4)
    owners = [Project.objects.create(team=world.team, name=f"Owner {n}") for n in (1, 2)]
    tubes = []
    for project in owners:
        ProjectMembership.objects.create(
            project=project, user=world.users["editor"], role=ProjectRole.EDITOR
        )
        ProjectMembership.objects.create(
            project=project, user=world.users["admin"], role=ProjectRole.VIEWER
        )
        specimen = tube(registrar, project, LABEL)
        linked = registrar.put(f"/entities/{specimen['id']}/projects/{world.project.pk}")
        assert linked.status_code == 201, linked.content
        tubes.append(specimen)
    first, second = tubes
    moved(registrar, (first, container, "A1"), (second, container, "A2"))
    started = counter.post(f"/storage/containers/{container['id']}/reconciliations")
    assert started.status_code == 201, started.content
    return Count(
        counter, f"/reconciliations/{started.json()['id']}", container, first, second, owners
    )


def test_shared_labels_at_their_recorded_positions_do_not_produce_a_move_or_removal(
    count: Count,
) -> None:
    api, path = count.api, count.path
    original = contents(api, count.box)
    assert original == {"A1": count.first["hrid"], "A2": count.second["hrid"]}
    original_events = list(Event.objects.order_by("pk").values_list("pk", flat=True))

    # The consequential case starts by scanning A2, where both records match the
    # label but only the second tube is recorded. The first tube is still unchecked.
    second = api.post(f"{path}/scans", {"scans": [{"position": "A2", "code": LABEL}]})
    assert second.status_code == 200, second.content
    findings = {line["entity"]: line for line in api.get(path).json()["lines"]}
    assert (findings[count.second["id"]]["finding"], findings[count.second["id"]]["found"]) == (
        "matched",
        "A2",
    )
    assert (findings[count.first["id"]]["finding"], findings[count.first["id"]]["found"]) == (
        "unchecked",
        None,
    )

    # A rack file can report the very same vendor label at both positions. The
    # repeat of A2 reviews its first observation; A1 is a fresh scan.
    observed = findings[count.second["id"]]
    rack = api.post(
        f"{path}/scans",
        {
            "scans": [{"position": "A1", "code": LABEL}, {"position": "A2", "code": LABEL}],
            "reviewed_lines": [{"line_id": observed["id"], "revision": observed["revision"]}],
        },
    )
    assert rack.status_code == 200, rack.content
    reviewed = api.get(path).json()
    assert {
        (row["entity"], row["expected"], row["found"], row["finding"]) for row in reviewed["lines"]
    } == {(count.first["id"], "A1", "A1", "matched"), (count.second["id"], "A2", "A2", "matched")}
    assert all(row["observation"]["code"] == LABEL for row in reviewed["lines"])
    assert {row["observation"]["identity_basis"] for row in reviewed["lines"]} == {"position"}
    evidence = api.get(f"{path}/history").json()["items"]
    assert {(e["line_id"], e["after"]["identity_basis"]) for e in evidence} == {
        (row["id"], "position") for row in reviewed["lines"]
    }

    done = api.post(f"{path}/complete", {}, headers=seen(reviewed["revision"]))
    assert done.status_code == 200, done.content
    assert done.json()["status"] == "completed" and contents(api, count.box) == original
    assert list(Event.objects.order_by("pk").values_list("pk", flat=True)) == original_events
    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=count.box["id"])
    assert audit.data["moved"] == 0 and audit.data["findings"]["matched"] == 2


def test_a_shared_label_at_an_unrecorded_position_needs_an_explicit_choice(
    count: Count, world: World
) -> None:
    api, path = count.api, count.path
    unrelated = tube(api, world.project, "DIFFERENT-LABEL")
    scan = {"position": "A3", "code": LABEL}
    companion = {"position": "A4", "code": None}
    original = api.get(path).json()
    placements = contents(api, count.box)
    movements = list(Event.objects.order_by("pk").values_list("pk", flat=True))
    audits = AuditEvent.objects.count()

    refused = api.post(f"{path}/scans", {"scans": [companion, scan]})
    assert refused.status_code == 409, refused.content
    assert refused.json()["code"] == "scan_ambiguous" and refused.json()["index"] == 1
    candidates = refused.json()["candidates"]
    assert {
        (c["id"], c["hrid"], c["owner_project"]["id"], c["box"]["id"], c["position"])
        for c in candidates
    } == {
        (count.first["id"], count.first["hrid"], str(count.owners[0].pk), count.box["id"], "A1"),
        (count.second["id"], count.second["hrid"], str(count.owners[1].pk), count.box["id"], "A2"),
    }
    invalid = api.post(f"{path}/scans", {"scans": [companion, {**scan, "choice": unrelated["id"]}]})
    assert invalid.status_code == 409, invalid.content
    assert invalid.json()["code"] == "scan_unavailable" and "candidates" not in invalid.json()
    assert api.get(path).json() == original
    assert api.get(f"{path}/history").json()["items"] == []
    assert contents(api, count.box) == placements
    assert list(Event.objects.order_by("pk").values_list("pk", flat=True)) == movements
    assert AuditEvent.objects.count() == audits

    accepted = api.post(
        f"{path}/scans", {"scans": [companion, {**scan, "choice": count.second["id"]}]}
    )
    assert accepted.status_code == 200, accepted.content
    latest = api.get(path).json()
    lines = {line["entity"]: line for line in latest["lines"]}
    assert latest["revision"] == original["revision"] + 1
    assert lines[count.first["id"]]["finding"] == "unchecked"
    assert (lines[count.second["id"]]["finding"], lines[count.second["id"]]["found"]) == (
        "wrong_slot",
        "A3",
    )
    chosen = lines[count.second["id"]]
    assert chosen["observation"]["identity_basis"] == "choice"
    assert lines[None]["finding"] == "empty" and lines[None]["found"] == "A4"
    evidence = api.get(f"{path}/history", line_id=chosen["id"]).json()["items"]
    assert len(evidence) == 1 and evidence[0]["after"]["identity_basis"] == "choice"
    assert contents(api, count.box) == placements

    # Knowing which record was observed does not authorize replacing the first
    # observation. The operator must also review its line revision and explain the move.
    recheck = {"position": "A2", "code": LABEL, "choice": count.second["id"]}
    correction = {"scans": [recheck], "reason": "Rechecked the tube at A2"}
    stale = api.post(f"{path}/scans", correction)
    assert problem(stale, 412, "stale")["affected_lines"] == [
        {"line_id": chosen["id"], "revision": chosen["revision"]}
    ]
    assert api.get(path).json() == latest
    saved = api.post(
        f"{path}/scans", {**correction, "reviewed_lines": stale.json()["affected_lines"]}
    )
    assert saved.status_code == 200, saved.content
    current = next(row for row in saved.json()["lines"] if row["entity"] == count.second["id"])
    assert current["finding"] == "matched" and current["found"] == "A2"


def test_the_chosen_tube_must_still_have_the_scanned_label(count: Count, world: World) -> None:
    api, path = count.api, count.path
    scan = {"position": "A3", "code": LABEL}
    conflict = api.post(f"{path}/scans", {"scans": [scan]})
    assert conflict.status_code == 409
    assert count.second["id"] in {item["id"] for item in conflict.json()["candidates"]}

    # The owner updates the tube between the operator seeing the list and submitting
    # the choice. The other candidate now has the only matching label.
    registrar = Api(world.browser("editor"))
    entity = f"/entities/{count.second['id']}"
    updated = registrar.patch(
        entity,
        {"values": {"barcode": "REPLACEMENT"}, "reason": "Read the replacement label"},
        headers={"If-Match": registrar.get(entity)["ETag"]},
    )
    assert updated.status_code == 200, updated.content
    before = api.get(path).json()
    rejected = api.post(f"{path}/scans", {"scans": [{**scan, "choice": count.second["id"]}]})
    assert rejected.status_code == 409, rejected.content
    assert rejected.json()["code"] == "scan_unavailable" and "candidates" not in rejected.json()
    assert api.get(path).json() == before and api.get(f"{path}/history").json()["items"] == []


def test_a_checked_out_reservation_does_not_break_a_shared_label_tie(count: Count) -> None:
    api, path = count.api, count.path
    version = api.get(path).json()["revision"]
    closed = api.post(f"{path}/cancel", {"reason": "Start after check-out"}, headers=seen(version))
    assert closed.status_code == 200
    held = api.post("/storage/check-out", {"records": [count.first["id"]], "reason": "In use"})
    assert held.status_code == 200, held.content
    started = api.post(f"/storage/containers/{count.box['id']}/reconciliations")
    assert started.status_code == 201, started.content
    opened = started.json()
    path = f"/reconciliations/{opened['id']}"
    reserved = next(row for row in opened["lines"] if row["entity"] == count.first["id"])
    assert reserved["expected"] == "A1" and reserved["finding"] == "checked_out"

    rejected = api.post(f"{path}/scans", {"scans": [{"position": "A1", "code": LABEL}]})
    assert rejected.status_code == 409, rejected.content
    assert rejected.json()["code"] == "scan_ambiguous"
    assert {item["id"] for item in rejected.json()["candidates"]} == {
        count.first["id"],
        count.second["id"],
    }
    assert api.get(path).json() == opened and api.get(f"{path}/history").json()["items"] == []


def test_a_choice_from_another_slot_cannot_override_its_matching_occupant(count: Count) -> None:
    api, path = count.api, count.path
    before = api.get(path).json()
    inventory = contents(api, count.box)
    audits = AuditEvent.objects.count()
    movements = Event.objects.count()

    listed = api.post(f"{path}/scans", {"scans": [{"position": "A3", "code": LABEL}]})
    assert listed.status_code == 409, listed.content
    choices = {item["id"] for item in listed.json()["candidates"]}
    assert choices == {count.first["id"], count.second["id"]}

    # The operator changes the position after seeing the two choices. A2 already
    # has a readable occupant whose vendor label matches the observation.
    scan = {"position": "A2", "code": LABEL}
    wrong = api.post(f"{path}/scans", {"scans": [{**scan, "choice": count.first["id"]}]})
    assert wrong.status_code == 409, wrong.content
    assert wrong.json()["code"] == "scan_unavailable" and "candidates" not in wrong.json()
    assert wrong.json()["detail"] == UNAVAILABLE
    assert api.get(path).json() == before and api.get(f"{path}/history").json()["items"] == []
    assert contents(api, count.box) == inventory
    assert AuditEvent.objects.count() == audits and Event.objects.count() == movements

    accepted = api.post(f"{path}/scans", {"scans": [{**scan, "choice": count.second["id"]}]})
    assert accepted.status_code == 200, accepted.content
    lines = {row["entity"]: row for row in accepted.json()["lines"]}
    assert lines[count.second["id"]]["finding"] == "matched"
    assert lines[count.first["id"]]["finding"] == "unchecked"


def test_a_partial_count_needs_positive_identity_away_from_a_matching_readable_slot(
    count: Count, world: World
) -> None:
    owner, counter = Api(world.browser("editor")), Api(world.browser("via_team"))
    version = owner.get(count.path).json()["revision"]
    closed = owner.post(
        f"{count.path}/cancel", {"reason": "Count after stocking A3"}, headers=seen(version)
    )
    assert closed.status_code == 200
    third = tube(owner, world.project, "SERVICE-TUBE")
    moved(owner, (third, count.box, "A3"))
    started = counter.post(f"/storage/containers/{count.box['id']}/reconciliations")
    assert started.status_code == 201, started.content
    path = f"/reconciliations/{started.json()['id']}"

    # The owner ends sharing of the A1 tube. The counter uses only identifiers
    # of records that are still visible from the service project.
    unlinked = owner.delete(f"/entities/{count.first['id']}/projects/{world.project.pk}")
    assert unlinked.status_code == 204, unlinked.content
    opened = counter.get(path).json()
    rows = {row["expected"]: row for row in opened["lines"]}
    assert rows["A1"]["ref"] is None
    visible = rows["A2"]
    assert visible["entity"] == count.second["id"] and visible["code"] == LABEL
    assert rows["A3"]["entity"] == third["id"]
    inventory = contents(owner, count.box)
    audits = AuditEvent.objects.count()
    movements = Event.objects.count()
    label = {"code": visible["code"]}
    choice = {**label, "choice": visible["entity"]}
    strict = {"code": visible["ref"]["hrid"], "mode": "iki_id"}
    third_label = {"code": rows["A3"]["code"]}
    for position, data in (
        ("A4", label),
        ("A4", choice),
        ("A3", label),
        ("A3", choice),
        ("A2", third_label),
        ("A4", {**third_label, "choice": rows["A3"]["entity"]}),
        ("A1", label),
        ("A1", choice),
        ("A1", strict),
    ):
        refused = counter.post(f"{path}/scans", {"scans": [{"position": position, **data}]})
        assert refused.status_code == 409, (position, data, refused.content)
        assert refused.json()["code"] == "scan_unavailable" and "candidates" not in refused.json()
        assert refused.json()["detail"] == UNAVAILABLE
    assert (
        counter.get(path).json() == opened and counter.get(f"{path}/history").json()["items"] == []
    )
    assert contents(owner, count.box) == inventory
    assert AuditEvent.objects.count() == audits and Event.objects.count() == movements

    accepted = counter.post(
        f"{path}/scans",
        {
            "scans": [
                {"position": "A2", **label},
                {"position": "A4", "code": rows["A3"]["ref"]["hrid"], "mode": "iki_id"},
            ]
        },
    )
    assert accepted.status_code == 200, accepted.content
    observed = {row["entity"]: row for row in counter.get(path).json()["lines"]}
    assert observed[count.second["id"]]["finding"] == "matched"
    assert observed[count.second["id"]]["observation"]["identity_basis"] == "position"
    assert (
        observed[third["id"]]["found"] == "A4" and observed[third["id"]]["finding"] == "wrong_slot"
    )
    assert observed[third["id"]]["observation"]["identity_basis"] == "iki_id"

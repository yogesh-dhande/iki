"""Where material is kept (ADR 0180): freezers, racks and boxes, each position valid and held
by one record; moves, all or none, each with who, when and why; custody; receiving a
request's tubes against what it expected; reconciling a box by scan; and draws from a tube,
linked to the sample and the experiment it went into."""

from typing import Any

import pytest
from api_client import Api
from django.db import DatabaseError, IntegrityError, connection, transaction
from org_world import World, build_world
from test_api_entities import create
from test_requests import ANALYST, ASK, QA, SAMPLE, Lab, _member, state, submit, takes

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity
from iki_server.graph.models import Edge
from iki_server.notifications.models import Notification
from iki_server.org import permissions
from iki_server.org.models import LabRoleGrant, Project, ProjectRole
from iki_server.storage import labels, services
from iki_server.storage.grids import NotAPosition, Shape
from iki_server.storage.models import Event, Place

pytestmark = pytest.mark.django_db

FREEZER, RACK, BOX, TUBE = (f"acme/protein.{t}" for t in ("Freezer", "Rack", "Box", "Tube"))


def made(api: Api, project: Project, type_name: str, values: dict[str, Any], **kw: Any) -> Any:
    answer = create(api, project, values, type_name, **kw)
    assert answer.status_code == 201, answer.content
    return answer.json()


def errors(response: Any) -> dict[str, str]:
    assert response.status_code == 422, response.content
    return {e["pointer"]: e["detail"] for e in response.json()["errors"]}


def moves(
    api: Api, *items: tuple[Any, Any, str | None], reason: str = "Reorganised", **kw: Any
) -> Any:
    return api.post(
        "/storage/moves",
        {
            "items": [
                {"record": r["id"], "to": t["id"] if t else None, "position": p}
                for r, t, p in items
            ],
            "reason": reason,
            **kw,
        },
    )


def moved(api: Api, *items: tuple[Any, Any, str | None], **kw: Any) -> Any:
    answer = moves(api, *items, **kw)
    assert answer.status_code == 200, answer.content
    return answer.json()


def contents(api: Api, container: Any) -> dict[str, str]:
    answer = api.get(f"/storage/containers/{container['id']}")
    assert answer.status_code == 200, answer.content
    return {o["position"]: o["ref"]["hrid"] for o in answer.json()["contents"] if o["ref"]}


@pytest.fixture
def world() -> World:
    return build_world()


def box(api: Api, project: Project, name: str = "Box 1", rows: int = 9, columns: int = 9) -> Any:
    return made(api, project, BOX, {"name": name, "rows": rows, "columns": columns})


def tube(api: Api, project: Project, barcode: str, **values: Any) -> Any:
    amount = values.pop("amount", None)
    return made(
        api,
        project,
        TUBE,
        {"barcode": barcode, **values},
        **({"amount": amount} if amount is not None else {}),
    )


# --- Geometry -----------------------------------------------------------------------------------


def test_a_grid_names_its_positions_and_refuses_what_isnt_one() -> None:
    plate = Shape(8, 12)
    assert [plate.label(*p) for p in list(plate.positions())[:3]] == ["A1", "A2", "A3"]
    assert plate.label(8, 12) == "H12" and plate.size == 96
    assert plate.parse(" b07 ") == (2, 7)
    # Rows go on past Z as a spreadsheet's columns do.
    assert Shape(32, 48).label(27, 1) == "AA1"
    assert Shape(32, 48).parse("AF48") == (32, 48)
    with pytest.raises(NotAPosition, match=r"I1 isn.t a position: they.re A1 to H12\."):
        plate.parse("I1")
    with pytest.raises(NotAPosition, match="A13 isn't a position"):
        plate.parse("A13")
    with pytest.raises(NotAPosition, match="they're A1 to H12"):
        plate.parse("13")
    # A 9 x 9 box numbered 1 to 81, along each row or down each column.
    by_row = Shape(9, 9, labels="number")
    assert by_row.label(1, 2) == "2" and by_row.label(2, 1) == "10"
    assert by_row.parse("81") == (9, 9)
    by_column = Shape(9, 9, labels="number", order="by_column")
    assert by_column.label(1, 2) == "10" and by_column.label(2, 1) == "2"
    assert [by_column.at(i) for i in range(3)] == [(1, 1), (2, 1), (3, 1)]
    with pytest.raises(NotAPosition, match="they're numbered 1 to 81"):
        by_row.parse("82")
    with pytest.raises(NotAPosition):
        by_row.parse("A1")


def test_a_box_takes_its_shape_as_its_made_and_each_position_holds_one(world: World) -> None:
    api = Api(world.browser("editor"))
    small = box(api, world.project, rows=2, columns=3)
    a, b, c = (tube(api, world.project, f"FX{i}") for i in range(3))
    detail = api.get(f"/storage/containers/{small['id']}").json()
    assert detail["shape"] == {
        "rows": 2,
        "columns": 3,
        "labels": "letter_number",
        "order": "by_row",
        "size": 6,
    }
    assert detail["free"] == 6 and detail["can"]["keep"] is True
    moved(api, (a, small, "B3"), (b, small, None), (c, small, None))
    # Given a position, it's that one; otherwise the next free ones in the grid's order.
    assert contents(api, small) == {"A1": b["hrid"], "A2": c["hrid"], "B3": a["hrid"]}
    # A position outside the grid, or one that's taken, is refused, saying why.
    d = tube(api, world.project, "FX3")
    assert errors(moves(api, (d, small, "C1"))) == {
        "#/items/0": f"{small['hrid']}: C1 isn't a position: they're A1 to B3."
    }
    assert errors(moves(api, (d, small, "a1"))) == {
        "#/items/0": f"{small['hrid']} has something at A1 already."
    }
    # Its shape can't change once it's made, so nothing in it is ever out of its grid.
    changed = api.patch(
        f"/entities/{small['id']}",
        {"values": {"rows": 1}},
        headers={"If-Match": api.get(f"/entities/{small['id']}")["ETag"]},
    )
    assert changed.status_code == 422, changed.content
    # And the database holds both, whatever writes to it.
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            'UPDATE iki_storage_place SET "row" = 1, "column" = 1 WHERE entity_id = %s',
            [a["id"]],
        )
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('UPDATE iki_storage_place SET "row" = 3 WHERE entity_id = %s', [a["id"]])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_storage_event SET reason = ''")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('UPDATE iki_storage_container SET "rows" = 9')


def test_a_freezer_holds_racks_and_boxes_and_a_box_says_where_it_is(world: World) -> None:
    api = Api(world.browser("editor"))
    freezer = made(api, world.project, FREEZER, {"name": "Freezer 3", "asset_tag": "AT-0003"})
    rack = made(api, world.project, RACK, {"name": "Rack A"})
    cryo = box(api, world.project, "Plasmid preps")
    sample = tube(api, world.project, "TB-0001")
    moved(api, (rack, freezer, None), (cryo, rack, "B2"), (sample, cryo, "C4"))
    where = api.get(f"/entities/{sample['id']}/location").json()
    assert [(s["hrid"], s["position"]) for s in where["path"]] == [
        (freezer["hrid"], None),
        (rack["hrid"], "B2"),
        (cryo["hrid"], "C4"),
    ]
    assert where["position"] == "C4" and where["stored"] and not where["hidden"]
    # The freezer is one of the places to start from; the rack and box are in it.
    places = api.get("/storage/places").json()
    assert [p["hrid"] for p in places] == [freezer["hrid"]]
    assert places[0]["count"] == 1
    # A tube holds nothing, and a box doesn't hold a freezer, or go inside itself.
    assert errors(moves(api, (cryo, sample, None)))["#/items/0"].endswith("which holds nothing.")
    assert errors(moves(api, (freezer, cryo, None)))["#/items/0"].endswith(
        "doesn't hold a Freezer."
    )
    assert errors(moves(api, (rack, rack, None)))["#/items/0"] == (
        f"{rack['hrid']} doesn't hold a Rack."
    )
    # Scanning a freezer's asset tag, a tube's barcode or an HRID finds it.
    for code, hrid in (("AT-0003", freezer["hrid"]), ("TB-0001", sample["hrid"])):
        found = api.get("/storage/scan", code=code).json()["items"]
        assert [m["hrid"] for m in found] == [hrid]
    # A tube's is where it is in its box, which is last in its path.
    assert api.get("/storage/scan", code="TB-0001").json()["items"][0]["position"] == "C4"
    found = api.get("/storage/scan", code=cryo["hrid"].lower()).json()["items"]
    assert [(m["hrid"], m["container"]) for m in found] == [(cryo["hrid"], True)]
    assert api.get("/storage/scan", code="nothing-at-all").json()["items"] == []


def test_a_bulk_move_is_all_or_none_with_one_reason_and_history(world: World) -> None:
    api = Api(world.browser("editor"))
    first, second = box(api, world.project, "Box 1"), box(api, world.project, "Box 2", 2, 2)
    tubes = [tube(api, world.project, f"BM{i}") for i in range(4)]
    moved(api, *[(t, first, None) for t in tubes], reason="Put away")
    before = contents(api, first)
    # The second box has room for four; a fifth that can't go refuses every one.
    extra = tube(api, world.project, "BM4")
    moved(api, (extra, second, "A1"))
    refused = moves(api, *[(t, second, None) for t in tubes], reason="Consolidating")
    assert errors(refused) == {"#/items/3": f"{second['hrid']} has no free position left."}
    assert contents(api, first) == before
    # A reason is required, and a preview changes nothing.
    assert errors(moves(api, (tubes[0], second, None), reason=" ")) == {
        "#/reason": "is required: say why they're moved."
    }
    preview = moved(api, *[(t, second, None) for t in tubes[:3]], preview=True)
    assert [i["to"]["position"] for i in preview["items"]] == ["A2", "B1", "B2"]
    assert [i["kind"] for i in preview["items"]] == ["moved"] * 3
    assert preview["done"] is False and contents(api, first) == before
    # Swapping two in one move is one change: each position stays held by one.
    done = moved(api, (tubes[0], first, "A2"), (tubes[1], first, "A1"), reason="Swapped")
    assert contents(api, first)["A1"] == tubes[1]["hrid"]
    assert contents(api, first)["A2"] == tubes[0]["hrid"]
    operation = done["operation"]
    events = Event.objects.filter(operation=operation)
    assert {e.reason for e in events} == {"Swapped"} and events.count() == 2
    audit = AuditEvent.objects.filter(action="storage.moved", data__operation=operation)
    assert audit.count() == 2
    assert {a.data["kind"] for a in audit} == {"moved"}
    history = api.get(f"/entities/{tubes[0]['id']}/location/events").json()["items"]
    assert [(e["kind"], e["reason"]) for e in history] == [
        ("moved", "Swapped"),
        ("placed", "Put away"),
    ]
    assert history[0]["from_place"]["position"] == "A1"
    assert history[0]["to_place"]["position"] == "A2"
    assert history[0]["user"]["email"] == "editor@example.com"
    # Taken out of storage, it's nowhere, and says so.
    moved(api, (tubes[2], None, None), reason="Used up")
    assert api.get(f"/entities/{tubes[2]['id']}/location").json()["stored"] is False


def test_a_checked_out_tube_keeps_its_position_until_its_checked_in(world: World) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project, rows=1, columns=2)
    a, b = tube(api, world.project, "CO1"), tube(api, world.project, "CO2")
    moved(api, (a, cryo, "A1"))
    out = api.post("/storage/check-out", {"records": [a["id"]], "reason": "Thawing for SEC"})
    assert out.status_code == 200, out.content
    where = api.get(f"/entities/{a['id']}/location").json()
    assert where["custodian"]["email"] == "editor@example.com" and where["position"] == "A1"
    mine = api.get("/storage/checked-out").json()
    assert [m["hrid"] for m in mine] == [a["hrid"]]
    # Nothing else takes its position while it's out.
    assert errors(moves(api, (b, cryo, "A1")))["#/items/0"].endswith("has something at A1 already.")
    # Handed to the admin, then checked back in where it was.
    handed = api.post(
        "/storage/hand-over",
        {"record": a["id"], "to": str(world.users["admin"].pk), "reason": "Bench 4"},
    )
    assert handed.status_code == 200, handed.content
    assert api.get("/storage/checked-out").json() == []
    back = api.post("/storage/check-in", {"items": [{"record": a["id"]}], "reason": "Done"})
    assert back.status_code == 200, back.content
    where = api.get(f"/entities/{a['id']}/location").json()
    assert where["custodian"] is None and where["position"] == "A1"
    kinds = [e["kind"] for e in api.get(f"/entities/{a['id']}/location/events").json()["items"]]
    assert kinds == ["checked_in", "handed_over", "checked_out", "placed"]


def test_another_projects_boxes_are_never_seen_or_used(world: World) -> None:
    api = Api(world.browser("editor"))
    other = build_world("b-")
    theirs = Api(other.browser("editor"))
    freezer = made(theirs, other.project, FREEZER, {"name": "Their freezer"})
    their_box = box(theirs, other.project, "Their box")
    moved(theirs, (their_box, freezer, None))
    mine = tube(api, world.project, "AC1")
    # Not listed, not found, not opened, and nothing goes in it.
    assert api.get("/storage/places").json() == []
    assert api.get("/storage/scan", code=freezer["hrid"]).json()["items"] == []
    assert api.get(f"/storage/containers/{their_box['id']}").status_code == 404
    assert moves(api, (mine, their_box, None)).status_code == 404
    # Their tube isn't moved out of their box by us either.
    theirs_tube = tube(theirs, other.project, "AC2")
    moved(theirs, (theirs_tube, their_box, "A1"))
    assert moves(api, (theirs_tube, None, None)).status_code == 404
    # A viewer of a project reads its boxes but doesn't change them.
    viewer = Api(world.browser("viewer"))
    cryo = box(api, world.project)
    assert viewer.get(f"/storage/containers/{cryo['id']}").json()["can"]["keep"] is False
    assert errors(moves(viewer, (mine, cryo, None)))["#/items/0"] == (
        f"Putting things in {cryo['hrid']} needs Editor in its project."
    )


def test_a_path_stops_at_a_place_the_reader_cant_open(lab: Lab) -> None:
    """The core keeps a project's tube in its own freezer: the project sees that it's
    stored, and nothing of the core's freezer."""
    tech = lab.api("tech")
    freezer = made(tech, lab.core, FREEZER, {"name": "Core freezer"})
    cryo = box(tech, lab.core, "Incoming")
    moved(tech, (cryo, freezer, None))
    ask, tubes = sent_with_tubes(lab, 1)
    receive(lab, ask, put={tubes[0]["id"]: (cryo, "A1")})
    where = lab.api("editor").get(f"/entities/{tubes[0]['id']}/location").json()
    assert where == {
        "path": [],
        "hidden": True,
        "stored": True,
        "position": None,
        "custodian": None,
        "custody_since": None,
    }
    seen = lab.api("tech").get(f"/entities/{tubes[0]['id']}/location").json()
    assert [s["hrid"] for s in seen["path"]] == [freezer["hrid"], cryo["hrid"]]


# --- Receiving ----------------------------------------------------------------------------------


@pytest.fixture
def lab() -> Lab:
    world = build_world()
    core = Project.objects.create(team=world.team, name="Protein Core", description="Assays")
    for name, role, held in (
        ("tech", ProjectRole.EDITOR, (ANALYST,)),
        ("core_qa", ProjectRole.EDITOR, (QA,)),
        ("core_admin", ProjectRole.ADMIN, ()),
    ):
        user = User.objects.create_user(f"{name}@example.com", name.replace("_", " ").title())
        world.users[name] = user
        _member(core, user, role, *held)
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["editor"]), role=ANALYST
    )
    permissions.forget(world.users["editor"])
    return Lab(world, core, world.users)


def sent_with_tubes(lab: Lab, n: int) -> tuple[Any, list[Any]]:
    api = lab.api("editor")
    tubes = [
        tube(api, lab.world.project, f"RC{i:03}", amount={"value": 50, "unit": "uL"})
        for i in range(n)
    ]
    takes(lab)
    ask = made(
        api, lab.world.project, ASK, {"title": "Intact mass", "tubes": [t["id"] for t in tubes]}
    )
    answer = submit(lab, ask["id"])
    assert answer.status_code == 200, answer.content
    return ask, tubes


def receive(lab: Lab, ask: Any, put: dict[str, tuple[Any, str | None]]) -> Any:
    api = lab.api("tech")
    receipt = api.post(f"/entities/{ask['id']}/receipts").json()
    for line in receipt["lines"]:
        api.post(f"/receipts/{receipt['id']}/scan", {"code": line["code"]})
    api.patch(f"/receipts/{receipt['id']}", {"handed_over_by": "Courier, waybill 1Z999"})
    by_entity = {x["entity"]: x["id"] for x in receipt["lines"]}
    done = api.post(
        f"/receipts/{receipt['id']}/complete",
        {
            "put_away": [
                {"line": by_entity[k], "to": c["id"], "position": p} for k, (c, p) in put.items()
            ]
        },
    )
    assert done.status_code == 200, done.content
    return done.json()


def test_receiving_a_request_checks_each_tube_takes_custody_and_reports_back(lab: Lab) -> None:
    tech = lab.api("tech")
    incoming = box(tech, lab.core, "Incoming", 2, 2)
    ask, tubes = sent_with_tubes(lab, 4)
    # Only the core's analysts receive it.
    assert lab.api("editor").post(f"/entities/{ask['id']}/receipts").status_code == 403
    qa = lab.api("core_qa").get(f"/entities/{ask['id']}/receipts").json()
    assert qa["can_receive"] is False and "Analyst" in qa["why_not"]
    started = tech.post(f"/entities/{ask['id']}/receipts")
    assert started.status_code == 201, started.content
    receipt = started.json()
    # The checklist is what was sent.
    assert [x["code"] for x in receipt["lines"]] == ["RC000", "RC001", "RC002", "RC003"]
    assert receipt["outcomes"]["unchecked"] == 4
    path = f"/receipts/{receipt['id']}"
    scanned = tech.post(f"{path}/scan", {"code": "RC000"}).json()
    assert scanned["found"] == "matched"
    assert tech.post(f"{path}/scan", {"code": "RC000"}).json()["found"] == "again"
    # A tube nobody expected is an extra line.
    extra = tech.post(f"{path}/scan", {"code": "UNLABELLED-7"}).json()
    assert extra["found"] == "extra"
    lines = {x["code"]: x["id"] for x in extra["receipt"]["lines"]}
    # Damaged and rejected need a note.
    assert errors(tech.patch(f"{path}/lines/{lines['RC001']}", {"outcome": "damaged"})) == {
        "#/note": "is required: say why it's damaged."
    }
    for code, outcome, note in (
        ("RC001", "damaged", "Cap cracked; contents intact"),
        ("RC002", "rejected", "Thawed on arrival"),
        ("UNLABELLED-7", "extra", "No label, no paperwork"),
    ):
        answer = tech.patch(f"{path}/lines/{lines[code]}", {"outcome": outcome, "note": note})
        assert answer.status_code == 200, answer.content
    # Who handed it over is required to complete it.
    assert "#/handed_over_by" in errors(tech.post(f"{path}/complete", {"missing_rest": True}))
    tech.patch(path, {"handed_over_by": "Courier, waybill 1Z999", "note": "Dry ice present"})
    done = tech.post(
        f"{path}/complete",
        {
            "missing_rest": True,
            "put_away": [
                {"line": lines["RC000"], "to": incoming["id"]},
                {"line": lines["RC001"], "to": incoming["id"]},
            ],
        },
    )
    assert done.status_code == 200, done.content
    receipt = done.json()
    assert receipt["status"] == "completed"
    assert receipt["outcomes"] == {
        "matched": 1,
        "missing": 1,
        "extra": 1,
        "damaged": 1,
        "rejected": 1,
        "unchecked": 0,
    }
    outcomes = {x["code"]: (x["outcome"], x["put_away"]) for x in receipt["lines"]}
    assert outcomes["RC000"][1]["position"] == "A1"
    assert outcomes["RC001"][1]["position"] == "A2"
    assert outcomes["RC003"] == ("missing", None)
    # Received, in the core's custody; rejected, held by the tech and not put away.
    rejected = tech.get(f"/entities/{tubes[2]['id']}/location").json()
    assert rejected["custodian"]["email"] == "tech@example.com" and not rejected["stored"]
    history = tech.get(f"/entities/{tubes[0]['id']}/location/events").json()["items"]
    assert [e["kind"] for e in history] == ["placed", "received"]
    assert history[1]["handed_over_by"] == "Courier, waybill 1Z999"
    # The requester hears of what didn't arrive as expected, and the audit trail keeps it
    # on both sides.
    told = Notification.objects.get(
        recipient=lab.users["editor"], kind="storage.receipt_discrepancy"
    )
    assert ask["hrid"] in told.title
    assert "Thawed" not in told.body
    assert AuditEvent.objects.filter(action="request.receipt_completed").count() == 2
    # The core, keeping it, draws from what it received.
    drawn = tech.post(
        f"/entities/{tubes[0]['id']}/amount/draw",
        {"amount": {"value": 5, "unit": "uL"}, "reason": "Intact mass", "confirm": "RC000"},
    )
    assert drawn.status_code == 200, drawn.content
    # A completed receipt is frozen; the next starts from what's still expected.
    assert tech.patch(f"{path}/lines/{lines['RC000']}", {"outcome": "missing"}).status_code == 422
    again = tech.post(f"/entities/{ask['id']}/receipts").json()
    assert [x["code"] for x in again["lines"]] == ["RC003"]


def test_completion_accounts_for_every_received_tube_before_taking_custody(lab: Lab) -> None:
    tech = lab.api("tech")
    incoming = box(tech, lab.core, "Incoming", 2, 2)
    ask, tubes = sent_with_tubes(lab, 3)
    receipt = tech.post(f"/entities/{ask['id']}/receipts").json()
    path = f"/receipts/{receipt['id']}"
    first, second, unchecked = [line["id"] for line in receipt["lines"]]
    tech.patch(path, {"handed_over_by": "Courier"})
    tech.patch(f"{path}/lines/{first}", {"outcome": "matched"})
    tech.patch(f"{path}/lines/{second}", {"outcome": "damaged", "note": "Cap cracked"})
    placement = {"line": first, "to": incoming["id"], "position": "A1"}

    # A pending box lookup used to send an empty list. A partial plan is just as unsafe.
    incomplete: list[dict[str, Any]] = [{}, {"put_away": []}, {"put_away": [placement]}]
    for decision in incomplete:
        refused = errors(tech.post(f"{path}/complete", {"missing_rest": True, **decision}))
        assert f"#/lines/{second}" in refused
    # An explicit decision must refer to one eligible line, and appear only once.
    for holds in ([first, second], [second, second], [second, unchecked], [second, incoming["id"]]):
        refused = errors(
            tech.post(
                f"{path}/complete",
                {"missing_rest": True, "put_away": [placement], "keep_in_custody": holds},
            )
        )
        assert any(key.startswith("#/keep_in_custody/") for key in refused), refused
    # A top-level box isn't a placement, even alongside explicit custody decisions.
    assert (
        tech.post(
            f"{path}/complete",
            {
                "box": incoming["id"],
                "put_away": [],
                "keep_in_custody": [first, second],
                "missing_rest": True,
            },
        ).status_code
        == 422
    )

    untouched = tech.get(path).json()
    assert untouched["status"] == "open" and untouched["outcomes"]["unchecked"] == 1
    assert not Place.objects.filter(entity_id__in=[t["id"] for t in tubes]).exists()
    assert not AuditEvent.objects.filter(action="request.receipt_completed").exists()

    done = tech.post(
        f"{path}/complete",
        {"missing_rest": True, "put_away": [placement], "keep_in_custody": [second]},
    )
    assert done.status_code == 200, done.content
    placements = {line["id"]: line["put_away"] for line in done.json()["lines"]}
    assert placements[first]["position"] == "A1" and placements[second] is None
    held = tech.get(f"/entities/{tubes[1]['id']}/location").json()
    assert held["custodian"]["email"] == "tech@example.com" and not held["stored"]


def test_material_received_without_a_location_can_be_reconciled(lab: Lab) -> None:
    tech = lab.api("tech")
    asking = lab.api("editor")
    incoming = box(tech, lab.core, "Incoming", 1, 2)
    ask, tubes = sent_with_tubes(lab, 3)
    receipt = tech.post(f"/entities/{ask['id']}/receipts").json()
    path = f"/receipts/{receipt['id']}"
    a, b, c = [line["id"] for line in receipt["lines"]]
    tech.patch(path, {"handed_over_by": "Courier"})
    tech.patch(f"{path}/lines/{a}", {"outcome": "matched"})
    tech.patch(f"{path}/lines/{b}", {"outcome": "damaged", "note": "Cracked cap"})
    tech.patch(f"{path}/lines/{c}", {"outcome": "rejected", "note": "Thawed"})
    done = tech.post(
        f"{path}/complete",
        {"keep_in_custody": [a], "put_away": [{"line": b, "to": incoming["id"], "position": "A2"}]},
    )
    assert done.status_code == 200, done.content
    queue = "/storage/awaiting-put-away"
    pending = tech.get(queue)
    assert pending.status_code == 200, pending.content
    assert [(r["id"], r["custodian"]["email"]) for r in pending.json()] == [
        (tubes[0]["id"], "tech@example.com")
    ]
    assert [r["id"] for r in asking.get(queue).json()] == [tubes[0]["id"]]
    assert lab.api("outsider").get(queue).json() == []

    # The asking project cannot see the receiving box. Hidden placement is distinct
    # from receiving a tube without a location or holding a rejected tube.
    report = asking.get(path).json()
    assert [line["received_unplaced"] for line in report["lines"]] == [True, False, False]
    assert all(line["put_away"] is None for line in report["lines"])

    # A physical scan finds the unplaced tube and resolves it into this box.
    started = tech.post(f"/storage/containers/{incoming['id']}/reconciliations")
    recon = f"/reconciliations/{started.json()['id']}"
    scanned = tech.post(
        f"{recon}/scans",
        {"scans": [{"position": "A1", "code": "RC000"}, {"position": "A2", "code": "RC001"}]},
    )
    assert scanned.status_code == 200, scanned.content
    unexpected = next(line for line in scanned.json()["lines"] if line["code"] == "RC000")
    assert unexpected["finding"] == "unexpected"
    resolved = tech.patch(f"{recon}/lines/{unexpected['id']}", {"resolution": "placed"})
    assert resolved.status_code == 200, resolved.content
    finished = tech.post(f"{recon}/complete", {})
    assert finished.status_code == 200, finished.content
    assert tech.get(queue).json() == []
    assert contents(tech, incoming)["A1"] == tubes[0]["hrid"]
    assert asking.get(path).json()["lines"][0]["received_unplaced"] is True


# --- Reconciling --------------------------------------------------------------------------------


def test_reconciling_a_box_finds_whats_wrong_and_records_how_its_resolved(world: World) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project, rows=2, columns=3)
    a, b, c, d = (tube(api, world.project, f"RZ{i}") for i in range(4))
    stray = tube(api, world.project, "RZ9")
    moved(api, (a, cryo, "A1"), (b, cryo, "A2"), (c, cryo, "A3"), (d, cryo, "B1"))
    r = api.post(f"/storage/containers/{cryo['id']}/reconciliations")
    assert r.status_code == 201, r.content
    path = f"/reconciliations/{r.json()['id']}"
    # a where it should be; b in the wrong position; c not there; the stray in B3; B2 empty.
    scans = [
        {"position": "A1", "code": "RZ0"},
        {"position": "B2", "code": "RZ1"},
        {"position": "A2", "code": None},
        {"position": "A3", "code": None},
        {"position": "B3", "code": "RZ9"},
    ]
    found = api.post(f"{path}/scans", {"scans": scans})
    assert found.status_code == 200, found.content
    findings = {x["code"]: x["finding"] for x in found.json()["lines"] if x["code"]}
    assert findings == {
        "RZ0": "matched",
        "RZ1": "wrong_slot",
        "RZ2": "missing",
        "RZ3": "unchecked",
        "RZ9": "unexpected",
    }
    assert "#/scans/0/position" in errors(
        api.post(f"{path}/scans", {"scans": [{"position": "C1", "code": "RZ0"}]})
    )
    lines = {x["code"]: x["id"] for x in found.json()["lines"] if x["code"]}
    # Nothing unresolved is completed.
    assert errors(api.post(f"{path}/complete", {}))
    assert "#/note" in errors(api.patch(f"{path}/lines/{lines['RZ1']}", {"resolution": "kept"}))
    assert "#/resolution" in errors(
        api.patch(f"{path}/lines/{lines['RZ1']}", {"resolution": "placed"})
    )
    for code, resolution in (("RZ1", "moved"), ("RZ2", "removed"), ("RZ9", "placed")):
        answer = api.patch(f"{path}/lines/{lines[code]}", {"resolution": resolution})
        assert answer.status_code == 200, answer.content
    done = api.post(f"{path}/complete", {"missing_rest": True, "note": "Quarterly count"})
    assert done.status_code == 200, done.content
    assert done.json()["status"] == "completed"
    assert contents(api, cryo) == {"A1": a["hrid"], "B2": b["hrid"], "B3": stray["hrid"]}
    # Each change shares the reconciliation's operation, and it's audited with its tally.
    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=cryo["id"])
    assert audit.data["findings"]["wrong_slot"] == 1 and audit.data["moved"] == 4
    assert Event.objects.filter(operation=audit.data["operation"]).count() == 4


def test_a_reconciliation_is_refused_if_the_box_changed_since_it_started(world: World) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project, rows=1, columns=2)
    a, b = tube(api, world.project, "RS0"), tube(api, world.project, "RS1")
    moved(api, (a, cryo, "A1"))
    r = api.post(f"/storage/containers/{cryo['id']}/reconciliations").json()
    api.post(f"/reconciliations/{r['id']}/scans", {"scans": [{"position": "A1", "code": "RS0"}]})
    moved(api, (b, cryo, "A2"))
    refused = errors(api.post(f"/reconciliations/{r['id']}/complete", {"missing_rest": True}))
    assert refused == {
        "#": f"Something moved into or out of {cryo['hrid']} since this started: start again, "
        "so what's recorded is current."
    }
    cancelled = api.post(f"/reconciliations/{r['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"


# --- Drawing ------------------------------------------------------------------------------------


def test_a_draw_from_a_tube_is_scanned_and_linked_to_its_sample_and_experiment(
    lab: Lab,
) -> None:
    world = lab.world
    api = lab.api("editor")
    sample = made(api, world.project, SAMPLE, {"label": "S-77"})
    tb = tube(
        api, world.project, "DR-001", amount={"value": 100, "unit": "uL"}, sample=sample["id"]
    )
    into = made(api, world.project, ASK, {"title": "SEC run 12"})
    draw = f"/entities/{tb['id']}/amount/draw"
    body = {"amount": {"value": 10, "unit": "uL"}, "reason": "SEC", "used_in": into["id"]}
    # Its barcode is scanned before a draw, so it's the right tube.
    assert errors(api.post(draw, body)) == {
        "#/confirm": f"Scan {tb['hrid']} to confirm it's the one you're drawing from."
    }
    assert errors(api.post(draw, {**body, "confirm": "DR-002"})) == {
        "#/confirm": f"“DR-002” isn't {tb['hrid']}: check you have the right one."
    }
    answer = api.post(draw, {**body, "confirm": "DR-001"})
    assert answer.status_code == 200, answer.content
    movement = answer.json()["movement"]
    assert (movement["used_in"], movement["source"]) == (into["id"], sample["id"])
    assert movement["confirmed"] == "DR-001"
    assert answer.json()["amount"]["value"] == "90"
    # The experiment used material from the tube: an edge of the graph.
    assert Edge.objects.filter(node_id=into["id"], on_id=tb["id"], kind="used").exists()
    # The graph displays the stock draw and lets readers follow just material use.
    graph_url = f"/entities/{into['id']}/graph"
    for params in ({}, {"edges": ["used"]}):
        picture = api.get(graph_url, up=1, down=0, **params)
        assert picture.status_code == 200, picture.content
        edges = {(e["node"], e["on"], e["kind"]) for e in picture.json()["edges"]}
        assert (into["id"], tb["id"], "used") in edges
    path = api.get(f"/entities/{into['id']}/upstream", depth=1, edges=["used"])
    assert path.status_code == 200, path.content
    assert [(item["entity"]["id"], item["path"][0]["kind"]) for item in path.json()["items"]] == [
        (tb["id"], "used")
    ]
    # The sample's hold applies to its tube, and says why.
    project_qa(lab)
    answer = state(lab, "project_qa", sample["id"], "custody", "quarantined", "Out of spec")
    assert answer.status_code == 200, answer.content
    page = api.get(f"/entities/{tb['id']}").json()
    assert page["draw_refusals"]["routine"] == (
        f"Its sample, {sample['hrid']}, is Quarantined: none of it can be taken until that changes."
    )
    held = errors(api.post(draw, {**body, "confirm": "DR-001"}))
    assert held["#/amount"].startswith(f"Its sample, {sample['hrid']}, is Quarantined")
    # And another project's record can't be named as what it went into.
    other = build_world("d-")
    theirs = made(Api(other.browser("editor")), other.project, ASK, {"title": "Not yours"})
    assert "#/used_in" in errors(
        api.post(draw, {**body, "used_in": theirs["id"], "confirm": "DR-001"})
    )


def project_qa(lab: Lab) -> None:
    """The asking project's own QA, who quarantines its samples."""
    user = User.objects.create_user("project_qa@example.com", "Project QA")
    lab.users["project_qa"] = user
    _member(lab.world.project, user, ProjectRole.EDITOR, QA, ANALYST)


# --- Labels -------------------------------------------------------------------------------------


def test_labels_are_zpl_with_the_printers_own_barcodes_and_are_audited(world: World) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project, "Plasmid preps ^ ~ batch")
    t = tube(api, world.project, "LB-0042")
    sizes = api.get("/storage/labels").json()
    assert [s["key"] for s in sizes["sizes"]] == ["cryo", "standard", "box"]
    answer = api.post(
        "/storage/labels",
        {"records": [t["id"], cryo["id"]], "size": "standard", "format": "zpl", "dpi": 300},
    )
    assert answer.status_code == 200, answer.content
    body = answer.json()
    assert [(x["hrid"], x["code"]) for x in body["labels"]] == [
        (t["hrid"], "LB-0042"),
        (cryo["hrid"], cryo["hrid"]),
    ]
    zpl = body["zpl"]
    assert zpl.count("^XA") == 2 and zpl.count("^XZ") == 2
    # 50.8 x 25.4 mm at 300 dpi; a DataMatrix and a Code 128 of its barcode.
    assert "^PW600" in zpl and "^LL300" in zpl
    assert "^BXN" in zpl and "^FDLB-0042^FS" in zpl and "^BCN" in zpl
    # ZPL's own control characters in a name are escaped, not read as commands.
    assert "Plasmid preps \\5E \\7E" in zpl
    cryo_label = api.post(
        "/storage/labels",
        {"records": [t["id"]], "size": "cryo", "format": "zpl", "symbology": "qr"},
    ).json()["zpl"]
    assert "^BQN" in cryo_label and "^BCN" not in cryo_label
    assert AuditEvent.objects.filter(action="entity.labelled", target_id=t["id"]).count() == 2
    # A preview shows what would print, and records nothing.
    shown = api.post(
        "/storage/labels",
        {"records": [t["id"]], "size": "cryo", "format": "zpl", "preview": True},
    ).json()
    assert shown["labels"][0]["code"] == "LB-0042" and shown["zpl"] is None
    assert AuditEvent.objects.filter(action="entity.labelled", target_id=t["id"]).count() == 2
    assert labels.code128_modules("LB-0042") < labels.code128_modules("LB-0042-LONGER-CODE")
    assert errors(
        api.post("/storage/labels", {"records": [t["id"]], "size": "a4", "format": "print"})
    )


def test_services_find_by_barcode_hrid_or_link(world: World) -> None:
    api = Api(world.browser("editor"))
    t = tube(api, world.project, "FN-1")
    record = Entity.objects.get(pk=t["id"])
    user = world.users["editor"]
    for code in ("FN-1", t["hrid"], f"https://iki.example.com/entities/{t['id']}"):
        assert [e.pk for e in services.find(user, code)] == [record.pk], code
        assert services.confirms(record, code)
    assert services.find(world.users["viewer"], "FN-1") == [record]
    assert not Place.objects.filter(pk=record.pk).exists()

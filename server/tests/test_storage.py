"""Where material is kept (ADR 0180): freezers, racks and boxes, each position valid and held
by one record; moves, all or none, each with who, when and why; custody; receiving a
request's tubes against what it expected; reconciling a box by scan; and draws from a tube,
linked to the sample and the experiment it went into."""

from importlib import import_module
from typing import Any

import pytest
from api_client import Api
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.state import ProjectState
from org_world import World, build_world
from test_api_entities import create
from test_requests import ANALYST, ASK, QA, SAMPLE, Lab, _member, state, submit, takes
from test_webhooks import deliveries
from test_webhooks import make as make_webhook

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity
from iki_server.graph.models import Edge
from iki_server.notifications.models import Notification
from iki_server.notifications.tasks import message as notification_email
from iki_server.org import permissions
from iki_server.org.models import LabRoleGrant, Project, ProjectRole
from iki_server.storage import labels, services
from iki_server.storage.grids import NotAPosition, Shape
from iki_server.storage.models import Event, Place, ReconciliationLine
from iki_server.webhooks import services as webhooks

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


def test_a_bulk_move_makes_the_plan_its_preview_showed(world: World) -> None:
    """ADR 0207, with two clients: moves sent with their preview's ``plan`` are made as it
    showed them, each record from where it was and to the well shown, or not at all. Another
    placement into a well shown, or another move of a record shown, refuses them with what
    they'd do now; that plan, reviewed, is made as shown."""
    me, other = Api(world.browser("editor")), Api(world.browser("admin"))
    dest, bench = box(me, world.project, "Dest", 2, 2), box(me, world.project, "Bench", 2, 2)
    mine, theirs = tube(me, world.project, "PV-1"), tube(me, world.project, "PV-2")
    moved(me, (mine, bench, "A1"), reason="Thawing")

    def unchanged() -> tuple[Any, ...]:
        return (
            contents(me, dest),
            contents(me, bench),
            list(Event.objects.order_by("created_at").values_list("pk", flat=True)),
            AuditEvent.objects.filter(action="storage.moved").count(),
        )

    # Starting at the first free well, the preview shows A1.
    shown = moved(me, (mine, dest, None), preview=True)
    assert shown["items"][0]["to"]["position"] == "A1" and len(shown["plan"]) == 64
    # Meanwhile, at another bench, someone puts their tube in A1.
    moved(other, (theirs, dest, "A1"), reason="Bench 2")
    before = unchanged()
    refused = moves(me, (mine, dest, None), plan=shown["plan"])
    assert refused.status_code == 409, refused.content
    assert refused.json()["code"] == "moves_plan_changed"
    now = refused.json()["moves"]
    # The same one move, to another well: nothing moved, and what it would do now is said.
    assert [(i["record"]["hrid"], i["to"]["position"]) for i in now["items"]] == [
        (mine["hrid"], "A2")
    ]
    assert now["plan"] != shown["plan"] and now["done"] is False
    assert unchanged() == before

    # The record itself moved meanwhile, to another well of its bench: where it comes from
    # is part of what was shown, so that refuses them too.
    moved(other, (mine, bench, "B2"), reason="Rearranged")
    before = unchanged()
    again = moves(me, (mine, dest, None), plan=now["plan"])
    assert again.status_code == 409, again.content
    latest = again.json()["moves"]
    assert latest["items"][0]["from_place"]["position"] == "B2"
    assert latest["items"][0]["to"]["position"] == "A2"
    assert unchanged() == before

    # Reviewed again, it's made as shown, by whoever approved it.
    done = moved(me, (mine, dest, None), plan=latest["plan"], reason="Into Dest")
    assert done["done"] is True and done["plan"] is None
    assert contents(me, dest) == {"A1": theirs["hrid"], "A2": mine["hrid"]}
    [event] = Event.objects.filter(operation=done["operation"])
    assert (event.from_row, event.from_column, event.to_row, event.to_column) == (2, 2, 1, 2)
    assert event.user == world.users["editor"] and event.reason == "Into Dest"
    # An explicit well is bound the same way, and a plan names its moves in order.
    swap = moved(me, (mine, dest, "B1"), (theirs, dest, "B2"), preview=True)
    flipped = moved(me, (theirs, dest, "B2"), (mine, dest, "B1"), preview=True)
    assert swap["plan"] != flipped["plan"]
    moved(me, (mine, dest, "B1"), (theirs, dest, "B2"), plan=swap["plan"])
    assert contents(me, dest) == {"B1": mine["hrid"], "B2": theirs["hrid"]}


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
    requester = make_webhook(lab.world, "https://hooks.example.com/project", ["storage.moved"])
    org = make_webhook(
        lab.world, "https://hooks.example.com/org", ["storage.moved"], org_level=True
    )
    receive(lab, ask, put={tubes[0]["id"]: (cryo, "A1")})
    moved(tech, (tubes[0], cryo, "B2"), reason="Moved after inspection")
    project_data = deliveries(requester)[-1].payload["data"]
    org_data = deliveries(org)[-1].payload["data"]
    assert project_data["from"] is None and project_data["to"] is None
    assert project_data["reason"] == "Moved after inspection"
    assert org_data == {key: project_data[key] for key in ("entity", "kind", "actor", "agent")}
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
    editor = lab.api("editor")
    own = tube(editor, lab.world.project, "LOCAL-1")
    visible = box(editor, lab.world.project)
    moved(editor, (own, visible, "A2"))
    assert deliveries(requester)[-1].payload["data"]["to"] == {
        "container": visible["id"],
        "hrid": visible["hrid"],
        "position": "A2",
    }


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
    path = f"/receipts/{receipt['id']}"
    for line in receipt["lines"]:
        scanned = api.post(f"{path}/scan", {"code": line["code"]})
        assert scanned.status_code == 200, scanned.content
        receipt = scanned.json()["receipt"]
    described = api.patch(
        path,
        {"plan_revision": receipt["plan_revision"], "handed_over_by": "Courier, waybill 1Z999"},
    )
    assert described.status_code == 200, described.content
    receipt = described.json()
    by_entity = {x["entity"]: x["id"] for x in receipt["lines"]}
    done = api.post(
        f"{path}/complete",
        {
            "plan_revision": receipt["plan_revision"],
            "put_away": [
                {"line": by_entity[k], "to": c["id"], "position": p} for k, (c, p) in put.items()
            ],
            "keep_in_custody": [line for entity, line in by_entity.items() if entity not in put],
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
    duplicate = tech.post(f"{path}/scan", {"code": "RC000"}).json()
    assert duplicate["found"] == "again"
    # A service-private tube nobody expected is an extra line.
    private_tube = tube(tech, lab.core, "UNLABELLED-7")
    extra = tech.post(f"{path}/scan", {"code": "UNLABELLED-7"}).json()
    assert extra["found"] == "extra"
    receipt = extra["receipt"]
    lines = {x["code"]: x["id"] for x in receipt["lines"]}
    # Damaged and rejected need a note.
    unchecked = tech.patch(
        f"{path}/lines/{lines['RC001']}",
        {"outcome": "damaged"},
    )
    assert errors(unchecked) == {"#/note": "is required: say why it's damaged."}
    for code, outcome, note in (
        ("RC001", "damaged", "Cap cracked; contents intact"),
        ("RC002", "rejected", "Thawed on arrival"),
        ("UNLABELLED-7", "extra", "No label, no paperwork"),
    ):
        line = next(item for item in receipt["lines"] if item["code"] == code)
        change = {"outcome": outcome, "note": note}
        if line["outcome"] is not None:
            change["line_revision"] = line["line_revision"]
            change["reason"] = "Inspected the unlabelled tube after its barcode was scanned."
        answer = tech.patch(f"{path}/lines/{lines[code]}", change)
        assert answer.status_code == 200, answer.content
        checked = answer.json()
        assert (checked["found"], checked["line"]) == ("recorded", line["id"])
        receipt = checked["receipt"]
    # Who handed it over is required to complete it.
    incomplete = tech.post(
        f"{path}/complete", {"plan_revision": receipt["plan_revision"], "missing_rest": True}
    )
    assert "#/handed_over_by" in errors(incomplete)
    described = tech.patch(
        path,
        {
            "plan_revision": receipt["plan_revision"],
            "handed_over_by": "Courier, waybill 1Z999",
            "note": "Dry ice present",
        },
    )
    assert described.status_code == 200, described.content
    approval = {
        "plan_revision": described.json()["plan_revision"],
        "missing_rest": True,
        "put_away": [
            {"line": lines["RC000"], "to": incoming["id"]},
            {"line": lines["RC001"], "to": incoming["id"]},
        ],
    }
    proposal = tech.post(
        f"{path}/put-away/preview",
        {"plan_revision": approval["plan_revision"], "put_away": approval["put_away"]},
    )
    assert proposal.status_code == 200, proposal.content
    done = tech.post(f"{path}/complete", {**approval, "plan": proposal.json()["plan"]})
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
    assert told.title.endswith("1 missing, 1 extra, 1 damaged, 1 rejected")
    assert told.path == f"/storage/receive/{ask['id']}/"
    assert not told.body
    email = notification_email(told)
    assert "1 extra" in email.subject
    for text in (email.body, *(content for content, _ in email.alternatives)):
        assert isinstance(text, str)
        assert private_tube["hrid"] not in text and "UNLABELLED-7" not in text
    assert AuditEvent.objects.filter(action="request.receipt_completed").count() == 2
    # The core, keeping it, draws from what it received.
    drawn = tech.post(
        f"/entities/{tubes[0]['id']}/amount/draw",
        {"amount": {"value": 5, "unit": "uL"}, "reason": "Intact mass", "confirm": "RC000"},
    )
    assert drawn.status_code == 200, drawn.content
    # A completed receipt is frozen; the next starts from what's still expected.
    matched = next(line for line in receipt["lines"] if line["code"] == "RC000")
    frozen = tech.patch(
        f"{path}/lines/{matched['id']}",
        {
            "line_revision": matched["line_revision"],
            "outcome": "missing",
            "reason": "Tube missing on review",
        },
    )
    assert "completed" in errors(frozen)["#"]
    again = tech.post(f"/entities/{ask['id']}/receipts").json()
    assert [x["code"] for x in again["lines"]] == ["RC003"]


def test_upgrade_filters_existing_movement_deliveries_and_notices(lab: Lab) -> None:
    org_migration = import_module(
        "iki_server.webhooks.migrations.0003_organization_movement_notifications"
    ).Migration("0003", "iki_webhooks")
    with connection.schema_editor() as schema_editor:
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        org_migration.unapply(ProjectState(), schema_editor)
    requester = make_webhook(lab.world, "https://hooks.example.com/project", ["storage.moved"])
    org = make_webhook(
        lab.world, "https://hooks.example.com/org", ["storage.moved"], org_level=True
    )
    visible = box(lab.api("editor"), lab.world.project)
    hidden = box(lab.api("tech"), lab.core)
    identifiers = {"entity": {"id": visible["id"]}, "kind": "moved", "actor": None, "agent": None}
    payload = {
        "data": {
            **identifiers,
            "from": {"container": visible["id"], "hrid": visible["hrid"], "position": "A1"},
            "to": {"container": hidden["id"], "hrid": hidden["hrid"], "position": "B2"},
            "reason": "Received",
        }
    }
    for hook in (requester, org):
        hook.deliveries.create(message_id="msg_saved", event="storage.moved", payload=payload)
    ask, _ = sent_with_tubes(lab, 1)
    notice = Notification.objects.create(
        recipient=lab.users["editor"],
        kind="storage.receipt_discrepancy",
        entity_id=ask["id"],
        title="1 extra",
        body="Previously stored line",
        path=f"/entities/{ask['id']}",
    )
    migration = import_module("iki_server.storage.migrations.0007_receipt_and_movement_privacy")
    with connection.schema_editor() as schema_editor:
        migration.Migration("0007", "iki_storage").apply(ProjectState(), schema_editor)
    notice.refresh_from_db()
    assert (notice.title, notice.body, notice.path) == (
        "1 extra",
        "",
        f"/storage/receive/{ask['id']}/",
    )
    project_payload = {**payload, "data": {**payload["data"], "to": None}}
    assert deliveries(requester)[0].payload == project_payload

    # Existing deliveries may still be displayed, retried or sent again after an upgrade.
    amount_identifiers = {**identifiers, "kind": "drawn"}
    amount = {
        "type": "entity.amount_moved",
        "timestamp": "2026-01-01T12:00:00Z",
        "data": {**amount_identifiers, "balance": "12", "counterpart": hidden["hrid"]},
    }
    for hook in (requester, org):
        hook.deliveries.create(message_id="msg_amount", event="entity.amount_moved", payload=amount)
    with connection.schema_editor() as schema_editor:
        org_migration.apply(ProjectState(), schema_editor)
    assert requester.deliveries.get(message_id="msg_saved").payload == project_payload
    assert requester.deliveries.get(message_id="msg_amount").payload == amount
    assert org.deliveries.get(message_id="msg_saved").payload == {**payload, "data": identifiers}
    assert org.deliveries.get(message_id="msg_amount").payload == {
        **amount,
        "data": amount_identifiers,
    }
    # Old pods may still submit a detailed body after the migration has committed.
    later = org.deliveries.create(
        message_id="msg_later", event="entity.amount_moved", payload=amount
    )
    resent = webhooks.redeliver(later, by=lab.world.users["org_admin"])
    later.refresh_from_db()
    assert later.payload == resent.payload == {**amount, "data": amount_identifiers}


# --- Reconciling --------------------------------------------------------------------------------


def reviewed_line(line: dict[str, Any]) -> dict[str, Any]:
    return {"line_id": line["id"], "line_revision": line["line_revision"]}


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
    lines = {x["code"]: x for x in found.json()["lines"] if x["code"]}
    # Nothing unresolved is completed.
    assert errors(api.post(f"{path}/complete", {"plan_revision": found.json()["plan_revision"]}))
    assert "#/note" in errors(
        api.patch(
            f"{path}/lines/{lines['RZ1']['id']}",
            {"line_revision": lines["RZ1"]["line_revision"], "resolution": "kept"},
        )
    )
    assert "#/resolution" in errors(
        api.patch(
            f"{path}/lines/{lines['RZ1']['id']}",
            {"line_revision": lines["RZ1"]["line_revision"], "resolution": "placed"},
        )
    )
    answer = found
    for code, resolution in (("RZ1", "moved"), ("RZ2", "removed"), ("RZ9", "placed")):
        line = lines[code]
        answer = api.patch(
            f"{path}/lines/{line['id']}",
            {"line_revision": line["line_revision"], "resolution": resolution},
        )
        assert answer.status_code == 200, answer.content
    done = api.post(
        f"{path}/complete",
        {
            "plan_revision": answer.json()["plan_revision"],
            "missing_rest": True,
            "note": "Quarterly count",
        },
    )
    assert done.status_code == 200, done.content
    assert done.json()["status"] == "completed"
    assert contents(api, cryo) == {"A1": a["hrid"], "B2": b["hrid"], "B3": stray["hrid"]}
    # Each change shares the reconciliation's operation, and it's audited with its tally.
    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=cryo["id"])
    assert audit.data["findings"]["wrong_slot"] == 1 and audit.data["moved"] == 4
    assert Event.objects.filter(operation=audit.data["operation"]).count() == 4


def test_a_rescan_cannot_apply_a_move_for_a_tube_now_missing(world: World) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project, rows=1, columns=4)
    missing = tube(api, world.project, "RC-A")
    companion = tube(api, world.project, "RC-B")
    moved(api, (missing, cryo, "A2"), (companion, cryo, "A1"))
    checkout = api.post("/storage/check-out", {"records": [companion["id"]], "reason": "Assay"})
    assert checkout.status_code == 200, checkout.content
    started = api.post(f"/storage/containers/{cryo['id']}/reconciliations")
    assert started.status_code == 201, started.content
    path = f"/reconciliations/{started.json()['id']}"
    scanned = api.post(
        f"{path}/scans",
        {
            "scans": [
                {"position": "A2", "code": None},
                {"position": "A3", "code": "RC-A"},
                {"position": "A4", "code": "RC-B"},
            ]
        },
    )
    assert scanned.status_code == 200, scanned.content
    lines = {line["code"]: line for line in scanned.json()["lines"] if line["code"]}
    resolved = scanned
    for line in lines.values():
        assert line["finding"] == "wrong_slot"
        resolved = api.patch(
            f"{path}/lines/{line['id']}",
            {"line_revision": line["line_revision"], "resolution": "moved"},
        )
        assert resolved.status_code == 200, resolved.content

    prior = next(line for line in resolved.json()["lines"] if line["code"] == "RC-A")
    rescanned = api.post(
        f"{path}/scans",
        {
            "scans": [{"position": "A3", "code": None}],
            "reviewed_lines": [reviewed_line(prior)],
            "reason": "Recount found A3 empty",
        },
    )
    assert rescanned.status_code == 200, rescanned.content
    before = rescanned.json()
    stale = next(line for line in before["lines"] if line["code"] == "RC-A")
    assert stale["finding"] == "missing" and stale["found"] is None
    places = Place.objects.filter(pk__in=[missing["id"], companion["id"]]).order_by("pk")
    stored = list(places.values())
    events, audits = Event.objects.count(), AuditEvent.objects.count()
    refused = errors(api.post(f"{path}/complete", {"plan_revision": before["plan_revision"]}))
    detail = refused[f"#/lines/{stale['id']}"]
    assert missing["hrid"] in detail or stale["code"] in detail
    assert "missing" in detail.lower() and "moved" in detail.lower()
    assert stale["resolution_stale"] is True
    assert (
        next(line for line in before["lines"] if line["code"] == "RC-B")["resolution_stale"]
        is False
    )
    assert api.get(path).json() == before
    assert list(places.values()) == stored
    assert Event.objects.count() == events
    assert AuditEvent.objects.count() == audits

    corrected = api.patch(
        f"{path}/lines/{stale['id']}",
        {
            "line_revision": stale["line_revision"],
            "resolution": "removed",
            "reason": "The tube is missing on recount",
        },
    )
    assert corrected.status_code == 200, corrected.content
    done = api.post(f"{path}/complete", {"plan_revision": corrected.json()["plan_revision"]})
    assert done.status_code == 200, done.content
    assert done.json()["status"] == "completed"
    assert contents(api, cryo) == {"A4": companion["hrid"]}
    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=cryo["id"])
    assert Event.objects.filter(operation=audit.data["operation"]).count() == 2


@pytest.mark.parametrize(
    ("decision", "changed_scan", "finding", "final_position"),
    [
        pytest.param(
            "kept", {"position": "A3", "code": None}, "missing", "A1", id="kept-now-missing"
        ),
        pytest.param(
            "moved", {"position": "A4", "code": "RC-OBS"}, "wrong_slot", "A4", id="new-target"
        ),
        pytest.param("moved", None, "wrong_slot", "A3", id="legacy-open-decision"),
    ],
)
def test_reconciliation_decisions_require_the_current_observation(
    world: World,
    decision: str,
    changed_scan: dict[str, str | None] | None,
    finding: str,
    final_position: str,
) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project, rows=1, columns=4)
    specimen = tube(api, world.project, "RC-OBS")
    moved(api, (specimen, cryo, "A1"))
    started = api.post(f"/storage/containers/{cryo['id']}/reconciliations")
    assert started.status_code == 201, started.content
    path = f"/reconciliations/{started.json()['id']}"
    scanned = api.post(
        f"{path}/scans",
        {"scans": [{"position": "A1", "code": None}, {"position": "A3", "code": "RC-OBS"}]},
    )
    assert scanned.status_code == 200, scanned.content
    line = next(line for line in scanned.json()["lines"] if line["code"] == "RC-OBS")
    assert line["finding"] == "wrong_slot"
    line_path = f"{path}/lines/{line['id']}"
    saved = api.patch(
        line_path,
        {
            "line_revision": line["line_revision"],
            "resolution": decision,
            "note": "Counted at A3.",
        },
    )
    assert saved.status_code == 200, saved.content
    assert (
        next(x for x in saved.json()["lines"] if x["id"] == line["id"])["resolution_stale"] is False
    )

    if changed_scan is None:
        # Before observations were saved, the open decision has no evidence to bind it to.
        ReconciliationLine.objects.filter(pk=line["id"]).update(resolved_position=None)
    else:
        prior = next(x for x in saved.json()["lines"] if x["id"] == line["id"])
        rescanned = api.post(
            f"{path}/scans",
            {
                "scans": [changed_scan],
                "reviewed_lines": [reviewed_line(prior)],
                "reason": "The second inspection differs from the first",
            },
        )
        assert rescanned.status_code == 200, rescanned.content
    reviewed = api.get(path).json()
    latest = next(x for x in reviewed["lines"] if x["id"] == line["id"])
    assert latest["finding"] == finding
    assert latest["resolution"] == decision and latest["resolution_stale"] is True
    assert f"#/lines/{line['id']}" in errors(
        api.post(f"{path}/complete", {"plan_revision": reviewed["plan_revision"]})
    )
    assert contents(api, cryo) == {"A1": specimen["hrid"]}

    corrected = api.patch(
        line_path,
        {
            "line_revision": latest["line_revision"],
            "resolution": decision,
            "note": "Reviewed the latest count.",
            "reason": "Base the decision on the latest inspection",
        },
    )
    assert corrected.status_code == 200, corrected.content
    current = next(x for x in corrected.json()["lines"] if x["id"] == line["id"])
    assert current["resolution_stale"] is False and current["note"] == "Reviewed the latest count."
    done = api.post(f"{path}/complete", {"plan_revision": corrected.json()["plan_revision"]})
    assert done.status_code == 200, done.content
    assert contents(api, cryo) == {final_position: specimen["hrid"]}
    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=cryo["id"])
    assert Event.objects.filter(operation=audit.data["operation"]).count() == (
        0 if decision == "kept" else 1
    )


def test_an_unexpected_tube_no_longer_found_needs_its_placement_cleared(world: World) -> None:
    api = Api(world.browser("editor"))
    original = box(api, world.project, "Recorded box", rows=1, columns=2)
    counted = box(api, world.project, "Counted box", rows=1, columns=2)
    stray = tube(api, world.project, "RC-STRAY")
    moved(api, (stray, original, "A2"))
    started = api.post(f"/storage/containers/{counted['id']}/reconciliations")
    assert started.status_code == 201, started.content
    path = f"/reconciliations/{started.json()['id']}"
    scanned = api.post(f"{path}/scans", {"scans": [{"position": "A1", "code": "RC-STRAY"}]})
    assert scanned.status_code == 200, scanned.content
    (line,) = scanned.json()["lines"]
    assert line["finding"] == "unexpected" and line["entity"] == stray["id"]
    line_path = f"{path}/lines/{line['id']}"
    saved = api.patch(line_path, {"line_revision": line["line_revision"], "resolution": "placed"})
    assert saved.status_code == 200, saved.content

    rescanned = api.post(
        f"{path}/scans",
        {
            "scans": [{"position": "A1", "code": None}],
            "reviewed_lines": [reviewed_line(saved.json()["lines"][0])],
            "reason": "A second inspection found A1 empty",
        },
    )
    assert rescanned.status_code == 200, rescanned.content
    before = api.get(path).json()
    stale = next(item for item in before["lines"] if item["id"] == line["id"])
    assert stale["ref"]["hrid"] == stray["hrid"] and stale["finding"] == "no_longer_found"
    assert stale["found"] is None and stale["resolution"] == "placed"
    assert stale["resolution_stale"] is True
    events, audits = Event.objects.count(), AuditEvent.objects.count()
    detail = errors(
        api.post(
            f"{path}/complete", {"plan_revision": before["plan_revision"], "missing_rest": True}
        )
    )[f"#/lines/{line['id']}"]
    assert stray["hrid"] in detail or stale["code"] in detail
    assert "placed" in detail.lower()
    assert api.get(path).json() == before
    assert contents(api, original) == {"A2": stray["hrid"]} and contents(api, counted) == {}
    assert (Event.objects.count(), AuditEvent.objects.count()) == (events, audits)

    cleared = api.patch(
        line_path,
        {
            "line_revision": stale["line_revision"],
            "resolution": None,
            "reason": "The proposed placement no longer applies",
        },
    )
    assert cleared.status_code == 200, cleared.content
    assert all(item["id"] != line["id"] for item in cleared.json()["lines"])
    assert cleared.json()["plan_revision"] > before["plan_revision"]
    done = api.post(
        f"{path}/complete", {"plan_revision": cleared.json()["plan_revision"], "missing_rest": True}
    )
    assert done.status_code == 200, done.content
    assert done.json()["status"] == "completed"
    assert contents(api, original) == {"A2": stray["hrid"]} and contents(api, counted) == {}
    assert Event.objects.count() == events


def test_an_editor_cannot_resolve_a_replacement_observation_they_never_saw(world: World) -> None:
    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    cryo = box(a, world.project, rows=1, columns=4)
    specimen = tube(a, world.project, "RC-RESOLVE")
    moved(a, (specimen, cryo, "A2"))
    opened = a.post(f"/storage/containers/{cryo['id']}/reconciliations").json()
    path = f"/reconciliations/{opened['id']}"
    line = f"{path}/lines/{opened['lines'][0]['id']}"
    first = a.post(f"{path}/scans", {"scans": [{"position": "A3", "code": "RC-RESOLVE"}]}).json()
    assert first["lines"][0]["found"] == "A3"
    choice = {
        "line_revision": first["lines"][0]["line_revision"],
        "resolution": "moved",
        "note": "Reviewed A3",
    }
    newer = b.post(
        f"{path}/scans",
        {
            "scans": [{"position": "A4", "code": "RC-RESOLVE"}],
            "reviewed_lines": [reviewed_line(first["lines"][0])],
            "reason": "Located the tube in A4 on recount",
        },
    ).json()
    assert newer["lines"][0]["found"] == "A4"
    refused = a.patch(line, choice)
    after = ReconciliationLine.objects.get(pk=opened["lines"][0]["id"])
    assert refused.status_code == 409, (after.resolved_position, after.resolved_by, after.note)
    assert (after.resolved_position, after.resolved_by_id, after.note) == (None, None, "")
    assert "review" in refused.json()["detail"].lower()
    for invalid in ({}, {"line_revision": 0}, {"line_revision": "1"}):
        assert a.patch(line, {"resolution": "moved", **invalid}).status_code == 422
    refreshed = a.get(path).json()
    assert refreshed == newer
    assert (
        refreshed["lines"][0]["resolution"] is None and refreshed["lines"][0]["resolved_by"] is None
    )
    fresh = a.patch(
        line,
        {
            "line_revision": refreshed["lines"][0]["line_revision"],
            "resolution": "moved",
            "note": "Reviewed A4",
        },
    )
    assert fresh.status_code == 200, fresh.content
    stored = ReconciliationLine.objects.get(pk=opened["lines"][0]["id"])
    assert (stored.resolved_position, stored.resolved_by_id, stored.note) == (
        "A4",
        world.users["editor"].pk,
        "Reviewed A4",
    )
    # B reviews A's move decision and replaces it. A's old Clear must preserve B's choice.
    a_decision = fresh.json()
    b_reviewed = b.get(path).json()
    replacement = b.patch(
        line,
        {
            "line_revision": b_reviewed["lines"][0]["line_revision"],
            "resolution": "kept",
            "note": "Verify A4 again",
            "reason": "Hold the move until A4 is verified",
        },
    )
    assert replacement.status_code == 200, replacement.content
    stale_clear = a.patch(
        line,
        {
            "line_revision": a_decision["lines"][0]["line_revision"],
            "resolution": None,
            "reason": "Withdraw my previous decision",
        },
    )
    assert stale_clear.status_code == 409
    latest = a.get(path).json()
    assert latest == replacement.json() and latest["plan_revision"] > a_decision["plan_revision"]
    saved_choice = latest["lines"][0]
    assert (
        saved_choice["resolution"],
        saved_choice["resolved_by"]["id"],
        saved_choice["note"],
    ) == ("kept", str(world.users["admin"].pk), "Verify A4 again")
    cleared = a.patch(
        line,
        {
            "line_revision": saved_choice["line_revision"],
            "resolution": None,
            "reason": "Reopen this decision for inspection",
        },
    )
    assert cleared.status_code == 200 and cleared.json()["plan_revision"] > latest["plan_revision"]
    stored.refresh_from_db()
    assert (stored.resolution, stored.resolved_position, stored.resolved_by_id, stored.note) == (
        None,
        None,
        None,
        "",
    )


def test_completion_requires_the_reconciliation_plan_the_editor_reviewed(world: World) -> None:
    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    cryo = box(a, world.project, rows=1, columns=4)
    specimen = tube(a, world.project, "RC-REVIEW")
    moved(a, (specimen, cryo, "A2"))
    listing = f"/storage/containers/{cryo['id']}/reconciliations"
    opened = a.post(listing).json()
    path = f"/reconciliations/{opened['id']}"
    line = f"{path}/lines/{opened['lines'][0]['id']}"
    scans = a.post(f"{path}/scans", {"scans": [{"position": "A3", "code": "RC-REVIEW"}]})
    assert scans.status_code == 200, scans.content
    first_revision = scans.json()["plan_revision"]
    assert a.post(f"{path}/scans", {"scans": []}).status_code == 422
    failed_batch = a.post(f"{path}/scans", {"scans": [{"position": "A1"}, {"position": "B1"}]})
    assert failed_batch.status_code == 422
    assert a.get(path).json() == scans.json()
    rescanned = a.post(
        f"{path}/scans",
        {
            "scans": [{"position": "A3", "code": "RC-REVIEW"}],
            "reviewed_lines": [reviewed_line(scans.json()["lines"][0])],
        },
    )
    assert rescanned.status_code == 200 and rescanned.json()["plan_revision"] == first_revision + 1
    assert (
        a.patch(
            line,
            {"line_revision": rescanned.json()["lines"][0]["line_revision"], "resolution": "moved"},
        ).status_code
        == 200
    )
    seen = a.get(path).json()
    assert seen["lines"][0]["found"] == "A3" and not seen["lines"][0]["resolution_stale"]
    approved = {
        "plan_revision": seen["plan_revision"],
        "missing_rest": False,
        "note": "Reviewed A3",
    }

    # A second editor really saw and approved a positive A4 observation.
    second = b.post(
        f"{path}/scans",
        {
            "scans": [{"position": "A4", "code": "RC-REVIEW"}],
            "reviewed_lines": [reviewed_line(seen["lines"][0])],
            "reason": "The second inspection located the tube in A4",
        },
    )
    assert second.status_code == 200 and second.json()["lines"][0]["resolution_stale"]
    cleared = b.patch(
        line,
        {
            "line_revision": second.json()["lines"][0]["line_revision"],
            "resolution": None,
            "reason": "The original A3 move is obsolete",
        },
    )
    assert cleared.status_code == 200
    resolved = b.patch(
        line,
        {
            "line_revision": cleared.json()["lines"][0]["line_revision"],
            "resolution": "moved",
            "note": "Seen at A4",
        },
    )
    assert resolved.status_code == 200, resolved.content
    current = resolved.json()
    assert current["lines"][0]["found"] == "A4" and not current["lines"][0]["resolution_stale"]
    events, audits = Event.objects.count(), AuditEvent.objects.count()

    refused = a.post(f"{path}/complete", approved)
    assert refused.status_code == 409, refused.content
    assert "review" in refused.json()["detail"].lower()
    for invalid in ({}, {"plan_revision": 0}):
        assert a.post(f"{path}/complete", invalid).status_code == 422
    refreshed = a.get(path).json()
    assert refreshed == current and refreshed["status"] == "open" and refreshed["note"] == ""
    assert contents(a, cryo) == {"A2": specimen["hrid"]}
    assert (Event.objects.count(), AuditEvent.objects.count()) == (events, audits)

    assert 0 < opened["plan_revision"] < scans.json()["plan_revision"] < seen["plan_revision"]
    assert seen["plan_revision"] < second.json()["plan_revision"] < cleared.json()["plan_revision"]
    assert cleared.json()["plan_revision"] < refreshed["plan_revision"]
    assert a.get(listing).json()[0]["plan_revision"] == refreshed["plan_revision"]
    done = a.post(
        f"{path}/complete", {"plan_revision": refreshed["plan_revision"], "note": "Reviewed A4"}
    )
    assert done.status_code == 200, done.content
    assert done.json()["note"] == "Reviewed A4"
    assert done.json()["plan_revision"] == refreshed["plan_revision"]
    assert contents(a, cryo) == {"A4": specimen["hrid"]}
    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=cryo["id"])
    assert audit.data["plan_revision"] == refreshed["plan_revision"]
    event = Event.objects.get(operation=audit.data["operation"])
    assert (event.from_column, event.to_column) == (2, 4)


def test_a_reconciliation_is_refused_if_the_box_changed_since_it_started(world: World) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project, rows=1, columns=2)
    a, b = tube(api, world.project, "RS0"), tube(api, world.project, "RS1")
    moved(api, (a, cryo, "A1"))
    r = api.post(f"/storage/containers/{cryo['id']}/reconciliations").json()
    scanned = api.post(
        f"/reconciliations/{r['id']}/scans", {"scans": [{"position": "A1", "code": "RS0"}]}
    )
    assert scanned.status_code == 200
    moved(api, (b, cryo, "A2"))
    refused = errors(
        api.post(
            f"/reconciliations/{r['id']}/complete",
            {"plan_revision": scanned.json()["plan_revision"], "missing_rest": True},
        )
    )
    assert refused == {
        "#": f"Something moved into or out of {cryo['hrid']} since this started: start again, "
        "so what's recorded is current."
    }
    cancelled = api.post(
        f"/reconciliations/{r['id']}/cancel",
        {"plan_revision": scanned.json()["plan_revision"], "reason": "The box changed mid-count"},
    )
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
        "#/confirm": "This scan can’t identify a tube here. "
        "Use its printed Iki ID or ask an authorized reviewer."
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
    for record, label in zip((t, cryo), body["labels"], strict=True):
        assert (label["hrid"], label["code"], label["vendor_code"]) == (
            record["hrid"],
            "IKI:" + record["id"].replace("-", "").upper(),
            None,
        )
        assert label["code"] in body["zpl"]
    zpl = body["zpl"]
    assert zpl.count("^XA") == 2 and zpl.count("^XZ") == 2
    assert "^PW600" in zpl and "^LL300" in zpl
    assert zpl.count("^BXN") == 2 and "^BCN" not in zpl
    # ZPL's own control characters in a title are escaped, not read as commands.
    assert r"\5E" in zpl and r"\7E" in zpl

    vendor = api.post(
        "/storage/labels",
        {"records": [t["id"]], "size": "standard", "format": "zpl", "include_vendor_code": True},
    )
    assert vendor.status_code == 200, vendor.content
    both = vendor.json()
    assert both["labels"][0]["vendor_code"] == "LB-0042"
    assert both["labels"][0]["code"] in both["zpl"]
    assert "LB-0042" in both["zpl"] and "^BCN" in both["zpl"]
    prepared = AuditEvent.objects.filter(action="entity.labelled", target_id=t["id"])
    assert prepared.count() == 2
    assert prepared.latest("occurred_at").data["vendor_code"] == "LB-0042"
    cryo_label = api.post(
        "/storage/labels",
        {"records": [t["id"]], "size": "cryo", "format": "zpl", "symbology": "qr"},
    ).json()["zpl"]
    assert (
        "^BQN" in cryo_label
        and both["labels"][0]["code"] in cryo_label
        and "^BCN" not in cryo_label
    )
    for how in ["print", "zpl"]:
        for preview in [True, False]:
            for records, size, message in [
                ([t["id"]], "cryo", "Code 128"),
                ([t["id"], cryo["id"]], "standard", "vendor barcode"),
            ]:
                refused = api.post(
                    "/storage/labels",
                    {
                        "records": records,
                        "size": size,
                        "format": how,
                        "preview": preview,
                        "include_vendor_code": True,
                    },
                )
                assert refused.status_code == 422, refused.content
                assert message in refused.content.decode()
    previewed = api.post(
        "/storage/labels",
        {
            "records": [t["id"]],
            "size": "box",
            "format": "print",
            "preview": True,
            "include_vendor_code": True,
        },
    ).json()
    assert previewed["labels"][0]["vendor_code"] == "LB-0042" and previewed["zpl"] is None
    assert prepared.count() == 3
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
    assert services.find(world.users["viewer"], "FN-1") == [record]
    assert not Place.objects.filter(pk=record.pk).exists()

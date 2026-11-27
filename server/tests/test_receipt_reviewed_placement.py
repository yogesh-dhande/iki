"""Every accepted receipt line needs its own reviewed disposition before any custody changes."""

from typing import Any

import pytest
from api_client import Api
from django.db import connection
from django.test.utils import CaptureQueriesContext
from test_receipt_observation_history import change, read
from test_requests import Lab, _member
from test_storage import box, contents, errors, moved, sent_with_tubes, tube
from test_storage import lab as lab

from iki_server.audit.models import AuditEvent
from iki_server.entities import services as entities
from iki_server.entities.models import Entity, ProjectLink
from iki_server.notifications.models import Notification
from iki_server.org.models import Project, ProjectRole
from iki_server.storage.models import Event, Place

pytestmark = pytest.mark.django_db


def custody_state(api: Api, path: str, entities: list[str]) -> dict[str, Any]:
    return {
        "receipt": read(api, path),
        "history": read(api, path + "/history"),
        "places": list(Place.objects.filter(entity_id__in=entities).order_by("pk").values()),
        "links": list(ProjectLink.objects.filter(entity_id__in=entities).order_by("pk").values()),
        "events": list(Event.objects.order_by("pk").values_list("pk", flat=True)),
        "audits": list(AuditEvent.objects.order_by("pk").values_list("pk", flat=True)),
        "notifications": list(Notification.objects.order_by("pk").values_list("pk", flat=True)),
    }


def test_empty_and_partial_dispositions_cannot_complete_or_take_custody(lab: Lab) -> None:
    api = lab.api("tech")
    incoming = box(api, lab.core, "Receiving", 1, 2)
    request, tubes = sent_with_tubes(lab, 3)
    start = f"/entities/{request['id']}/receipts"
    receipt = change(api, "POST", start, status=201)
    # A real line from a different, cancelled receipt cannot stand in for today's tube.
    foreign = receipt["lines"][0]["id"]
    change(api, "POST", f"/receipts/{receipt['id']}/cancel", receipt, reason="Wrong waybill")
    receipt = change(api, "POST", start, status=201)
    path = f"/receipts/{receipt['id']}"
    matched, damaged, unchecked = (line["id"] for line in receipt["lines"])
    receipt = change(api, "POST", path + "/scan", code="RC000")["receipt"]
    receipt = change(api, "PATCH", f"{path}/lines/{damaged}", outcome="damaged", note="Bent cap")
    receipt = change(api, "PATCH", path, receipt, handed_over_by="Courier 104")
    base = {"plan_revision": receipt["plan_revision"], "missing_rest": True}
    place = {"line": matched, "to": incoming["id"], "position": "A1"}
    ids = [tube["id"] for tube in tubes]
    before = custody_state(api, path, ids)
    invalid_dispositions: list[tuple[dict[str, Any], str]] = [
        ({"put_away": []}, f"#/lines/{matched}"),
        ({}, f"#/lines/{damaged}"),
        ({"put_away": [place]}, f"#/lines/{damaged}"),
        ({"put_away": [place, place], "keep_in_custody": [damaged]}, "#/put_away/1"),
        ({"put_away": [{**place, "line": foreign}]}, "#/put_away/0"),
        ({"put_away": [place], "keep_in_custody": [damaged, matched]}, "#/keep_in_custody/1"),
        ({"put_away": [place], "keep_in_custody": [damaged, damaged]}, "#/keep_in_custody/1"),
        ({"put_away": [place], "keep_in_custody": [damaged, foreign]}, "#/keep_in_custody/1"),
        ({"put_away": [place], "keep_in_custody": [damaged, unchecked]}, "#/keep_in_custody/1"),
        ({"box": incoming["id"], "keep_in_custody": [matched, damaged]}, "#/box"),
    ]
    for fields, pointer in invalid_dispositions:
        response = api.post(path + "/complete", {**base, **fields})
        assert pointer in errors(response)
        assert custody_state(api, path, ids) == before


def test_live_custody_and_receipt_history_after_mixed_dispositions(lab: Lab) -> None:
    api, requester = lab.api("tech"), lab.api("editor")
    incoming = box(api, lab.core, "Receiving", 1, 2)
    request, tubes = sent_with_tubes(lab, 4)
    opened = change(api, "POST", f"/entities/{request['id']}/receipts", status=201)
    path = f"/receipts/{opened['id']}"
    a, b, c, d = (line["id"] for line in opened["lines"])
    receipt = change(api, "POST", path + "/scan", code="RC000")["receipt"]
    for line, outcome, note in ((b, "damaged", "Bent cap"), (c, "rejected", "Wrong material")):
        receipt = change(api, "PATCH", f"{path}/lines/{line}", outcome=outcome, note=note)
    extra = change(api, "POST", path + "/scan", code="UNLISTED")
    extra_line = next(line for line in extra["receipt"]["lines"] if line["id"] == extra["line"])
    receipt = change(
        api,
        "PATCH",
        f"{path}/lines/{extra['line']}",
        extra_line,
        outcome="extra",
        note="Returned to courier",
        reason="Reviewed waybill",
    )
    receipt = change(api, "PATCH", path, receipt, handed_over_by="Courier 104")
    assert not any(line["received_unplaced"] for line in receipt["lines"])
    queue = "/storage/awaiting-put-away"
    assert api.get(queue).json() == {"items": [], "next_cursor": None}
    done = change(
        api,
        "POST",
        path + "/complete",
        receipt,
        missing_rest=True,
        keep_in_custody=[a],
        put_away=[{"line": b, "to": incoming["id"], "position": "A2"}],
    )
    assert done["plan_revision"] == receipt["plan_revision"] + 1
    assert done["status"] == "completed" and read(api, path) == done
    assert [(line["id"], line["outcome"]) for line in done["lines"]] == [
        (a, "matched"),
        (b, "damaged"),
        (c, "rejected"),
        (d, "missing"),
        (extra["line"], "extra"),
    ]
    assert done["lines"][1]["put_away"]["position"] == "A2"
    for reader in (requester, api):
        pending = reader.get(queue)
        assert pending.status_code == 200 and len(pending.json()["items"]) == 1
        row = pending.json()["items"][0]
        assert (row["id"], row["hrid"]) == (tubes[0]["id"], tubes[0]["hrid"])
        assert row["custodian"]["id"] == str(lab.users["tech"].pk) and row["custody_since"]
    assert lab.api("outsider").get(queue).json()["items"] == []
    # A private receiving box and an explicitly unplaced tube have distinct receipt history.
    report = read(requester, path)
    assert [line["received_unplaced"] for line in report["lines"]] == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert all(line["put_away"] is None for line in report["lines"])
    assert Place.objects.get(pk=tubes[2]["id"]).custodian_id == lab.users["tech"].pk
    assert not Place.objects.filter(pk=tubes[3]["id"]).exists()

    # A later box count locates the held tube without changing the historical receipt.
    recon = change(api, "POST", f"/storage/containers/{incoming['id']}/reconciliations", status=201)
    count = f"/reconciliations/{recon['id']}"
    seen = change(
        api,
        "POST",
        count + "/scans",
        scans=[{"position": "A1", "code": "RC000"}, {"position": "A2", "code": "RC001"}],
    )
    observed = next(line for line in seen["lines"] if line["entity"] == tubes[0]["id"])
    assert (observed["finding"], observed["found"]) == ("unexpected", "A1")
    resolved = api.patch(
        f"{count}/lines/{observed['id']}",
        {"resolution": "placed", "line_revision": observed["line_revision"]},
    )
    assert resolved.status_code == 200, resolved.content
    change(api, "POST", count + "/complete", resolved.json())
    assert contents(api, incoming) == {"A1": tubes[0]["hrid"], "A2": tubes[1]["hrid"]}
    assert api.get(queue).json() == {"items": [], "next_cursor": None}
    assert read(requester, path) == report


def test_a_damaged_tube_kept_in_custody_reads_back_as_held_and_awaiting_put_away(lab: Lab) -> None:
    api, requester = lab.api("tech"), lab.api("editor")
    incoming = box(api, lab.core, "Receiving", 1, 2)
    request, tubes = sent_with_tubes(lab, 2)
    opened = change(api, "POST", f"/entities/{request['id']}/receipts", status=201)
    path = f"/receipts/{opened['id']}"
    matched, damaged = (line["id"] for line in opened["lines"])
    change(api, "POST", path + "/scan", code="RC000")
    change(api, "PATCH", f"{path}/lines/{damaged}", outcome="damaged", note="Cracked cap")
    receipt = change(api, "PATCH", path, read(api, path), handed_over_by="Courier 104")
    done = change(
        api,
        "POST",
        path + "/complete",
        receipt,
        put_away=[{"line": matched, "to": incoming["id"], "position": "A1"}],
        keep_in_custody=[damaged],
    )
    lines = {line["id"]: line for line in done["lines"]}
    # Damaged, kept by hand: in the receiver's custody, in no box, and its note kept.
    assert (lines[damaged]["outcome"], lines[damaged]["note"]) == ("damaged", "Cracked cap")
    assert (lines[damaged]["put_away"], lines[damaged]["received_unplaced"]) == (None, True)
    assert lines[matched]["put_away"]["position"] == "A1"
    assert not lines[matched]["received_unplaced"]
    held = read(api, f"/entities/{tubes[1]['id']}/location")
    assert held["custodian"]["id"] == str(lab.users["tech"].pk) and not held["stored"]
    assert held["path"] == [] and held["position"] is None
    assert contents(api, incoming) == {"A1": tubes[0]["hrid"]}
    for reader in (api, requester):
        waiting = reader.get("/storage/awaiting-put-away")
        assert waiting.status_code == 200, waiting.content
        assert [row["id"] for row in waiting.json()["items"]] == [tubes[1]["id"]]
    assert [line["received_unplaced"] for line in read(requester, path)["lines"]] == [False, True]


def test_an_archived_box_and_tube_read_back_as_archived_where_receiving_offers_wells(
    lab: Lab,
) -> None:
    api = lab.api("tech")
    incoming = box(api, lab.core, "Retired", 1, 2)
    request, tubes = sent_with_tubes(lab, 2)
    opened = change(api, "POST", f"/entities/{request['id']}/receipts", status=201)
    path = f"/receipts/{opened['id']}"
    for record, who in ((incoming, "tech"), (tubes[1], "editor")):
        entities.set_archived(
            lab.users[who], Entity.objects.get(pk=record["id"]), True, reason="Retired"
        )
    shown = read(api, f"/storage/containers/{incoming['id']}")
    assert shown["archived"] is True
    assert [line["ref"]["archived"] for line in read(api, path)["lines"]] == [False, True]


def test_next_free_receipt_placement_requires_the_current_reviewed_plan(lab: Lab) -> None:
    api = lab.api("tech")
    dest = box(api, lab.core, "Receiving", 1, 2)
    other = tube(api, lab.core, "BLOCKER")
    request, tubes = sent_with_tubes(lab, 1)
    opened = change(api, "POST", f"/entities/{request['id']}/receipts", status=201)
    path = f"/receipts/{opened['id']}"
    matched = opened["lines"][0]["id"]
    receipt = change(api, "POST", path + "/scan", code="RC000")["receipt"]
    receipt = change(api, "PATCH", path, receipt, handed_over_by="Courier")
    body = {
        "plan_revision": receipt["plan_revision"],
        "put_away": [{"line": matched, "to": dest["id"]}],
    }
    ids = [tubes[0]["id"]]
    before = custody_state(api, path, ids)
    preview = api.post(path + "/put-away/preview", body)
    assert preview.status_code == 200 and preview.json()["items"][0]["to"]["position"] == "A1"
    assert custody_state(api, path, ids) == before
    for expected in ["A1", "A2"]:
        before = custody_state(api, path, ids)
        response = api.post(path + "/complete", body)
        assert response.status_code == 409, response.content
        current = response.json()["moves"]
        assert current["items"][0]["to"]["position"] == expected
        assert custody_state(api, path, ids) == before
        body["plan"] = current["plan"]
        if expected == "A1":
            assert body["plan"] == preview.json()["plan"]
            moved(api, (other, dest, "A1"))
    done = api.post(path + "/complete", body)
    assert done.status_code == 200, done.content
    assert done.json()["lines"][0]["put_away"]["position"] == "A2"


def test_the_put_away_backlog_reads_a_page_at_a_time_longest_held_first(lab: Lab) -> None:
    """A core with a long backlog opens Storage on one page of it, not the whole queue."""
    api = lab.api("tech")
    request, tubes = sent_with_tubes(lab, 5)
    opened = change(api, "POST", f"/entities/{request['id']}/receipts", status=201)
    path = f"/receipts/{opened['id']}"
    for line in opened["lines"]:
        change(api, "POST", path + "/scan", code=line["code"])
    receipt = change(api, "PATCH", path, read(api, path), handed_over_by="Courier 104")
    change(
        api,
        "POST",
        path + "/complete",
        receipt,
        keep_in_custody=[line["id"] for line in opened["lines"]],
    )
    held = Place.objects.filter(entity_id__in=[t["id"] for t in tubes])
    expected = [str(p.pk) for p in held.order_by("custody_since", "pk")]
    assert len(expected) == 5

    queue = "/storage/awaiting-put-away"
    walked: list[str] = []
    cursor: str | None = None
    while True:
        response = api.get(queue, limit=2, **({"cursor": cursor} if cursor else {}))
        assert response.status_code == 200, response.content
        body = response.json()
        assert len(body["items"]) <= 2
        walked += [row["id"] for row in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert walked == expected

    # Reading a page costs the same however many rows it holds: nothing is read per row.
    def queries(limit: int) -> int:
        with CaptureQueriesContext(connection) as captured:
            assert api.get(queue, limit=limit).status_code == 200
        return len(captured)

    assert queries(1) == queries(5)
    assert api.get(queue, limit=101).status_code == 422


def test_receiving_says_which_tubes_a_box_can_take_before_a_well_is_chosen(lab: Lab) -> None:
    """A technician who edits two projects scans a box in the one the tubes aren't linked to:
    each tube is refused there before completing, by the same check completing makes."""
    api = lab.api("tech")
    stores = Project.objects.create(team=lab.world.team, name="Cold Room", description="Boxes")
    _member(stores, lab.users["tech"], ProjectRole.EDITOR)
    elsewhere = box(api, stores, "Cold room shelf", 1, 4)
    here = box(api, lab.core, "Receiving", 1, 4)
    request, tubes = sent_with_tubes(lab, 2)
    opened = change(api, "POST", f"/entities/{request['id']}/receipts", status=201)
    path = f"/receipts/{opened['id']}"
    first, second = (line["id"] for line in opened["lines"])
    change(api, "POST", path + "/scan", code="RC000")
    receipt = change(api, "POST", path + "/scan", code="RC001")["receipt"]
    receipt = change(api, "PATCH", path, receipt, handed_over_by="Courier 104")

    def placeable(to: Any) -> dict[str, str | None]:
        checked = api.post(
            path + "/put-away/placeable",
            {"plan_revision": receipt["plan_revision"], "box": to["id"]},
        )
        assert checked.status_code == 200, checked.content
        return {row["line"]: row["problem"] for row in checked.json()}

    assert placeable(here) == {first: None, second: None}
    refused = placeable(elsewhere)
    assert set(refused) == {first, second}
    for line, made in ((first, tubes[0]), (second, tubes[1])):
        assert refused[line] == (
            f"{made['hrid']} isn't in {elsewhere['hrid']}'s project, so it can't be kept there: "
            "whoever reads the box reads what's in it."
        )
    # Completing refuses an explicitly chosen well there just the same, and changes nothing.
    ids = [t["id"] for t in tubes]
    before = custody_state(api, path, ids)
    response = api.post(
        path + "/complete",
        {
            "plan_revision": receipt["plan_revision"],
            "put_away": [{"line": first, "to": elsewhere["id"], "position": "A1"}],
            "keep_in_custody": [second],
        },
    )
    assert response.status_code == 422, response.content
    assert custody_state(api, path, ids) == before
    # A stale revision is refused like the preview's, and an unreadable box isn't found.
    stale = api.post(
        path + "/put-away/placeable",
        {"plan_revision": receipt["plan_revision"] - 1, "box": here["id"]},
    )
    assert stale.status_code in (409, 412), stale.content
    secret = Project.objects.create(team=lab.world.team, name="Secret", description="Not tech's")
    _member(secret, lab.users["editor"], ProjectRole.EDITOR)
    hidden = box(lab.api("editor"), secret, "Not yours", 1, 1)
    unseen = api.post(
        path + "/put-away/placeable",
        {"plan_revision": receipt["plan_revision"], "box": hidden["id"]},
    )
    assert unseen.status_code == 404, unseen.content

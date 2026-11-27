"""A physical count distinguishes identical unregistered labels by observed slot."""

from typing import Any

import pytest
from api_client import Api, problem, seen
from org_world import World
from test_count_observation_history import history, review
from test_storage import box, contents, errors, tube, world

from iki_server.audit.models import AuditEvent
from iki_server.storage.models import Event

pytestmark = pytest.mark.django_db

__all__ = ["world"]

LABEL = "VENDOR-UNREGISTERED"


def start(api: Api, world: World, name: str = "Counting box") -> tuple[str, dict[str, Any]]:
    container = box(api, world.project, name, rows=1, columns=6)
    opened = api.post(f"/storage/containers/{container['id']}/reconciliations")
    assert opened.status_code == 201, opened.content
    return f"/reconciliations/{opened.json()['id']}", container


def reading(position: str, code: str = LABEL, **options: Any) -> dict[str, Any]:
    return {"position": position, "code": code, **options}


def by_slot(count: dict[str, Any]) -> dict[str, Any]:
    return {line["found"]: line for line in count["lines"] if line["found"] is not None}


def snapshot(api: Api, path: str) -> tuple[Any, ...]:
    return (
        api.get(path).json(),
        history(api, path),
        AuditEvent.objects.count(),
        Event.objects.count(),
    )


def test_rack_file_preserves_three_separate_observations_through_completion(world: World) -> None:
    api = Api(world.browser("editor"))
    path, container = start(api, world)
    positions = ["A3", "A1", "A2"]
    scanned = api.post(f"{path}/scans", {"scans": [reading(slot) for slot in positions]})
    assert scanned.status_code == 200, scanned.content
    at = by_slot(scanned.json())
    assert set(at) == set(positions)
    assert len({line["id"] for line in at.values()}) == 3
    events = history(api, path)
    assert len(events) == 3 and len({event["operation"] for event in events}) == 1
    for event in events:
        line = at[event["after"]["position"]]
        assert line["entity"] is None and line["finding"] == "unexpected" and line["code"] == LABEL
        assert event["line_id"] == line["id"] and line["observation"]["id"] == event["id"]
        assert event["after"]["code"] == LABEL and event["after"].get("entity_id") is None
        saved = api.patch(
            f"{path}/lines/{line['id']}",
            {
                "resolution": "kept",
                "note": f"Physical tube observed at {line['found']}",
            },
            headers=seen(line["revision"]),
        )
        assert saved.status_code == 200, saved.content
    revision = api.get(path).json()["revision"]
    done = api.post(f"{path}/complete", {}, headers=seen(revision))
    assert done.status_code == 200, done.content
    closed = by_slot(done.json())
    assert {slot: row["id"] for slot, row in closed.items()} == {
        slot: row["id"] for slot, row in at.items()
    }
    for row in closed.values():
        assert row["resolution"] == "kept" and row["reviewed_observation"] == row["observation"]
    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=container["id"])
    assert audit.data["findings"]["unexpected"] == 3 and audit.data["resolutions"]["kept"] == 3
    assert audit.data["moved"] == 0 and not Event.objects.filter(operation=audit.data["operation"])
    assert contents(api, container) == {}


def test_additional_tube_needs_a_choice_and_preserves_every_prior_observation(world: World) -> None:
    api = Api(world.browser("editor"))
    path, _ = start(api, world)
    first = api.post(f"{path}/scans", {"scans": [reading("A1")]})
    assert first.status_code == 200, first.content
    at = by_slot(first.json())

    for position in ("A2", "A3"):
        # On the third slot the unrelated first entry proves a late choice rolls back
        # the entire rack file as well as protecting a standalone read.
        scans = (
            [reading(position)]
            if position == "A2"
            else [reading("A6", "OTHER-LABEL"), reading(position)]
        )
        before = snapshot(api, path)
        denied = api.post(f"{path}/scans", {"scans": scans})
        assert denied.status_code == 409, denied.content
        problem = denied.json()
        assert problem["code"] == "count_unregistered_choice"
        assert problem["index"] == len(scans) - 1 and problem["position"] == position
        assert {entry["position"]: entry for entry in problem["observations"]} == {
            slot: {**review(line), "position": slot} for slot, line in at.items()
        }
        if position == "A2":
            # The legacy correction payload supplies a valid revision and reason, but
            # it still does not say whether this is a second tube or the first one moved.
            legacy = api.post(
                f"{path}/scans",
                {"scans": scans, "reviewed_lines": [review(at["A1"])], "reason": "Recounted rack"},
            )
            assert legacy.status_code == 409, legacy.content
            assert legacy.json()["code"] == "count_unregistered_choice"
        assert snapshot(api, path) == before
        added = api.post(
            f"{path}/scans", {"scans": [reading(position, unregistered={"action": "additional"})]}
        )
        assert added.status_code == 200, added.content
        updated = by_slot(added.json())
        assert {slot: updated[slot] for slot in at} == at
        assert updated[position]["id"] not in {line["id"] for line in at.values()}
        assert updated[position]["entity"] is None
        event = history(api, path)[-1]
        assert event["after"]["unregistered_action"] == "additional" and event["previous"] is None
        assert event["line_id"] == updated[position]["id"]
        at = updated
    assert set(at) == {"A1", "A2", "A3"} and len(history(api, path)) == 3


def test_moving_one_of_two_identical_labels_checks_the_named_line_revision_and_reason(
    world: World,
) -> None:
    editor, admin = (Api(world.browser(who)) for who in ("editor", "admin"))
    path, _ = start(editor, world)
    first = editor.post(f"{path}/scans", {"scans": [reading("A1"), reading("A2")]})
    assert first.status_code == 200, first.content
    a1, a2 = (by_slot(first.json())[slot] for slot in ("A1", "A2"))
    # A third-party reread keeps the physical line and makes the originally viewed
    # revision stale. A reread at a known slot needs no unregistered choice.
    refreshed = admin.post(
        f"{path}/scans", {"scans": [reading("A2")], "reviewed_lines": [review(a2)]}
    )
    assert refreshed.status_code == 200, refreshed.content
    seen = by_slot(refreshed.json())["A2"]
    assert seen["id"] == a2["id"] and seen["revision"] > a2["revision"]
    assert seen["observation"]["id"] != a2["observation"]["id"]
    before = snapshot(editor, path)
    move = reading("A3", unregistered={"action": "move", "line_id": a2["id"]})
    why = "Rescanned the tube taken from A2 in A3"
    for bad_review in ([], [review(a1)], [review(a2)]):
        refused = editor.post(
            f"{path}/scans", {"scans": [move], "reviewed_lines": bad_review, "reason": why}
        )
        assert problem(refused, 412, "stale")["affected_lines"] == [review(seen)]
    no_reason = editor.post(f"{path}/scans", {"scans": [move], "reviewed_lines": [review(seen)]})
    assert "#/reason" in errors(no_reason)
    assert snapshot(editor, path) == before

    moved = editor.post(
        f"{path}/scans", {"scans": [move], "reviewed_lines": [review(seen)], "reason": why}
    )
    assert moved.status_code == 200, moved.content
    now = by_slot(moved.json())
    assert set(now) == {"A1", "A3"} and now["A1"] == a1 and now["A3"]["id"] == a2["id"]
    event = history(editor, path)[-1]
    assert event["line_id"] == seen["id"] and event["previous"] == seen["observation"]["id"]
    assert event["before"]["position"] == "A2" and event["after"]["position"] == "A3"
    assert event["reason"] == why and event["actor"]["id"] == str(world.users["editor"].pk)
    assert event["after"]["unregistered_action"] == "move"
    repeated = admin.post(
        f"{path}/scans", {"scans": [reading("A3")], "reviewed_lines": [review(now["A3"])]}
    )
    assert repeated.status_code == 200, repeated.content
    latest = history(admin, path)[-1]
    assert latest["previous"] == event["id"] and latest["before"] == event["after"]
    assert latest["after"].get("unregistered_action") is None


def test_unregistered_move_requires_a_matching_source_in_this_count(world: World) -> None:
    api = Api(world.browser("editor"))
    path, _ = start(api, world)
    other, _ = start(api, world, "Second box")
    registered = tube(api, world.project, "REGISTERED-TUBE")
    scanned = api.post(
        f"{path}/scans",
        {"scans": [reading("A1"), reading("A2", "OTHER-CODE"), reading("A3", "REGISTERED-TUBE")]},
    )
    assert scanned.status_code == 200, scanned.content
    at = by_slot(scanned.json())
    assert at["A3"]["entity"] == registered["id"]
    foreign = api.post(f"{other}/scans", {"scans": [reading("A1")]})
    assert foreign.status_code == 200, foreign.content
    elsewhere = by_slot(foreign.json())["A1"]
    initial, independent = snapshot(api, path), snapshot(api, other)
    for source in (at["A2"], at["A3"], elsewhere):
        denied = api.post(
            f"{path}/scans",
            {
                "scans": [
                    reading("A5", "INDEPENDENT"),
                    reading("A4", unregistered={"action": "move", "line_id": source["id"]}),
                ],
                # A reviewed matching line cannot substitute for the source named above.
                "reviewed_lines": [review(at["A1"])],
                "reason": "Corrected source slot",
            },
        )
        assert denied.status_code == 409, denied.content
        problem = denied.json()
        assert problem["code"] == "count_unregistered_choice" and problem["index"] == 1
        assert problem["position"] == "A4"
        assert problem["observations"] == [{**review(at["A1"]), "position": "A1"}]
        assert snapshot(api, path) == initial and snapshot(api, other) == independent
    # The existing registered-tube duplicate check still follows its identity, even
    # when both input strings and both positions differ.
    duplicate = api.post(
        f"{path}/scans",
        {"scans": [reading("A4", "REGISTERED-TUBE"), reading("A5", registered["hrid"])]},
    )
    assert "#/scans/1/code" in errors(duplicate)
    assert snapshot(api, path) == initial


def test_one_file_rereads_the_previous_slot_and_adds_a_new_label_match(world: World) -> None:
    api = Api(world.browser("editor"))
    path, _ = start(api, world)
    first = api.post(f"{path}/scans", {"scans": [reading("A1")]})
    assert first.status_code == 200, first.content
    old = by_slot(first.json())["A1"]
    # The new position appears first in the file. A later reading in the same
    # file independently establishes that the original tube is still at A1.
    rescanned = api.post(
        f"{path}/scans",
        {"scans": [reading("A2"), reading("A1")], "reviewed_lines": [review(old)]},
    )
    assert rescanned.status_code == 200, rescanned.content
    at = by_slot(rescanned.json())
    assert set(at) == {"A1", "A2"} and all(line["entity"] is None for line in at.values())
    assert at["A1"]["id"] == old["id"] and at["A2"]["id"] != old["id"]
    reread, additional = (
        api.get(f"{path}/history", line_id=at[slot]["id"]).json()["items"][0]
        for slot in ("A1", "A2")
    )
    assert reread["previous"] == old["observation"]["id"] and additional["previous"] is None
    assert reread["operation"] == additional["operation"]


def test_new_registration_at_a_third_slot_leaves_both_unregistered_observations_intact(
    world: World,
) -> None:
    api = Api(world.browser("editor"))
    path, _ = start(api, world)
    scanned = api.post(f"{path}/scans", {"scans": [reading("A1"), reading("A2")]})
    assert scanned.status_code == 200, scanned.content
    previous = by_slot(scanned.json())
    original = history(api, path)
    registered = tube(api, world.project, LABEL)
    fresh = api.post(f"{path}/scans", {"scans": [reading("A3")]})
    assert fresh.status_code == 200, fresh.content
    at = by_slot(fresh.json())
    assert {slot: at[slot] for slot in ("A1", "A2")} == previous
    assert at["A3"]["id"] not in {line["id"] for line in previous.values()}
    assert (
        at["A3"]["entity"] == registered["id"]
        and at["A3"]["observation"]["identity_basis"] == "label"
    )
    evidence = history(api, path)
    assert evidence[:-1] == original and evidence[-1]["line_id"] == at["A3"]["id"]
    assert evidence[-1]["previous"] is None


def test_correcting_an_unregistered_label_preserves_its_slot_line_and_the_other_tube(
    world: World,
) -> None:
    editor, admin = (Api(world.browser(who)) for who in ("editor", "admin"))
    path, _ = start(editor, world)
    first = editor.post(f"{path}/scans", {"scans": [reading("A1"), reading("A2")]})
    assert first.status_code == 200, first.content
    old = by_slot(first.json())
    initial = snapshot(editor, path)
    corrected = reading("A1", "CORRECTED-Y")
    why = "Read the label again under better light"
    missing_review = admin.post(f"{path}/scans", {"scans": [corrected], "reason": why})
    assert problem(missing_review, 412, "stale")["affected_lines"] == [review(old["A1"])]
    payload = {"scans": [corrected], "reviewed_lines": [review(old["A1"])]}
    assert "#/reason" in errors(admin.post(f"{path}/scans", payload))
    assert snapshot(editor, path) == initial

    saved = admin.post(f"{path}/scans", {**payload, "reason": why})
    assert saved.status_code == 200, saved.content
    at = by_slot(saved.json())
    assert set(at) == {"A1", "A2"} and at["A2"] == old["A2"]
    assert at["A1"]["id"] == old["A1"]["id"] and at["A1"]["revision"] > old["A1"]["revision"]
    assert at["A1"]["entity"] is None and at["A1"]["code"] == "CORRECTED-Y"
    events = history(admin, path)
    assert events[:-1] == initial[1]
    change = events[-1]
    assert (
        change["line_id"] == old["A1"]["id"]
        and change["previous"] == old["A1"]["observation"]["id"]
    )
    assert change["before"]["code"] == LABEL and change["after"]["code"] == "CORRECTED-Y"
    assert change["before"]["position"] == change["after"]["position"] == "A1"
    assert at["A1"]["observation"]["id"] == change["id"] and change["reason"] == why
    assert change["actor"]["id"] == str(world.users["admin"].pk)

    current = snapshot(editor, path)
    conflict = editor.post(
        f"{path}/scans",
        {
            "scans": [corrected, reading("A1", "CONFLICT-Z")],
            "reviewed_lines": [review(at["A1"])],
            "reason": "Recounted both labels",
        },
    )
    assert "#/scans/1/position" in errors(conflict)
    assert snapshot(editor, path) == current


def test_explicit_rack_move_leaves_an_independent_replacement_at_the_source(world: World) -> None:
    editor, admin = (Api(world.browser(who)) for who in ("editor", "admin"))
    path, _ = start(editor, world)
    original = editor.post(f"{path}/scans", {"scans": [reading("A1"), reading("A2")]})
    assert original.status_code == 200, original.content
    old = by_slot(original.json())
    why = "Took the tube out of A1 and checked its replacement"
    rescanned = admin.post(
        f"{path}/scans",
        {
            # The replacement is first in file order; it cannot claim the line explicitly moved.
            "scans": [
                reading("A1", "REPLACEMENT"),
                reading("A3", unregistered={"action": "move", "line_id": old["A1"]["id"]}),
            ],
            "reviewed_lines": [review(old["A1"])],
            "reason": why,
        },
    )
    assert rescanned.status_code == 200, rescanned.content
    at = by_slot(rescanned.json())
    assert set(at) == {"A1", "A2", "A3"} and at["A2"] == old["A2"]
    assert at["A3"]["id"] == old["A1"]["id"] and at["A3"]["code"] == LABEL
    assert at["A1"]["id"] not in {line["id"] for line in old.values()}
    assert at["A1"]["code"] == "REPLACEMENT" and at["A1"]["entity"] is None
    events = {event["line_id"]: event for event in history(admin, path)[-2:]}
    correction, replacement = events[old["A1"]["id"]], events[at["A1"]["id"]]
    assert correction["previous"] == old["A1"]["observation"]["id"]
    assert correction["before"]["position"] == "A1" and correction["after"]["position"] == "A3"
    assert correction["after"]["unregistered_action"] == "move"
    assert replacement["before"] is None and replacement["previous"] is None
    assert (
        replacement["after"]["code"] == "REPLACEMENT" and replacement["after"]["position"] == "A1"
    )
    assert all(event["reason"] == why for event in events.values())
    assert correction["operation"] == replacement["operation"]

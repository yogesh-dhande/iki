"""Count observations and decisions keep their evidence when an operator amends them."""

from typing import Any

import pytest
from api_client import Api, problem, seen
from django.utils import timezone
from org_world import World
from test_storage import box, contents, errors, moved, tube, world

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity

pytestmark = pytest.mark.django_db

__all__ = ["world"]


def begin(api: Api, world: World, cols: int = 4) -> tuple[str, dict[str, Any], dict[str, Any]]:
    container = box(api, world.project, rows=1, columns=cols)
    specimen = tube(api, world.project, "COUNT-A")
    moved(api, (specimen, container, "A1"))
    opened = api.post(f"/storage/containers/{container['id']}/reconciliations")
    assert opened.status_code == 201, opened.content
    body = opened.json()
    return f"/reconciliations/{body['id']}", container, body


def review(line: dict[str, Any]) -> dict[str, Any]:
    return {"line_id": line["id"], "revision": line["revision"]}


def history(api: Api, path: str) -> list[dict[str, Any]]:
    answer = api.get(f"{path}/history")
    assert answer.status_code == 200, answer.content
    return list(reversed(answer.json()["items"]))


def test_correcting_an_observed_slot_reviews_all_superseded_evidence_atomically(
    world: World,
) -> None:
    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    path, container, _ = begin(a, world)
    first = a.post(
        f"{path}/scans", {"scans": [{"position": "A2"}, {"position": "A1", "code": "COUNT-A"}]}
    )
    assert first.status_code == 200, first.content
    at = {line["found"]: line for line in first.json()["lines"]}
    observations = history(b, path)
    assert len(observations) == 2 and all(
        x["actor"]["id"] == str(world.users["editor"].pk) and x["created_at"] for x in observations
    )
    empty, positive = observations
    assert empty["before"] is None and empty["after"]["observed"] and empty["after"]["code"] is None
    assert not positive["before"]["observed"] and positive["after"]["code"] == "COUNT-A"
    # Another operator may add independent evidence against the same open count.
    fresh = b.post(
        f"{path}/scans", {"scans": [{"position": "A4", "code": "OTHER"}], "reason": None}
    )
    assert fresh.status_code == 200
    before = fresh.json()
    now = history(b, path)
    audits = AuditEvent.objects.count()
    change = {"scans": [{"position": "A3", "code": "NEW"}, {"position": "A2", "code": "COUNT-A"}]}
    expected = [review(at["A1"]), review(at["A2"])]
    for reviewed in ([], expected[:1], [{**expected[1], "revision": 99}, expected[0]]):
        conflict = b.post(
            f"{path}/scans",
            {**change, "reviewed_lines": reviewed, "reason": "Rechecked the physical box"},
        )
        assert {
            x["line_id"]: x["revision"] for x in problem(conflict, 412, "stale")["affected_lines"]
        } == {x["line_id"]: x["revision"] for x in expected}
    for reason_fields in ({}, {"reason": None}, {"reason": ""}, {"reason": " "}):
        denied = b.post(f"{path}/scans", {**change, "reviewed_lines": expected, **reason_fields})
        assert "#/reason" in errors(denied)
    assert (
        a.get(path).json() == before
        and history(a, path) == now
        and AuditEvent.objects.count() == audits
    )
    why = "Tube observed in the formerly empty slot"
    done = b.post(f"{path}/scans", {**change, "reviewed_lines": expected, "reason": why})
    assert done.status_code == 200, done.content
    entries = history(a, path)[len(now) :]
    assert len({e["operation"] for e in entries}) == 1 and all(
        e["reason"] == why and e["actor"]["id"] == str(world.users["admin"].pk) for e in entries
    )
    removal = next(e for e in entries if e["line_id"] == empty["line_id"])
    move = next(e for e in entries if e["line_id"] == positive["line_id"])
    assert (
        removal["before"] == empty["after"]
        and removal["after"] is None
        and removal["previous"] == empty["id"]
    )
    assert (
        move["before"] == positive["after"]
        and move["after"]["position"] == "A2"
        and move["previous"] == positive["id"]
    )
    assert move["after"]["revision"] > move["before"]["revision"]
    assert set(contents(a, container)) == {"A1"}


def test_decisions_review_the_physical_empty_observation_and_keep_its_original_note(
    world: World,
) -> None:
    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    path, _, opened = begin(a, world)
    observed = a.post(f"{path}/scans", {"scans": [{"position": "A1"}]}).json()
    expected = next(x for x in observed["lines"] if x["code"] == "COUNT-A")
    empty = next(x for x in observed["lines"] if x["finding"] == "empty")
    line = f"{path}/lines/{expected['id']}"
    physical = empty["observation"]
    assert physical == expected["observation"] and physical["line_id"] == empty["id"]
    assert physical["line_id"] != expected["id"] and physical["position"] == "A1"
    assert physical["code"] is None and physical["scanned_by"]["id"] == str(
        world.users["editor"].pk
    )
    assert physical["scanned_at"] and expected["reviewed_observation"] is None
    assert a.get(f"{path}/history", line_id=expected["id"]).json()["items"] == []
    assert (
        a.get(f"{path}/history", line_id=physical["line_id"]).json()["items"][0]["id"]
        == physical["id"]
    )
    assert expected["revision"] > opened["lines"][0]["revision"]
    first = {"resolution": "kept", "note": "Double-checked map"}
    unrelated = b.post(f"{path}/scans", {"scans": [{"position": "A4", "code": "OTHER"}]})
    assert unrelated.status_code == 200
    saved = a.patch(line, first, headers=seen(expected["revision"]))
    assert saved.status_code == 200, saved.content
    before = saved.json()
    decision = next(x for x in before["lines"] if x["id"] == expected["id"])
    prior = history(a, path)
    assert prior[-1]["after"]["observation"] == prior[0]["id"]
    identical = b.patch(line, {**first}, headers=seen(decision["revision"]))
    assert identical.status_code == 200 and identical.json() == before and history(a, path) == prior
    for mutation in (
        {"resolution": "kept", "note": "Changed note"},
        {"resolution": "removed", "note": "Support"},
        {"resolution": None},
    ):
        refused = b.patch(line, {**mutation}, headers=seen(decision["revision"]))
        assert "#/reason" in errors(refused)
    # A fresh, physical verification of exactly the same empty slot is a new observation.
    repeat = b.post(
        f"{path}/scans", {"scans": [{"position": "A1"}], "reviewed_lines": [review(empty)]}
    )
    assert repeat.status_code == 200, repeat.content
    current = next(x for x in repeat.json()["lines"] if x["id"] == expected["id"])
    assert current["resolution_stale"] and current["revision"] > decision["revision"]
    assert (
        current["reviewed_observation"] == physical
        and current["observation"]["id"] != physical["id"]
    )
    assert current["observation"]["scanned_by"]["id"] == str(world.users["admin"].pk)
    actual = history(a, path)[-1]
    assert (
        actual["line_id"] == empty["id"]
        and actual["after"]["code"] is None
        and actual["previous"] == prior[0]["id"]
    )
    outdated = b.patch(
        line, {**first, "reason": "Saw map again"}, headers=seen(decision["revision"])
    )
    assert problem(outdated, 412, "stale")["affected_lines"] == [
        {"line_id": expected["id"], "revision": current["revision"]}
    ]
    assert outdated["ETag"] == f'"{current["revision"]}"'
    assert "#/reason" in errors(b.patch(line, {**first}, headers=seen(current["revision"])))
    reheard = b.patch(
        line,
        {
            **first,
            "reason": "Rechecked against latest empty scan",
        },
        headers=seen(current["revision"]),
    )
    assert reheard.status_code == 200, reheard.content
    confirmed = next(x for x in reheard.json()["lines"] if x["id"] == expected["id"])
    assert not confirmed["resolution_stale"]
    event = history(a, path)[-1]
    assert event["before"] == {**prior[-1]["after"], "revision": current["revision"]}
    assert event["after"]["observation"] == actual["id"] and event["previous"] == prior[-1]["id"]
    cleared = b.patch(
        line,
        {
            "resolution": None,
            "reason": "Reopen the count finding",
        },
        headers=seen(confirmed["revision"]),
    )
    assert cleared.status_code == 200, cleared.content
    final = history(a, path)[-1]
    assert (
        final["before"]["note"] == "Double-checked map"
        and final["reason"] == "Reopen the count finding"
        and final["after"] is None
    )
    latest = next(x for x in cleared.json()["lines"] if x["id"] == expected["id"])
    noop = b.patch(line, {"resolution": None}, headers=seen(latest["revision"]))
    assert noop.json() == cleared.json() and history(a, path)[-1] == final


def test_same_rack_file_rejects_conflicting_positions_and_canonical_tube_aliases(
    world: World,
) -> None:
    api = Api(world.browser("editor"))
    path, _, opened = begin(api, world)
    hrid = opened["lines"][0]["ref"]["hrid"]
    for scans, pointer in (
        (
            [{"position": "A2", "code": "COUNT-A"}, {"position": "A3", "code": hrid}],
            "#/scans/1/code",
        ),
        ([{"position": "A1"}, {"position": "a01", "code": "unknown"}], "#/scans/1/position"),
    ):
        denied = api.post(f"{path}/scans", {"scans": scans})
        assert (
            pointer in errors(denied)
            and api.get(path).json() == opened
            and history(api, path) == []
        )
    same = api.post(
        f"{path}/scans",
        {"scans": [{"position": "A2", "code": "COUNT-A"}, {"position": "a02", "code": hrid}]},
    )
    assert same.status_code == 200, same.content
    first, second = history(api, path)
    assert first["line_id"] == second["line_id"] and second["previous"] == first["id"]
    assert (first["after"]["code"], second["after"]["code"]) == ("COUNT-A", hrid)


def test_count_cancellation_requires_a_current_plan_and_who_why(world: World) -> None:
    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    path, _, opened = begin(a, world)
    v = opened["revision"]
    assert b.post(f"{path}/cancel").status_code == 422
    for reason_fields in ({}, {"reason": None}, {"reason": ""}, {"reason": "  "}):
        rejected = b.post(f"{path}/cancel", {**reason_fields}, headers=seen(v))
        assert "#/reason" in errors(rejected)
    assert a.get(path).json() == opened
    changed = a.post(f"{path}/scans", {"scans": [{"position": "A2", "code": "UNCATALOGUED"}]})
    assert changed.status_code == 200
    outdated = b.post(f"{path}/cancel", {"reason": "Wrong box"}, headers=seen(v))
    problem(outdated, 412, "stale")
    assert outdated["ETag"] == f'"{changed.json()["revision"]}"'
    assert b.post(f"{path}/cancel", {"reason": "Wrong box"}).status_code == 428
    why = "Wrong physical box opened"
    final = b.post(f"{path}/cancel", {"reason": why}, headers=seen(changed.json()["revision"]))
    assert final.status_code == 200, final.content
    assert final.json()["status"] == "cancelled" and final.json()["cancel_reason"] == why
    actor = str(world.users["admin"].pk)
    assert final.json()["cancelled_by"]["id"] == actor and final.json()["completed_at"]
    event = history(a, path)[-1]
    assert event["kind"] == "status" and event["line_id"] is None
    assert (
        event["actor"]["id"] == event["after"]["cancelled_by"] == actor and event["reason"] == why
    )
    assert event["before"]["status"] == "open" and event["after"]["status"] == "cancelled"
    assert a.post(f"{path}/scans", {"scans": [{"position": "A3"}]}).status_code == 422
    assert history(a, path)[-1] == event


def test_legacy_count_requires_a_real_rescan_before_adjudication(world: World) -> None:
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader
    from django.utils import timezone

    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    container = box(a, world.project, rows=1, columns=4)
    specimen = tube(a, world.project, "COUNT-A")
    moved(a, (specimen, container, "A1"))
    # The old application's INSERT must succeed after the rolling migration. No
    # legacy writer knew the new columns or could supply an observation event ID.
    prior = (
        MigrationLoader(connection)
        .project_state([("iki_storage", "0003_reconciliation_plan_revision")])
        .apps
    )
    operator = world.users["editor"].pk
    parent = prior.get_model("iki_storage", "Reconciliation").objects.create(
        container_id=container["id"], started_by_id=operator
    )
    at = timezone.now()
    old = prior.get_model("iki_storage", "ReconciliationLine").objects.create(
        reconciliation_id=parent.pk,
        entity_id=specimen["id"],
        code="COUNT-A",
        expected_row=1,
        expected_column=1,
        found_row=1,
        found_column=2,
        scanned_by_id=operator,
        scanned_at=at,
        resolution="moved",
        resolved_position="A2",
        resolved_by_id=operator,
        resolved_at=at,
        note="Legacy decision",
    )
    path = f"/reconciliations/{parent.pk}"
    legacy = b.get(path).json()
    row = legacy["lines"][0]
    assert legacy["cancel_reason"] == "" and row["revision"] == 1
    assert row["resolution_stale"] and history(a, path) == []
    line = f"{path}/lines/{old.pk}"
    refused = b.patch(
        line, {"resolution": "moved", "reason": "Revisit"}, headers=seen(row["revision"])
    )
    assert "Rescan" in errors(refused)["#/resolution"]
    refreshed = b.post(
        f"{path}/scans",
        {"scans": [{"position": "A2", "code": "COUNT-A"}], "reviewed_lines": [review(row)]},
    )
    assert refreshed.status_code == 200, refreshed.content
    evidence = history(b, path)[0]
    assert evidence["previous"] is None and evidence["before"]["input_unknown"]
    assert (
        evidence["before"]["scanned_by"] == str(world.users["editor"].pk)
        and evidence["before"]["scanned_at"] == at.isoformat()
    )
    row = refreshed.json()["lines"][0]
    new = b.patch(
        line,
        {
            "resolution": "moved",
            "note": "Checked again",
            "reason": "Confirmed the new scan",
        },
        headers=seen(row["revision"]),
    )
    assert new.status_code == 200, new.content
    decision = history(b, path)[-1]
    assert (
        decision["previous"] is None
        and decision["before"]["note"] == "Legacy decision"
        and decision["before"]["observation"] is None
    )
    assert decision["after"]["observation"] == evidence["id"]


@pytest.mark.django_db(transaction=True)
def test_two_operators_can_scan_independent_slots_and_only_one_can_replace_the_same_fact(
    world: World,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import connections

    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    path, _, _ = begin(a, world)

    def together(left: dict[str, Any], right: dict[str, Any]) -> list[int]:
        barrier = Barrier(2, timeout=10)

        def send(api: Api, body: dict[str, Any]) -> int:
            try:
                barrier.wait()
                return api.post(f"{path}/scans", body).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(send, api, body) for api, body in ((a, left), (b, right))]
            return sorted(f.result(timeout=20) for f in calls)

    assert together(
        {"scans": [{"position": "A2"}]}, {"scans": [{"position": "A3", "code": "UNLISTED"}]}
    ) == [200, 200]
    current = b.get(path).json()
    empty = next(x for x in current["lines"] if x["found"] == "A2")
    condition = {"reviewed_lines": [review(empty)], "reason": "Rechecked slot A2"}
    assert together(
        {**condition, "scans": [{"position": "A2", "code": "BLUE"}]},
        {**condition, "scans": [{"position": "A2", "code": "GREEN"}]},
    ) == [200, 412]
    rows = history(a, path)
    assert len(rows) == 4
    assert {e["after"]["code"] for e in rows if e["after"] is not None} in (
        {"BLUE", None, "UNLISTED"},
        {"GREEN", None, "UNLISTED"},
    )


def test_removing_an_unexpected_line_keeps_its_scan_choice_and_clear_by_line_id(
    world: World,
) -> None:
    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    path, _, _ = begin(a, world)
    made = a.post(f"{path}/scans", {"scans": [{"position": "A3", "code": "UNREGISTERED"}]}).json()
    unknown = next(x for x in made["lines"] if x["code"] == "UNREGISTERED")
    line = f"{path}/lines/{unknown['id']}"
    saved = a.patch(
        line,
        {
            "resolution": "kept",
            "note": "Label read; no registered tube",
        },
        headers=seen(unknown["revision"]),
    )
    assert saved.status_code == 200, saved.content
    old = next(x for x in saved.json()["lines"] if x["id"] == unknown["id"])
    changed = b.post(
        f"{path}/scans",
        {
            "scans": [{"position": "A3"}],
            "reviewed_lines": [review(old)],
            "reason": "Box slot verified empty",
        },
    )
    gone = next(x for x in changed.json()["lines"] if x["id"] == unknown["id"])
    assert gone["finding"] == "no_longer_found" and gone["resolution_stale"]
    clear = b.patch(
        line,
        {
            "resolution": None,
            "reason": "Release previously kept choice",
        },
        headers=seen(gone["revision"]),
    )
    assert clear.status_code == 200 and unknown["id"] not in {
        x["id"] for x in clear.json()["lines"]
    }
    only = a.get(f"{path}/history", line_id=unknown["id"]).json()["items"]
    assert [x["kind"] for x in only] == ["resolution", "scan", "resolution", "scan"]
    assert (
        only[0]["before"]["note"] == "Label read; no registered tube" and only[0]["after"] is None
    )
    assert only[1]["before"]["code"] == "UNREGISTERED" and not only[1]["after"]["observed"]
    assert all(x["line_id"] == unknown["id"] and x["created_at"] for x in only)


def test_rack_swap_keeps_the_original_positive_observation_ids(world: World) -> None:
    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    path, _, _ = begin(a, world)
    made = a.post(
        f"{path}/scans",
        {"scans": [{"position": "A2", "code": "RED"}, {"position": "A3", "code": "BLUE"}]},
    ).json()
    prior = {x["code"]: x for x in made["lines"] if x["found"]}
    scans = [
        {
            "position": slot,
            "code": code,
            "unregistered": {"action": "move", "line_id": prior[code]["id"]},
        }
        for slot, code in (("A2", "BLUE"), ("A3", "RED"))
    ]
    saved = b.post(
        f"{path}/scans",
        {
            "scans": scans,
            "reviewed_lines": [review(x) for x in prior.values()],
            "reason": "Corrected rack orientation",
        },
    )
    assert saved.status_code == 200, saved.content
    changed = {x["code"]: x for x in saved.json()["lines"] if x["found"]}
    for code, slot in (("RED", "A3"), ("BLUE", "A2")):
        assert changed[code]["id"] == prior[code]["id"] and changed[code]["found"] == slot
        entries = b.get(f"{path}/history", line_id=prior[code]["id"]).json()["items"]
        recent, old = entries
        assert recent["before"] == old["after"] and recent["after"]["position"] == slot


def test_a_newly_registered_tube_changes_the_identity_of_a_previously_unknown_scan(
    world: World,
) -> None:
    from iki_server.org.models import Project, ProjectMembership, ProjectRole

    a, b, viewer = (Api(world.browser(who)) for who in ("editor", "admin", "viewer"))
    path, _, _ = begin(a, world)
    first = a.post(f"{path}/scans", {"scans": [{"position": "A2", "code": "LATER"}]})
    old = next(x for x in first.json()["lines"] if x["code"] == "LATER")
    assert old["entity"] is None and old["finding"] == "unexpected"
    original = history(a, path)[0]
    private = Project.objects.create(team=world.team, name="Restricted registration")
    ProjectMembership.objects.create(
        project=private, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    registered = tube(b, private, "LATER")
    retry = {"scans": [{"position": "A2", "code": "LATER"}]}
    unreviewed = problem(b.post(f"{path}/scans", retry), 412, "stale")
    assert unreviewed["affected_lines"] == [review(old)]
    checked = {**retry, "reviewed_lines": [review(old)]}
    denied = b.post(f"{path}/scans", checked)
    assert "#/reason" in errors(denied) and history(b, path) == [original]
    saved = b.post(f"{path}/scans", {**checked, "reason": "Matched the registered tube"})
    assert saved.status_code == 200, saved.content
    linked = next(x for x in saved.json()["lines"] if x["id"] == old["id"])
    assert linked["entity"] == registered["id"] and linked["observation"]["code"] == "LATER"
    evidence = history(b, path)[-1]
    assert evidence["previous"] == original["id"] and evidence["line_id"] == old["id"]
    resolved = b.patch(
        f"{path}/lines/{old['id']}",
        {"resolution": "placed"},
        headers=seen(linked["revision"]),
    )
    assert resolved.status_code == 200, resolved.content
    for reader, visible in ((b, "LATER"), (viewer, "")):
        row = next(x for x in reader.get(path).json()["lines"] if x["id"] == old["id"])
        assert row["reviewed_observation"] == row["observation"]
        assert row["observation"]["code"] == visible and row["observation"]["line_id"] == row["id"]
        events = history(reader, path)
        assert events[0]["after"]["code"] == "LATER"
        assert events[1]["before"]["code"] == "LATER" and events[1]["after"]["code"] == visible


@pytest.mark.django_db(transaction=True)
def test_scan_reply_holds_its_parent_lock_through_the_returned_snapshot(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    import psycopg
    from django.db import OperationalError, connections, transaction

    from iki_server.api import storage as endpoints
    from iki_server.storage.models import Reconciliation

    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    path, _, opened = begin(a, world)
    output = endpoints._reconciliation_out
    lock_checks = []

    def try_lock(count_id: Any) -> bool:
        try:
            with transaction.atomic():
                Reconciliation.objects.select_for_update(nowait=True).get(pk=count_id)
        except OperationalError as exc:
            assert isinstance(exc.__cause__, psycopg.errors.LockNotAvailable)
            return True
        finally:
            connections.close_all()
        return False

    def read_locked(user: Any, count: Reconciliation, *, locked: bool = False) -> dict[str, Any]:
        # A competing transaction probes the exact interval between mutation and serialization.
        with ThreadPoolExecutor(max_workers=1) as worker:
            lock_checks.append(worker.submit(try_lock, count.pk).result(timeout=10))
        return output(user, count, locked=locked)

    monkeypatch.setattr(endpoints, "_reconciliation_out", read_locked)
    first = a.post(f"{path}/scans", {"scans": [{"position": "A2"}]})
    second = b.post(f"{path}/scans", {"scans": [{"position": "A3"}]})
    assert first.status_code == second.status_code == 200
    assert lock_checks == [True, True]
    assert first.json()["revision"] == opened["revision"] + 1
    assert second.json()["revision"] == opened["revision"] + 2
    assert {x["found"] for x in first.json()["lines"] if x["found"]} == {"A2"}
    assert {x["found"] for x in second.json()["lines"] if x["found"]} == {"A2", "A3"}


@pytest.mark.parametrize(
    "fields", [{}, {"note": None}, {"note": ""}], ids=["omitted", "null", "empty"]
)
def test_count_completion_accepts_an_optional_note(
    world: World, fields: dict[str, str | None]
) -> None:
    api = Api(world.browser("editor"))
    path, _, _ = begin(api, world)
    scanned = api.post(f"{path}/scans", {"scans": [{"position": "A1", "code": "COUNT-A"}]})
    assert scanned.status_code == 200, scanned.content
    finished = api.post(f"{path}/complete", {**fields}, headers=seen(scanned.json()["revision"]))
    assert finished.status_code == 200, finished.content
    assert finished.json()["status"] == "completed" and finished.json()["note"] == ""


def test_count_evidence_records_which_identifier_was_reviewed(world: World) -> None:
    api = Api(world.browser("editor"))
    path, _, opened = begin(api, world)
    expected = opened["lines"][0]
    hrid = expected["ref"]["hrid"]
    # A supplier can print another record's Iki ID as its own vendor label.
    tube(api, world.project, hrid)
    variants = (
        ({"code": hrid, "mode": "iki_id"}, "iki_id"),
        ({"code": f"/entities/{hrid}/"}, "record_link"),
        ({"code": f"/entities/{expected['entity']}/"}, "record_link"),
        ({"code": "COUNT-A"}, "label"),
    )
    previous = None
    for scan, basis in variants:
        req: dict[str, Any] = {"scans": [{"position": "A1", **scan}]}
        if previous is not None:
            req["reviewed_lines"] = [review(previous)]
        answer = api.post(f"{path}/scans", req)
        assert answer.status_code == 200, answer.content
        current = answer.json()["lines"][0]
        observed = current["observation"]
        assert current["entity"] == expected["entity"] and current["finding"] == "matched"
        assert (observed["code"], observed["identity_basis"]) == (scan["code"], basis)
        event = history(api, path)[-1]
        assert event["after"]["identity_basis"] == basis and event["after"]["code"] == scan["code"]
        if previous is not None:
            assert event["before"]["identity_basis"] == previous["observation"]["identity_basis"]
        previous = current


def test_count_line_and_observation_hide_a_purged_tubes_label(world: World) -> None:
    api = Api(world.browser("editor"))
    path, _, opened = begin(api, world)
    (expected,) = opened["lines"]
    observed = api.post(f"{path}/scans", {"scans": [{"position": "A1", "code": "COUNT-A"}]})
    assert observed.status_code == 200, observed.content
    (line,) = observed.json()["lines"]
    assert line["code"] == line["observation"]["code"] == "COUNT-A"
    Entity.objects.filter(pk=expected["entity"]).update(purged_at=timezone.now())

    current = api.get(path)
    assert current.status_code == 200, current.content
    (redacted,) = current.json()["lines"]
    assert redacted["id"] == expected["id"]
    assert redacted["observation"]["code"] == "" and history(api, path)[0]["after"]["code"] == ""
    assert redacted["code"] == ""


def test_counts_return_the_same_refusal_for_every_input_at_a_concealed_slot(world: World) -> None:
    from iki_server.org.models import Project, ProjectMembership, ProjectRole

    a, b = Api(world.browser("editor")), Api(world.browser("admin"))
    external = Project.objects.create(team=world.team, name="Other store")
    ProjectMembership.objects.create(
        project=external, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    sample = tube(b, external, "OWNER-LABEL")
    other = tube(b, external, "RECENT-LABEL")
    own = tube(a, world.project, "SERVICE-LABEL")
    container = box(a, world.project, rows=1, columns=3)
    link = f"/entities/{sample['id']}/projects/{world.project.pk}"
    assert b.put(link).status_code == 201
    moved(b, (sample, container, "A1"))
    count = a.post(f"/storage/containers/{container['id']}/reconciliations").json()
    path = f"/reconciliations/{count['id']}"
    positive = b.post(f"{path}/scans", {"scans": [{"position": "A2", "code": "RECENT-LABEL"}]})
    assert positive.status_code == 200
    assert b.delete(link).status_code == 204
    old = a.get(path).json()
    assert all(x["ref"] is None for x in old["lines"])
    original_history, audits = history(a, path), AuditEvent.objects.count()
    # A1's expected record and A2's previously observed record have limited visibility.
    # The service operator submits only the service's own identifiers and an empty scan.
    for pos in ("A1", "A2"):
        for data in ({}, {"code": "SERVICE-LABEL"}, {"code": own["hrid"], "mode": "iki_id"}):
            resp = a.post(f"{path}/scans", {"scans": [{"position": pos, **data}]})
            assert resp.status_code == 409, resp.content
            reply = resp.json()
            assert reply["code"] == "scan_unavailable" and "candidates" not in reply
            assert reply["detail"] == (
                "This scan can’t identify a tube here. "
                "Use its printed Iki ID or ask an authorized reviewer."
            )
    assert a.get(path).json() == old
    assert history(a, path) == original_history and AuditEvent.objects.count() == audits
    assert b.get(f"/entities/{sample['id']}/location").json()["position"] == "A1"
    assert not b.get(f"/entities/{other['id']}/location").json()["stored"]
    # A different, ordinary open position remains usable.
    ok = a.post(
        f"{path}/scans", {"scans": [{"position": "A3", "code": own["hrid"], "mode": "iki_id"}]}
    )
    assert ok.status_code == 200, ok.content


def test_a_scan_from_before_identity_basis_retains_its_original_evidence(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iki_server.storage import observations

    api = Api(world.browser("editor"))
    path, _, _ = begin(api, world)
    original_writer = observations.record

    def older_snapshot(*args: Any, **kwargs: Any) -> Any:
        # Model the immutable scan snapshot emitted by the previous writer.
        kwargs["after"].pop("identity_basis")
        return original_writer(*args, **kwargs)

    with monkeypatch.context() as old:
        old.setattr(observations, "record", older_snapshot)
        answer = api.post(f"{path}/scans", {"scans": [{"position": "A1", "code": "COUNT-A"}]})
    assert answer.status_code == 200, answer.content
    observed = api.get(path).json()["lines"][0]
    assert observed["observation"]["identity_basis"] is None
    earlier = history(api, path)
    assert len(earlier) == 1 and "identity_basis" not in earlier[0]["after"]

    repeated = api.post(
        f"{path}/scans",
        {"scans": [{"position": "A1", "code": "COUNT-A"}], "reviewed_lines": [review(observed)]},
    )
    assert repeated.status_code == 200, repeated.content
    original, newer = history(api, path)
    assert earlier == [original] and newer["before"] == original["after"]
    assert newer["after"]["identity_basis"] == "label"

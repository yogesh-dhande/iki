"""Two receivers can review the observations behind a corrected receipt (ADR 0180)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from threading import Barrier, Event
from typing import Any
from uuid import uuid4

import pytest
from api_client import Api, seen
from django.db import DatabaseError, connection, transaction
from test_requests import ANALYST, Lab
from test_storage import box, sent_with_tubes, tube
from test_storage import lab as lab

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity
from iki_server.org import permissions
from iki_server.org.models import LabRoleGrant
from iki_server.storage import receipts
from iki_server.storage.models import ObservationEvent, ReceiptLine
from iki_server.storage.models import Receipt as ReceiptModel

pytestmark = pytest.mark.django_db

type Receiving = tuple[Api, Api, str]
type JsonObject = dict[str, Any]


@pytest.fixture
def receiving(lab: Lab) -> Receiving:
    second = lab.users["core_qa"]
    LabRoleGrant.objects.create(membership=lab.core.memberships.get(user=second), role=ANALYST)
    permissions.forget(second)
    first, other = lab.api("tech"), lab.api("core_qa")
    request, _ = sent_with_tubes(lab, 2)
    opened = first.post(f"/entities/{request['id']}/receipts")
    assert opened.status_code == 201, opened.content
    return first, other, f"/receipts/{opened.json()['id']}"


def read(api: Api, path: str) -> JsonObject:
    response = api.get(path)
    assert response.status_code == 200, response.content
    data: JsonObject = response.json()
    return data


def change(
    api: Api,
    method: str,
    path: str,
    reviewed: JsonObject | None = None,
    *,
    status: int = 200,
    **fields: Any,
) -> JsonObject:
    # Made from the revision reviewed: the receipt's, or its line's (ADR 0289).
    headers = seen(reviewed["revision"]) if reviewed is not None else None
    response = api.call(method, path, fields, headers=headers)
    assert response.status_code == status, response.content
    data: JsonObject = response.json()
    if method == "PATCH" and "/lines/" in path and status == 200:
        assert set(data) == {"found", "line", "receipt"}
        assert data["found"] in {"recorded", "again"} and data["line"] == path.rsplit("/", 1)[1]
        receipt: JsonObject = data["receipt"]
        assert "found" not in receipt
        return receipt
    return data


def row(receipt: JsonObject, line_id: str) -> JsonObject:
    lines: list[JsonObject] = receipt["lines"]
    return next(line for line in lines if line["id"] == line_id)


def timeline(api: Api, path: str, **query: Any) -> list[JsonObject]:
    response = api.get(path + "/history", **query)
    assert response.status_code == 200, response.content
    events: list[JsonObject] = response.json()["items"]
    return list(reversed(events))


def test_two_receivers_correct_a_check_and_keep_the_original_observation(
    receiving: Receiving, lab: Lab
) -> None:
    first, second, path = receiving
    scan = change(first, "POST", path + "/scan", code="RC000")
    line = scan["line"]
    line_path = f"{path}/lines/{line}"
    matched = scan["receipt"]
    original = row(matched, line)
    assert scan["found"] == "matched" and original["outcome"] == "matched"
    assert original["checked_by"]["id"] == str(lab.users["tech"].pk) and original["checked_at"]
    scan_history = read(first, path + "/history")
    again = change(second, "POST", path + "/scan", code="RC000")
    assert again["found"] == "again" and again["line"] == line
    assert again["receipt"] == matched

    change(second, "PATCH", line_path, row(matched, line), status=422, outcome=None, reason="  ")
    change(
        second,
        "PATCH",
        line_path,
        row(matched, line),
        status=422,
        outcome="damaged",
        note="Cracked cap",
    )
    assert read(first, path) == matched
    assert read(second, path + "/history") == scan_history
    observer = lab.users["tech"]
    observer.email = "renamed-tech@example.com"
    observer.save(update_fields=["email"])
    cleared = change(
        second, "PATCH", line_path, row(matched, line), outcome=None, reason="Adjacent tube"
    )
    assert row(cleared, line)["outcome"] is None and row(cleared, line)["checked_by"] is None
    assert cleared["revision"] == matched["revision"] + 1
    rescanned = change(first, "POST", path + "/scan", code="RC000")["receipt"]
    damaged = change(
        second,
        "PATCH",
        line_path,
        row(rescanned, line),
        outcome="damaged",
        note="Cracked cap",
        reason="Inspection after unpacking",
    )
    change(
        first,
        "PATCH",
        line_path,
        row(damaged, line),
        status=422,
        outcome="damaged",
        note="Hairline crack",
        reason=" ",
    )
    updated = change(
        first,
        "PATCH",
        line_path,
        row(damaged, line),
        outcome="damaged",
        note="Hairline crack",
        reason="Inspected under light",
    )
    assert row(updated, line)["note"] == "Hairline crack"
    for field in ("checked_by", "checked_at"):
        assert row(updated, line)[field] == row(damaged, line)[field]
    assert read(second, path) == updated
    assert (
        change(
            second, "PATCH", line_path, row(updated, line), outcome="damaged", note="Hairline crack"
        )
        == updated
    )

    events = timeline(second, path, line_id=line)
    assert [event["after"]["outcome"] for event in events] == [
        "matched",
        None,
        "matched",
        "damaged",
        "damaged",
    ]
    assert [event["reason"] for event in events] == [
        "",
        "Adjacent tube",
        "",
        "Inspection after unpacking",
        "Inspected under light",
    ]
    reviewer = lab.users["core_qa"]
    assert [(event["actor"]["id"], event["actor"]["email"]) for event in events] == [
        (str(user.pk), user.email) for user in (observer, reviewer, observer, reviewer, observer)
    ]
    stored = ObservationEvent.objects.filter(receipt_id=matched["id"], line_id=line).order_by("id")
    observer_id, reviewer_id = str(observer.pk), str(reviewer.pk)
    assert list(stored.values_list("before__checked_by", "after__checked_by")) == [
        (None, observer_id),
        (observer_id, None),
        (None, observer_id),
        (observer_id, reviewer_id),
        (reviewer_id, reviewer_id),
    ]
    assert all(
        event["receipt_id"] == matched["id"] and event["line_id"] == line for event in events
    )
    assert events[0]["previous"] is None
    for earlier, later in pairwise(events):
        assert later["previous"] == earlier["id"] and later["before"] == earlier["after"]
        assert later["revision"] == earlier["revision"] + 1
    assert events[1]["before"]["checked_by"] == observer_id and events[1]["before"]["checked_at"]
    assert events[-1]["before"]["note"] == "Cracked cap"
    assert events[-1]["after"]["note"] == "Hairline crack"
    assert events[-1]["after"]["checked_by"] == reviewer_id


def test_a_removed_extra_can_only_be_restored_explicitly_to_the_same_line(
    receiving: Receiving,
) -> None:
    first, second, path = receiving
    extra = change(first, "POST", path + "/scan", code="EXTRA-71")
    line = extra["line"]
    line_path = f"{path}/lines/{line}"
    assert extra["found"] == "extra"
    noted = change(
        first,
        "PATCH",
        line_path,
        row(extra["receipt"], line),
        outcome="extra",
        note="Blue cap",
        reason="Inspected the packaging",
    )
    for reason_fields in ({}, {"reason": None}, {"reason": ""}):
        change(second, "DELETE", line_path, row(noted, line), status=422, **reason_fields)
    removed = change(
        second, "DELETE", line_path, row(noted, line), reason="Listed on a separate delivery"
    )
    assert removed["outcomes"]["extra"] == 0 and all(x["id"] != line for x in removed["lines"])
    assert [x["id"] for x in removed["removed_lines"]] == [line]
    change(first, "POST", line_path + "/restore", status=422, reason="Check waybill")
    change(
        first, "POST", line_path + "/restore", row(noted, line), status=409, reason="Check waybill"
    )
    again = change(first, "POST", path + "/scan", code="EXTRA-71")
    assert again["found"] == "removed" and again["line"] == line and again["receipt"] == removed
    for reason_fields in ({}, {"reason": None}, {"reason": ""}, {"reason": " "}):
        change(
            first,
            "POST",
            line_path + "/restore",
            removed["removed_lines"][0],
            status=422,
            **reason_fields,
        )
    restored = change(
        first,
        "POST",
        line_path + "/restore",
        removed["removed_lines"][0],
        reason="Waybill confirmed",
    )
    assert restored["outcomes"]["extra"] == 1 and restored["removed_lines"] == []
    assert row(restored, line)["code"] == "EXTRA-71" and row(restored, line)["note"] == "Blue cap"
    assert read(second, path) == restored

    events = timeline(first, path, line_id=line)
    assert [e["after"]["removed"] for e in events] == [False, False, True, False]
    assert events[-2]["reason"] == "Listed on a separate delivery"
    assert events[-2]["actor"]["email"] == "core_qa@example.com"
    assert (
        events[-1]["previous"] == events[-2]["id"] and events[-1]["reason"] == "Waybill confirmed"
    )

    retired = change(second, "DELETE", line_path, row(restored, line), reason="Returned to courier")
    ready = change(first, "PATCH", path, retired, handed_over_by="Courier waybill 511")
    final = change(first, "POST", path + "/complete", ready, missing_rest=True)
    assert final["outcomes"]["extra"] == 0 and final["outcomes"]["missing"] == 2
    assert final["removed_lines"] == retired["removed_lines"]
    change(
        first,
        "POST",
        line_path + "/restore",
        final["removed_lines"][0],
        status=422,
        reason="Check waybill",
    )
    assert read(first, path) == final


def test_handoff_corrections_and_cancellation_are_attributed_to_the_receiver(
    receiving: Receiving, lab: Lab
) -> None:
    first, second, path = receiving
    scan = change(first, "POST", path + "/scan", code="RC000")
    entered = change(first, "PATCH", path, scan["receipt"], handed_over_by="Courier waybill 501")
    described = change(
        second, "PATCH", path, entered, handed_over_by="Courier waybill 501", note="Dry ice"
    )
    assert change(first, "PATCH", path, described, note="Dry ice") == described
    change(second, "PATCH", path, described, status=422, handed_over_by="Courier waybill 511")
    change(second, "PATCH", path, described, status=422, note="")
    corrected = change(
        second,
        "PATCH",
        path,
        described,
        handed_over_by="Courier waybill 511",
        note="Dry ice present",
        reason="Checked the consignment record",
    )
    for reason_fields in ({}, {"reason": None}, {"reason": ""}, {"reason": " "}):
        change(first, "POST", path + "/cancel", corrected, status=422, **reason_fields)
    cancelled = change(first, "POST", path + "/cancel", corrected, reason="Request withdrawn")
    assert cancelled["lines"] == corrected["lines"]
    assert cancelled["status"] == "cancelled" and cancelled["received_by"] is None
    assert cancelled["cancelled_by"]["id"] == str(lab.users["tech"].pk)
    assert cancelled["cancel_reason"] == "Request withdrawn" and cancelled["completed_at"]
    assert cancelled["revision"] == corrected["revision"] + 1
    assert read(second, path) == cancelled
    final_history = read(second, path + "/history")
    change(
        second,
        "PATCH",
        f"{path}/lines/{scan['line']}",
        row(cancelled, scan["line"]),
        status=422,
        outcome=None,
        reason="Retest",
    )
    change(second, "PATCH", path, cancelled, status=422, note="Sample unpacked", reason="Unpacked")
    change(second, "POST", path + "/cancel", cancelled, status=422, reason="Retry")
    assert read(first, path) == cancelled
    assert read(first, path + "/history") == final_history

    events = timeline(second, path)
    assert [e["kind"] for e in events] == ["outcome", "handoff", "handoff", "handoff", "status"]
    amended, terminal = events[-2:]
    assert amended["before"] == {"handed_over_by": "Courier waybill 501", "note": "Dry ice"}
    assert amended["after"] == {"handed_over_by": "Courier waybill 511", "note": "Dry ice present"}
    assert (
        amended["previous"] == events[-3]["id"]
        and amended["actor"]["email"] == "core_qa@example.com"
    )
    assert (terminal["line_id"], terminal["previous"], terminal["reason"]) == (
        None,
        None,
        "Request withdrawn",
    )
    assert terminal["before"]["status"] == "open" and terminal["after"]["status"] == "cancelled"
    assert terminal["actor"]["id"] == cancelled["cancelled_by"]["id"]


def test_failed_completion_rolls_back_missing_observations_and_custody(
    receiving: Receiving, lab: Lab
) -> None:
    first, second, path = receiving
    incoming = box(first, lab.core, "Incoming", 1, 1)
    scan = change(first, "POST", path + "/scan", code="RC000")
    before = change(first, "PATCH", path, scan["receipt"], handed_over_by="Courier waybill 501")
    movements = f"/entities/{row(before, scan['line'])['entity']}/location/events"
    movements_before = read(second, movements)
    history_before = read(first, path + "/history")
    change(
        second,
        "POST",
        path + "/complete",
        before,
        status=422,
        missing_rest=True,
        put_away=[{"line": scan["line"], "to": incoming["id"], "position": "A2"}],
    )
    assert read(second, path) == before
    assert read(second, movements) == movements_before
    assert read(second, path + "/history") == history_before
    final = change(
        second,
        "POST",
        path + "/complete",
        before,
        missing_rest=True,
        keep_in_custody=[scan["line"]],
    )
    assert final["outcomes"]["matched"] == final["outcomes"]["missing"] == 1
    assert final["revision"] == before["revision"] + 1
    assert final["received_by"]["id"] == str(lab.users["core_qa"].pk)
    assert [item["kind"] for item in read(second, movements)["items"]] == ["received"]
    final_history = read(second, path + "/history")
    change(first, "POST", path + "/scan", status=422, code="RC001")
    change(first, "POST", path + "/cancel", final, status=422, reason="Retry")
    assert read(first, path) == final
    assert read(first, path + "/history") == final_history

    final_events = [e for e in final_history["items"] if e["revision"] == final["revision"]]
    assert {e["kind"] for e in final_events} == {"outcome", "status"} and len(final_events) == 2
    assert len({e["operation"] for e in final_events}) == 1
    assert all(e["actor"]["id"] == final["received_by"]["id"] for e in final_events)
    missing = next(e for e in final_events if e["kind"] == "outcome")
    assert missing["before"]["outcome"] is None and missing["after"]["outcome"] == "missing"
    assert ObservationEvent.objects.values_list("after__checked_by", flat=True).get(
        pk=missing["id"]
    ) == str(lab.users["core_qa"].pk)


def test_receipt_scoped_writes_require_the_revision_the_receiver_read(receiving: Receiving) -> None:
    first, second, path = receiving
    before = read(second, path)
    checked = change(first, "POST", path + "/scan", code="RC000")["receipt"]
    writes: list[tuple[str, str, JsonObject]] = [
        ("PATCH", path, {"handed_over_by": "Courier"}),
        ("POST", path + "/complete", {"missing_rest": True}),
        ("POST", path + "/cancel", {"reason": "Request withdrawn"}),
    ]
    # Without the receipt's revision (If-Match, ADR 0289) it's refused; with an old one, stale.
    for method, target, fields in writes:
        change(second, method, target, status=428, **fields)
        change(second, method, target, before, status=412, **fields)
    for unusable in ("*", 'W/"1"', '"1", "2"', "1"):
        refused = second.call(
            "PATCH", path, {"handed_over_by": "Courier"}, headers={"If-Match": unusable}
        )
        assert refused.status_code == 428, (unusable, refused.content)
    assert read(first, path) == checked


def test_corrections_follow_the_line_even_when_another_line_changes(receiving: Receiving) -> None:
    first, second, path = receiving
    scan = change(first, "POST", path + "/scan", code="RC000")
    original = row(scan["receipt"], scan["line"])
    assert original["revision"] > 0
    line_path = f"{path}/lines/{scan['line']}"
    other = change(second, "POST", path + "/scan", code="RC001")["receipt"]
    # Correcting a check names the line's revision reviewed (If-Match).
    change(second, "PATCH", line_path, status=428, outcome=None, reason="Wrong tube")
    for unusable in ("*", 'W/"1"', "1"):
        refused = second.call(
            "PATCH",
            line_path,
            {"outcome": None, "reason": "Wrong tube"},
            headers={"If-Match": unusable},
        )
        assert refused.status_code == 428, (unusable, refused.content)
    cleared = change(first, "PATCH", line_path, original, outcome=None, reason="Wrong tube")
    assert cleared["revision"] == other["revision"] + 1
    assert row(cleared, scan["line"])["revision"] == original["revision"] + 1
    history = read(first, path + "/history")
    audits = AuditEvent.objects.filter(
        action="storage.observation_recorded", target_id=cleared["id"]
    )
    audit_count = audits.count()
    change(
        second,
        "PATCH",
        line_path,
        original,
        status=412,
        outcome="missing",
        reason="Second inventory",
    )
    assert read(second, path) == cleared
    assert read(second, path + "/history") == history
    assert audits.count() == audit_count


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("manual", [False, True], ids=["disjoint-scans", "same-line-first-checks"])
def test_receivers_can_enter_first_observations_concurrently(
    receiving: Receiving, manual: bool
) -> None:
    first, second, path = receiving
    before = read(first, path)
    line_path = f"{path}/lines/{before['lines'][0]['id']}"
    barrier = Barrier(2, timeout=15)

    def observe(api: Api, index: int) -> tuple[str, JsonObject, int]:
        backend_pid = 0

        def overlap_receipt_locks(
            execute: Any, sql: str, params: Any, many: bool, context: Any
        ) -> Any:
            nonlocal backend_pid
            if (
                backend_pid == 0
                and f'FROM "{ReceiptModel._meta.db_table}"' in sql
                and "FOR UPDATE" in sql
            ):
                backend_pid = connection.connection.info.backend_pid
                barrier.wait()
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(overlap_receipt_locks):
                if manual:
                    response = api.patch(line_path, {"outcome": ["matched", "missing"][index]})
                else:
                    response = api.post(path + "/scan", {"code": ["RC000", "RC001"][index]})
            assert response.status_code == 200 and backend_pid, response.content
            data = response.json()
            assert set(data) == {"found", "line", "receipt"} and "found" not in data["receipt"]
            if manual:
                assert data["line"] == before["lines"][0]["id"]
            return data["found"], data["receipt"], backend_pid
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(observe, [first, second], [0, 1]))
    assert results[0][2] != results[1][2]
    findings = [found for found, _, _ in results]
    revisions = [snapshot["revision"] for _, snapshot, _ in results]
    after = read(first, path)
    if manual:
        assert sorted(findings) == ["again", "recorded"]
        assert revisions == [before["revision"] + 1] * 2
        assert after["revision"] == revisions[0]
        winner = findings.index("recorded")
        accepted = after["lines"][0]
        assert accepted["outcome"] == ["matched", "missing"][winner]
        assert (
            accepted["checked_by"]["email"] == ["tech@example.com", "core_qa@example.com"][winner]
        )
        assert all(snapshot["lines"][0] == accepted for _, snapshot, _ in results)
    else:
        assert findings == ["matched", "matched"]
        assert sorted(revisions) == [before["revision"] + 1, before["revision"] + 2]
        assert after["revision"] == max(revisions) and after["outcomes"]["matched"] == 2
        actors = {"RC000": "tech@example.com", "RC001": "core_qa@example.com"}
        assert {r["code"]: r["checked_by"]["email"] for r in after["lines"]} == actors
        for code, (_, snapshot, _) in zip(actors, results, strict=True):
            saved = next(line for line in snapshot["lines"] if line["code"] == code)
            assert saved["outcome"] == "matched" and saved["checked_by"]["email"] == actors[code]
    events = timeline(first, path)
    assert [e["revision"] for e in events] == list(
        range(before["revision"] + 1, after["revision"] + 1)
    )


def test_an_audit_write_failure_rolls_back_the_observation_and_its_revisions(
    receiving: Receiving,
) -> None:
    first, second, path = receiving
    before = read(first, path)
    history_before = read(second, path + "/history")
    refused_audits: list[str] = []

    def fail_audit(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        if sql.startswith(f'INSERT INTO "{AuditEvent._meta.db_table}"'):
            refused_audits.append(sql)
            raise DatabaseError("Observation audit unavailable")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_audit):
        failed = first.post(path + "/scan", {"code": "RC000"})
    assert failed.status_code == 500 and len(refused_audits) == 1
    assert read(second, path) == before
    assert read(first, path + "/history") == history_before
    retry = change(second, "POST", path + "/scan", code="RC000")
    assert retry["found"] == "matched"
    assert retry["receipt"]["revision"] == before["revision"] + 1


@pytest.mark.django_db(transaction=True)
def test_a_reader_waits_for_the_whole_receipt_change(receiving: Receiving) -> None:
    first, second, path = receiving
    before = read(first, path)
    locking = Event()

    def concurrent_read() -> JsonObject:
        def watch_lock(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
            if f'FROM "{ReceiptModel._meta.db_table}"' in sql and "FOR UPDATE" in sql:
                locking.set()
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(watch_lock):
                return read(second, path)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            ReceiptModel.objects.select_for_update().get(pk=before["id"])
            reader = pool.submit(concurrent_read)
            assert locking.wait(timeout=10), "Receipt GET did not request the parent lock."
            changed = change(first, "POST", path + "/scan", code="RC000")
        assert reader.result(timeout=10) == changed["receipt"]


def test_first_correction_of_a_legacy_line_preserves_its_known_check(
    receiving: Receiving, lab: Lab
) -> None:
    first, second, path = receiving
    line = read(first, path)["lines"][0]["id"]
    checked_at = datetime(2023, 5, 4, 12, 45, tzinfo=UTC)
    ReceiptLine.objects.filter(pk=line).update(
        outcome="damaged",
        note="Cap cracked",
        checked_by=lab.users["tech"],
        checked_at=checked_at,
    )
    known = row(read(second, path), line)
    assert known["checked_by"]["email"] == "tech@example.com"
    assert timeline(first, path, line_id=line) == []
    change(
        second, "PATCH", f"{path}/lines/{line}", known, outcome=None, reason="Package re-inspected"
    )
    assert row(read(first, path), line)["outcome"] is None
    (event,) = timeline(first, path, line_id=line)
    assert event["previous"] is None and event["actor"]["email"] == "core_qa@example.com"
    assert event["before"]["outcome"] == "damaged" and event["before"]["note"] == "Cap cracked"
    assert event["before"]["checked_by"] == str(lab.users["tech"].pk)
    assert datetime.fromisoformat(event["before"]["checked_at"]) == checked_at
    assert event["after"]["outcome"] is None and event["reason"] == "Package re-inspected"


def test_an_older_writer_can_insert_a_receipt_during_the_rolling_migration(
    receiving: Receiving,
) -> None:
    first, _, path = receiving
    source = read(first, path)
    legacy_id, line_id = uuid4(), uuid4()
    # A serving pod from before this migration supplies only its original fields.
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO iki_storage_receipt
               (id, request_id, project_id, status, handed_over_by, note, started_by_id, operation)
               SELECT %s, request_id, project_id, 'cancelled', handed_over_by, note,
                      started_by_id, operation
               FROM iki_storage_receipt WHERE id = %s""",
            [legacy_id, source["id"]],
        )
        cursor.execute(
            """INSERT INTO iki_storage_receipt_line
               (id, receipt_id, entity_id, code, expected, note)
               SELECT %s, %s, entity_id, code, expected, note
               FROM iki_storage_receipt_line WHERE id = %s""",
            [line_id, legacy_id, source["lines"][0]["id"]],
        )
    legacy = read(first, f"/receipts/{legacy_id}")
    assert legacy["revision"] == 1 and legacy["cancel_reason"] == ""
    assert legacy["cancelled_by"] is None and legacy["removed_lines"] == []
    assert row(legacy, str(line_id))["revision"] == 1
    assert timeline(first, f"/receipts/{legacy_id}") == []


def test_a_removed_extra_hides_its_linked_record_after_purge(
    receiving: Receiving, lab: Lab
) -> None:
    first, second, path = receiving
    extra = tube(first, lab.core, "PURGE-EXTRA")
    scanned = change(first, "POST", path + "/scan", code="PURGE-EXTRA")
    line_id = scanned["line"]
    current = row(scanned["receipt"], line_id)
    assert current["entity"] == extra["id"]
    retired = change(
        second,
        "DELETE",
        f"{path}/lines/{line_id}",
        current,
        reason="This delivery belonged to a different request",
    )
    assert retired["removed_lines"][0]["code"] == "PURGE-EXTRA"
    Entity.objects.filter(pk=extra["id"]).update(purged_at=datetime.now(UTC))
    hidden = read(second, path)["removed_lines"][0]
    assert hidden["id"] == line_id and hidden["code"] == ""
    assert hidden["ref"] is None and hidden["entity"] is None
    assert all(e["after"]["code"] == "" for e in timeline(second, path, line_id=line_id))


@pytest.mark.django_db(transaction=True)
def test_resuming_an_open_receipt_returns_the_open_snapshot_before_completion(
    receiving: Receiving, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, path = receiving
    reviewed = change(first, "PATCH", path, read(first, path), handed_over_by="Courier 104")
    resume_path = f"/entities/{reviewed['request']['id']}/receipts"
    found_open, release = Event(), Event()
    start = receipts.start

    def pause_after_finding_open(user: User, entity: Entity) -> ReceiptModel:
        existing = start(user, entity)
        found_open.set()
        assert release.wait(timeout=15), "The competing receiver did not release the resume."
        return existing

    monkeypatch.setattr(receipts, "start", pause_after_finding_open)

    def resume() -> JsonObject:
        try:
            response = first.post(resume_path)
            assert response.status_code == 201, response.content
            value: JsonObject = response.json()
            return value
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        resumed = pool.submit(resume)
        try:
            assert found_open.wait(timeout=15)
            with transaction.atomic():
                available = (
                    ReceiptModel.objects.select_for_update(skip_locked=True)
                    .filter(pk=reviewed["id"])
                    .exists()
                )
                if available:
                    change(second, "POST", path + "/complete", reviewed, missing_rest=True)
        finally:
            release.set()
        assert resumed.result(timeout=15) == reviewed
    assert not available, "An in-progress resume must retain its receipt lock."
    final = change(second, "POST", path + "/complete", reviewed, missing_rest=True)
    assert final["status"] == "completed" and final["revision"] == reviewed["revision"] + 1


@pytest.mark.parametrize("code,outcome", [("RC000", "matched"), ("EXTRA-71", "extra")])
def test_a_receiver_can_volunteer_a_reason_for_a_first_note_on_a_reviewed_line(
    receiving: Receiving, code: str, outcome: str
) -> None:
    first, second, path = receiving
    scan = change(first, "POST", path + "/scan", code=code)
    before = scan["receipt"]
    observed = row(before, scan["line"])
    line_path = f"{path}/lines/{scan['line']}"
    history = timeline(first, path)
    blind = second.patch(line_path, {"outcome": outcome, "note": "Blue cap"})
    assert blind.status_code == 200 and blind.json()["found"] == "again"
    change(
        second,
        "PATCH",
        line_path,
        status=422,
        outcome=outcome,
        note="Blue cap",
        reason="Packaging examined",
    )
    assert read(first, path) == before and timeline(first, path) == history
    updated = change(
        second,
        "PATCH",
        line_path,
        observed,
        outcome=outcome,
        note="Blue cap",
        reason="Packaging examined",
    )
    assert row(updated, scan["line"])["checked_by"] == observed["checked_by"]
    assert row(updated, scan["line"])["checked_at"] == observed["checked_at"]
    event = timeline(first, path)[-1]
    assert event["after"]["note"] == "Blue cap" and event["reason"] == "Packaging examined"
    assert event["actor"]["email"] == "core_qa@example.com"


def test_first_manual_observation_preserves_a_retained_note(receiving: Receiving) -> None:
    first, second, path = receiving
    scan = change(first, "POST", path + "/scan", code="RC000")
    line = scan["line"]
    line_path = f"{path}/lines/{line}"
    cleared = change(
        second,
        "PATCH",
        line_path,
        row(scan["receipt"], line),
        outcome=None,
        note="Check shipping manifest",
        reason="Tube label needs review",
    )
    assert row(cleared, line)["outcome"] is None
    history = timeline(first, path)
    change(first, "PATCH", line_path, status=422, outcome="matched", note="Different note")
    assert read(second, path) == cleared and timeline(second, path) == history
    confirmed = first.patch(line_path, {"outcome": "matched"})
    assert confirmed.status_code == 200, confirmed.content
    assert set(confirmed.json()) == {"found", "line", "receipt"}
    assert confirmed.json()["found"] == "recorded" and confirmed.json()["line"] == line
    current = row(confirmed.json()["receipt"], line)
    assert current["outcome"] == "matched" and current["note"] == "Check shipping manifest"
    event = timeline(second, path)[-1]
    assert event["before"]["note"] == event["after"]["note"] == current["note"]


@pytest.mark.parametrize("reason", ["", "Inspected at handoff"], ids=["empty", "volunteered"])
def test_first_manual_check_accepts_an_optional_reason_without_a_line_revision(
    receiving: Receiving, reason: str
) -> None:
    first, second, path = receiving
    before = read(first, path)
    line = before["lines"][0]["id"]
    line_path = f"{path}/lines/{line}"
    payload = {"outcome": "matched", "reason": reason}
    checked = first.patch(line_path, payload)
    assert checked.status_code == 200, checked.content
    result = checked.json()
    assert (result["found"], result["line"]) == ("recorded", line)
    assert row(result["receipt"], line)["checked_by"]["email"] == "tech@example.com"
    observed = timeline(first, path, line_id=line)
    assert len(observed) == 1 and observed[0]["reason"] == reason
    again = second.patch(line_path, payload)
    assert again.status_code == 200, again.content
    assert again.json() == {**result, "found": "again"}
    assert timeline(second, path, line_id=line) == observed


@pytest.mark.parametrize("code,outcome", [("RC000", "matched"), ("EXTRA-71", "extra")])
@pytest.mark.parametrize(
    "first_reason", [{}, {"reason": None}, {"reason": ""}], ids=["omitted", "null", "empty"]
)
def test_a_first_condition_note_preserves_the_observer_and_later_corrections_need_a_reason(
    receiving: Receiving, code: str, outcome: str, first_reason: dict[str, str | None]
) -> None:
    first, second, path = receiving
    scan = change(first, "POST", path + "/scan", code=code)
    line_id = scan["line"]
    line_path = f"{path}/lines/{line_id}"
    checked = row(scan["receipt"], line_id)
    assert checked["outcome"] == outcome and checked["note"] == ""

    response = second.patch(
        line_path,
        {
            "outcome": outcome,
            "note": "Blue cap",
            **first_reason,
        },
        headers=seen(checked["revision"]),
    )
    assert response.status_code == 200, response.content
    assert response.json()["found"] == "recorded"
    receipt = response.json()["receipt"]
    noted = row(receipt, line_id)
    assert noted["revision"] == checked["revision"] + 1
    assert receipt["revision"] == scan["receipt"]["revision"] + 1
    assert (
        noted["checked_by"] == checked["checked_by"]
        and noted["checked_at"] == checked["checked_at"]
    )
    observed, added = timeline(first, path, line_id=line_id)
    assert added["previous"] == observed["id"] and added["before"] == observed["after"]
    assert added["after"]["note"] == "Blue cap" and added["reason"] == ""
    assert added["actor"]["email"] == "core_qa@example.com"
    assert (
        added["after"]["checked_by"]
        == observed["after"]["checked_by"]
        == checked["checked_by"]["id"]
    )

    change(first, "PATCH", line_path, checked, status=409, outcome=outcome, note="Different cap")
    for attempted in ("Red cap", ""):
        refusal = change(
            first,
            "PATCH",
            line_path,
            noted,
            status=422,
            outcome=outcome,
            note=attempted,
            reason=" ",
        )
        assert any(error["pointer"] == "#/reason" for error in refusal["errors"])
    assert read(first, path) == receipt
    assert timeline(first, path, line_id=line_id) == [observed, added]

    corrected = change(
        first,
        "PATCH",
        line_path,
        noted,
        outcome=outcome,
        note="Red cap",
        reason="Inspected in daylight",
    )
    assert row(corrected, line_id)["note"] == "Red cap"
    replaced = timeline(second, path, line_id=line_id)[-1]
    assert replaced["previous"] == added["id"] and replaced["before"] == added["after"]
    assert replaced["after"]["note"] == "Red cap" and replaced["reason"] == "Inspected in daylight"


@pytest.mark.parametrize("clear", [None, "", " \t "], ids=["null", "empty", "whitespace"])
def test_an_explicit_note_clear_needs_its_review_and_reason_but_omission_retains_it(
    receiving: Receiving, clear: str | None
) -> None:
    first, second, path = receiving
    line = read(first, path)["lines"][0]["id"]
    line_path = f"{path}/lines/{line}"
    initial = change(first, "PATCH", line_path, outcome="damaged", note="Blue cap")
    reviewed = change(
        second, "PATCH", line_path, row(initial, line), outcome="matched", reason="Checked the seal"
    )
    saved = row(reviewed, line)
    assert saved["outcome"] == "matched" and saved["note"] == "Blue cap"
    events = timeline(first, path, line_id=line)
    assert events[-1]["before"]["note"] == events[-1]["after"]["note"] == "Blue cap"

    amendment = {"outcome": "matched", "note": clear}
    for preconditions in (
        {},
        {"reason": "Note was a transcription error"},
        {"revision": saved["revision"]},
        {"revision": saved["revision"], "reason": None},
        {"revision": saved["revision"], "reason": ""},
        {"revision": saved["revision"], "reason": "   "},
    ):
        refusal = first.patch(line_path, {**amendment, **preconditions})
        assert refusal.status_code == 422, refusal.content
    assert read(first, path) == reviewed and timeline(second, path, line_id=line) == events

    cleared = change(
        first,
        "PATCH",
        line_path,
        saved,
        outcome="matched",
        note=clear,
        reason="Note was a transcription error",
    )
    assert row(cleared, line)["note"] == ""
    assert row(cleared, line)["checked_by"] == saved["checked_by"]
    removed = timeline(second, path, line_id=line)[-1]
    assert removed["before"] == events[-1]["after"] and removed["after"]["note"] == ""
    assert (
        removed["previous"] == events[-1]["id"]
        and removed["reason"] == "Note was a transcription error"
    )


@pytest.mark.parametrize("clear", [None, "", "   "], ids=["null", "empty", "whitespace"])
def test_handoff_note_is_preserved_on_omission_and_needs_a_reason_to_clear(
    receiving: Receiving, clear: str | None
) -> None:
    first, second, path = receiving
    receipt = change(first, "PATCH", path, read(first, path), note="Dry ice", reason=None)
    handoff = change(second, "PATCH", path, receipt, handed_over_by="Courier")
    assert handoff["note"] == "Dry ice"
    previous = timeline(first, path)
    refused = change(first, "PATCH", path, handoff, status=422, note=clear, reason=None)
    assert any(field["pointer"] == "#/reason" for field in refused["errors"])
    assert read(second, path) == handoff and timeline(second, path) == previous
    cleared = change(first, "PATCH", path, handoff, note=clear, reason="Correcting the handoff")
    assert cleared["note"] == "" and cleared["handed_over_by"] == "Courier"
    events = timeline(second, path)
    assert events[:-1] == previous and events[-1]["reason"] == "Correcting the handoff"
    assert events[-1]["before"]["note"] == "Dry ice" and events[-1]["after"]["note"] == ""

"""A stale receipt command names the current lines the operator needs to review."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from api_client import problem
from django.db import connection, transaction
from test_receipt_observation_history import JsonObject, Receiving, change, read, row, timeline
from test_receipt_observation_history import lab as lab
from test_receipt_observation_history import receiving as receiving

pytestmark = pytest.mark.django_db


def version(line: JsonObject) -> JsonObject:
    return {"line_id": line["id"], "line_revision": line["line_revision"]}


def test_a_stale_line_command_names_only_its_current_line_including_a_removed_extra(
    receiving: Receiving,
) -> None:
    first, second, path = receiving
    opened = read(second, path)
    scanned = change(first, "POST", path + "/scan", code="RC000")
    target = f"{path}/lines/{scanned['line']}"
    original = row(opened, scanned["line"])
    current = row(scanned["receipt"], scanned["line"])
    extra = change(first, "POST", path + "/scan", code="EXTRA-RED")
    old_extra = row(extra["receipt"], extra["line"])
    extra_path = f"{path}/lines/{extra['line']}"
    removed = change(first, "DELETE", extra_path, old_extra, reason="Duplicated on the waybill")
    retired = removed["removed_lines"][0]
    history = read(first, path + "/history")

    for method, url, line, fields, latest in (
        ("PATCH", target, original, {"outcome": None, "reason": "Review check"}, current),
        ("DELETE", extra_path, old_extra, {"reason": "Revisit"}, retired),
        ("POST", extra_path + "/restore", old_extra, {"reason": "Revisit"}, retired),
    ):
        error = change(second, method, url, line, status=409, **fields)
        assert error["code"] == "refused"
        assert error["affected_lines"] == [version(latest)]
    assert read(second, path) == removed and read(second, path + "/history") == history


def test_parent_conflicts_name_each_line_changed_since_review_at_its_latest_revision(
    receiving: Receiving,
) -> None:
    first, second, path = receiving
    # The first tube was already checked when the second receiver reviewed the parent.
    reviewed = change(first, "POST", path + "/scan", code="RC000")["receipt"]
    checked = change(first, "POST", path + "/scan", code="RC001")
    line = row(checked["receipt"], checked["line"])
    corrected = change(
        first,
        "PATCH",
        f"{path}/lines/{line['id']}",
        line,
        outcome="damaged",
        note="Cracked cap",
        reason="Inspected the tube",
    )
    extra = change(first, "POST", path + "/scan", code="EXTRA-RED")
    extra_line = row(extra["receipt"], extra["line"])
    removed = change(
        first,
        "DELETE",
        f"{path}/lines/{extra['line']}",
        extra_line,
        reason="Different delivery",
    )
    current = change(first, "PATCH", path, removed, handed_over_by="Courier")
    history = read(first, path + "/history")
    expected = sorted(
        [version(row(corrected, line["id"])), version(removed["removed_lines"][0])],
        key=lambda item: item["line_id"],
    )

    for method, url, payload in (
        ("PATCH", path, {"note": "Unpacked"}),
        ("POST", path + "/cancel", {"reason": "Check delivery"}),
        ("POST", path + "/complete", {}),
    ):
        changed = change(second, method, url, reviewed, status=409, **payload)
        assert changed["code"] == "refused" and changed["affected_lines"] == expected
        # Only the handoff changed since 'removed'; none of its earlier lines changed again.
        handoff = change(second, method, url, removed, status=409, **payload)
        assert handoff["code"] == "refused" and handoff["affected_lines"] == []
    assert read(second, path) == current and read(second, path + "/history") == history


@pytest.mark.django_db(transaction=True)
def test_stale_handoff_waits_for_a_new_line_and_reports_its_committed_revision(
    receiving: Receiving,
) -> None:
    scanner, receiver, path = receiving
    reviewed = read(receiver, path)

    def handoff() -> JsonObject:
        try:
            return problem(
                receiver.patch(
                    path,
                    {"plan_revision": reviewed["plan_revision"], "handed_over_by": "Courier 341"},
                ),
                409,
                "refused",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            scan = change(scanner, "POST", path + "/scan", code="EXTRA-52")
            assert scan["found"] == "extra"
            added = row(scan["receipt"], scan["line"])
            assert added["id"] not in {line["id"] for line in reviewed["lines"]}
            waiting = pool.submit(handoff)
            # Hold the scan uncommitted until PostgreSQL confirms the other receiver
            # is blocked on this writer. The two HTTP calls use separate connections.
            deadline = time.monotonic() + 15
            with connection.cursor() as cursor:
                while True:
                    cursor.execute(
                        "SELECT EXISTS (SELECT FROM pg_locks WHERE NOT granted "
                        "AND pg_backend_pid() = ANY(pg_blocking_pids(pid)))"
                    )
                    if cursor.fetchone()[0]:
                        break
                    assert not waiting.done(), "The handoff finished before the scan committed."
                    assert time.monotonic() < deadline, "The handoff did not block on the scan."
                    time.sleep(0.01)
        refusal = waiting.result(timeout=15)

    assert refusal["affected_lines"] == [
        {"line_id": added["id"], "line_revision": added["line_revision"]}
    ]
    current = read(receiver, path)
    assert current == scan["receipt"] and current["handed_over_by"] == ""
    (event,) = timeline(receiver, path)
    assert event["line_id"] == added["id"] and event["revision"] == current["plan_revision"]
    assert event["actor"]["email"] == added["checked_by"]["email"] == "tech@example.com"

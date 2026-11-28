"""Separate reviewers may act only on the import, preview and saved template they saw."""

import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from api_client import problem
from browser import Response
from django.db import connection, connections, transaction
from org_world import World
from test_spreadsheet_imports import (
    SAMPLE,
    api_as,
    committed,
    previewed,
    records,
    rows,
    seen,
    settle,
    started,
    storage,
    world,
)
from workbook_files import xlsx

from iki_server.audit.models import AuditEvent
from iki_server.runs.models import ActionRun, ExternalOrigin
from iki_server.spreadsheets.models import PreviewRow, SpreadsheetImport

__all__ = ["storage", "world"]  # shared test fixtures

pytestmark = pytest.mark.django_db


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["change", "preview"])
def test_a_draft_command_returns_the_revision_it_accepted(
    world: World, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from iki_server.spreadsheets import services as sheets

    source = started(world, xlsx(*((name, [["Label"], [name]]) for name in ("Cover", "A", "B"))))
    path = f"/spreadsheet-imports/{source['id']}"
    tab_a, tab_b = api_as(world), api_as(world)
    saved, respond = Event(), Event()
    action = getattr(sheets, operation)

    def pause_after_save(*args: Any, **kwargs: Any) -> Any:
        result = action(*args, **kwargs)
        if not saved.is_set():
            saved.set()
            assert respond.wait(timeout=15)
        return result

    monkeypatch.setattr(sheets, operation, pause_after_save)

    def first_tab() -> dict[str, Any]:
        try:
            if operation == "change":
                result = tab_a.patch(path, {"sheet": "A"}, headers=seen(source))
            else:
                result = tab_a.post(f"{path}/preview", headers=seen(source))
            assert result.status_code in (200, 202), result.content
            body: dict[str, Any] = result.json()
            assert result["ETag"] == seen(body)["If-Match"]
            return body
        finally:
            connections.close_all()

    def choose_b() -> None:
        response = tab_b.patch(path, {"sheet": "B"}, headers=seen(tab_b.get(path).json()))
        assert response.status_code == 200, response.content

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_tab)
        try:
            assert saved.wait(timeout=15)
            with transaction.atomic():
                available = (
                    SpreadsheetImport.objects.select_for_update(skip_locked=True)
                    .filter(pk=source["id"])
                    .exists()
                )
                if available:
                    choose_b()
        finally:
            respond.set()
        accepted = first.result(timeout=15)
    if not available:
        choose_b()
    assert tab_b.get(path).json()["sheet"] == "B"
    assert accepted["sheet"] == ("A" if operation == "change" else "Cover")
    assert accepted["revision"] == source["revision"] + 1


def test_a_second_preview_of_the_same_mapping_needs_its_own_approval(world: World) -> None:
    source = started(world, b"Label,Purity (%)\nFree,90\nClaimed,80\nBad,none\n", name="batch.csv")
    tab_a, tab_b = api_as(world), api_as(world)
    url = f"/spreadsheet-imports/{source['id']}"
    first = previewed(world, source)
    assert [r["row"] for r in rows(world, first, "problem")] == [4]
    first_page = tab_a.get(f"{url}/rows", headers=seen(first), limit=1)
    assert first_page.status_code == 200 and first_page.json()["items"][0]["row"] == 2
    cursor = first_page.json()["next_cursor"]
    assert cursor

    # Once another operator creates this label, unchanged mapping choices produce a different
    # preview. Both reviewers can legitimately approve skipping the problems they actually saw.
    existing = api_as(world, "via_team").post(
        "/entities",
        {"type": SAMPLE, "project": str(world.project.pk), "values": {"label": "Claimed"}},
    )
    assert existing.status_code == 201, existing.content
    pending = tab_b.post(f"{url}/preview", headers=seen(first))
    assert pending.status_code == 202, pending.content
    settle()
    current = tab_b.get(url).json()
    assert [r["row"] for r in rows(world, current, "problem")] == [3, 4]
    before = SpreadsheetImport.objects.filter(pk=source["id"]).values().get()
    stale = tab_a.post(
        f"{url}/commit",
        {"skip_problems": True, "reason": "Approved one excluded row"},
        headers=seen(first),
    )
    problem(stale, 412, "stale")
    assert stale["ETag"] == pending["ETag"] == seen(current)["If-Match"]
    for params in ({}, {"cursor": cursor}):
        obsolete = tab_a.get(f"{url}/rows", headers=seen(first), **params)
        problem(obsolete, 412, "stale")
        assert obsolete["ETag"] == stale["ETag"]
    legacy = tab_b.get(f"{url}/rows", outcome="problem")
    assert [r["row"] for r in legacy.json()["items"]] == [3, 4]
    assert current["revision"] == first["revision"] + 1 == pending.json()["revision"]
    settle()
    assert SpreadsheetImport.objects.filter(pk=source["id"]).values().get() == before
    assert list(records(world)) == ["Claimed"]
    assert not ActionRun.objects.exists() and not ExternalOrigin.objects.exists()

    reviewed = tab_a.get(url).json()
    done = committed(world, reviewed, skip_problems=True, reason="Approved two excluded rows")
    assert done["state"] == "imported"
    run = ActionRun.objects.get(pk=done["run"]["id"])
    assert run.reason == "Approved two excluded rows"
    saved = records(world)
    assert set(saved) == {"Free", "Claimed"} and str(saved["Claimed"].pk) == existing.json()["id"]
    (origin,) = ExternalOrigin.objects.filter(run=run)
    assert origin.entity_id == saved["Free"].pk and origin.identifier.endswith("row 2")


@pytest.mark.django_db(transaction=True)
def test_a_preview_can_finish_while_a_page_of_superseded_rows_is_being_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iki_server.api import spreadsheets

    source = started(world, b"Label\nFirst\nSecond\n", name="rows.csv")
    reviewed = previewed(world, source)
    first, second = api_as(world), api_as(world)
    path = f"/spreadsheet-imports/{source['id']}"
    reading, release = Event(), Event()
    serialize = spreadsheets._row_out

    def pause(row: PreviewRow) -> dict[str, Any]:
        reading.set()
        assert release.wait(timeout=15), "The other tab did not finish its preview."
        return serialize(row)

    monkeypatch.setattr(spreadsheets, "_row_out", pause)

    def read_page() -> Response:
        try:
            return first.get(f"{path}/rows", headers=seen(reviewed), limit=1)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(read_page)
        try:
            assert reading.wait(timeout=15), "The first tab did not reach the reviewed page."
            # A reader holding a draft lock would block the other tab or its skip_locked worker.
            with transaction.atomic():
                assert (
                    SpreadsheetImport.objects.select_for_update(skip_locked=True)
                    .filter(pk=source["id"])
                    .exists()
                )
                restarted = second.post(f"{path}/preview", headers=seen(reviewed))
            assert restarted.status_code == 202, restarted.content
            settle()
            latest = second.get(path).json()
            assert latest["state"] == "previewed" and latest["revision"] > reviewed["revision"]
        finally:
            release.set()
        obsolete = pending.result(timeout=15)
    refusal = problem(obsolete, 412, "stale")
    assert "items" not in refusal and obsolete["ETag"] == seen(latest)["If-Match"]


def test_a_new_settings_revision_never_shows_the_previous_previews_rows(world: World) -> None:
    source = started(world, b"Label\nFirst\nSecond\n", name="rows.csv")
    api = api_as(world)
    path = f"/spreadsheet-imports/{source['id']}"
    reviewed = previewed(world, source)
    first_page = api.get(f"{path}/rows", headers=seen(reviewed), limit=1).json()
    assert first_page["items"][0]["row"] == 2 and first_page["next_cursor"]
    changed = api.patch(path, {"first_row": 3}, headers=seen(reviewed))
    assert changed.status_code == 200, changed.content
    current = changed.json()
    assert current["state"] == "mapping" and current["preview"] is None
    # Rows are cleared by a background task; the former preview is still in the database.
    assert PreviewRow.objects.filter(spreadsheet_import_id=source["id"]).count() == 2
    for params in ({}, {"cursor": first_page["next_cursor"]}):
        result = api.get(f"{path}/rows", headers=seen(current), **params)
        assert result.status_code == 200, result.content
        assert result.json() == {"items": [], "next_cursor": None}


def test_saving_a_template_after_preview_changes_the_approval_revision(world: World) -> None:
    source = started(world, b"Label\nSample 1\n", name="samples.csv")
    checked = previewed(world, source)
    assert checked["mapping"] is None
    first, second = api_as(world), api_as(world)
    url = f"/spreadsheet-imports/{source['id']}"
    created = second.post(f"{url}/mappings", {"name": "Sample manifest"}, headers=seen(checked))
    assert created.status_code == 201, created.content
    before = SpreadsheetImport.objects.filter(pk=source["id"]).values().get()
    late = first.post(
        f"{url}/commit", {"reason": "Reviewed without a template"}, headers=seen(checked)
    )
    problem(late, 412, "stale")
    settle()
    assert SpreadsheetImport.objects.filter(pk=source["id"]).values().get() == before
    assert not ActionRun.objects.exists() and not records(world)
    fresh = first.get(url).json()
    assert fresh["state"] == "previewed" and fresh["mapping"]["id"] == created.json()["id"]
    done = committed(world, fresh)
    assert done["state"] == "imported" and done["applied_mapping"]["template"] == fresh["mapping"]


def test_replacing_a_template_reviews_both_import_and_template_independently(world: World) -> None:
    alice, bob = api_as(world), api_as(world, "via_team")
    source_a = started(world, b"Label,Purity (%)\nA,96\n", name="a.csv")
    source_b = started(world, b"Label\nB\n", name="b.csv", principal="via_team")
    path_a = f"/spreadsheet-imports/{source_a['id']}"
    listing = f"/projects/{world.project.pk}/spreadsheet-mappings"
    audits = AuditEvent.objects.filter(action__startswith="spreadsheet_mapping.")
    saved = alice.post(f"{path_a}/mappings", {"name": "Layout A"}, headers=seen(source_a))
    assert saved.status_code == 201, saved.content
    (target,) = bob.every(listing)
    assert target == saved.json() and target["revision"] == 1
    template = f"/spreadsheet-mappings/{target['id']}"
    reviewed_a = alice.get(path_a).json()
    assert reviewed_a["mappings"][0]["revision"] == target["revision"]

    def from_import(name: str, imported: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "import": imported["id"], "import_revision": imported["revision"]}

    replaced = bob.put(template, from_import("Layout B", source_b), headers=seen(target))
    assert replaced.status_code == 200, replaced.content
    latest = replaced.json()
    assert latest["revision"] == 2 and latest["headers"] == ["Label"]
    assert replaced["ETag"] == '"2"' and latest["id"] == target["id"]
    assert latest["updated_by"]["id"] == str(world.users["via_team"].pk)
    before = SpreadsheetImport.objects.filter(pk=source_a["id"]).values().get()
    assert before["revision"] == reviewed_a["revision"]

    # Bob changed the shared template using his own import; Alice's import is still current,
    # but the template she agreed to replace isn't.
    obsolete = alice.put(template, from_import("Late A", reviewed_a), headers=seen(target))
    assert "Layout B" in problem(obsolete, 412, "stale")["detail"]
    assert obsolete["ETag"] == seen(latest)["If-Match"]
    assert alice.every(listing) == [latest] and audits.count() == 2
    assert SpreadsheetImport.objects.filter(pk=source_a["id"]).values().get() == before

    # Conversely, changing the import can't be hidden by a current template precondition.
    current_target = alice.every(listing)[0]
    updated = alice.patch(
        path_a, {"columns": [{"index": 0, "field": "label"}]}, headers=seen(reviewed_a)
    )
    assert updated.status_code == 200, updated.content
    current_a = updated.json()
    late_create = alice.post(
        f"{path_a}/mappings", {"name": "Late create"}, headers=seen(reviewed_a)
    )
    problem(late_create, 412, "stale")
    assert late_create["ETag"] == seen(current_a)["If-Match"]
    late_replace = alice.put(
        template, from_import("Late replace", reviewed_a), headers=seen(current_target)
    )
    problem(late_replace, 412, "stale")
    # The import is named in the body: the ETag answered is the template's, so none is.
    assert "ETag" not in late_replace.headers
    assert alice.every(listing) == [latest] and audits.count() == 2

    fresh = alice.put(
        template, from_import("Reviewed again", current_a), headers=seen(current_target)
    )
    assert fresh.status_code == 200, fresh.content
    changed = fresh.json()
    assert changed["revision"] == 3 and changed["id"] == latest["id"]
    assert changed["name"] == "Reviewed again"
    assert changed["updated_by"] == target["created_by"] and audits.count() == 3


def test_a_template_replacement_requires_the_reviewed_template_and_import(
    world: World,
) -> None:
    api = api_as(world)
    source = started(world, b"Label\none\n", name="source.csv")
    path = f"/spreadsheet-imports/{source['id']}"
    before = api.post(f"{path}/mappings", {"name": "Saved"}, headers=seen(source))
    assert before.status_code == 201, before.content
    template = before.json()
    url = f"/spreadsheet-mappings/{template['id']}"
    source = api.get(path).json()
    body = {"name": "Overwrite", "import": source["id"], "import_revision": source["revision"]}
    for fields in (
        {"import_revision": None},
        {"import_revision": 0},
        {"import_revision": True},
        {"import_revision": "1"},
        {"import": None},
    ):
        attempt = {k: v for k, v in {**body, **fields}.items() if v is not None}
        bad = api.put(url, attempt, headers=seen(template))
        assert bad.status_code == 422, bad.content
    for headers in (None, {"If-Match": "*"}, {"If-Match": 'W/"1"'}):
        problem(api.put(url, body, headers=headers), 428, "precondition_required")
    duplicate = api.post(f"{path}/mappings", {"name": "Saved"}, headers=seen(source))
    assert problem(duplicate, 422, "validation_failed")["errors"][0]["pointer"] == "#/name"
    # The way a mapping was once replaced is refused, not taken as a new mapping.
    old_way = {"name": "Overwrite", "replace": template["id"], "target_revision": 1}
    refused = problem(
        api.post(f"{path}/mappings", old_way, headers=seen(source)), 422, "validation_failed"
    )
    assert {e["pointer"] for e in refused["errors"]} == {"#/replace", "#/target_revision"}
    assert api.every(f"/projects/{world.project.pk}/spreadsheet-mappings") == [template]
    assert AuditEvent.objects.filter(action__startswith="spreadsheet_mapping.").count() == 1
    assert api.get(path).json() == source


def blocked_by_this_transaction(work: Future[Any]) -> bool:
    """Did the competing HTTP request wait for this transaction, or finish before its commit?"""
    deadline = time.monotonic() + 15
    with connection.cursor() as cursor:
        while not work.done():
            cursor.execute(
                "SELECT EXISTS (SELECT FROM pg_locks WHERE NOT granted "
                "AND pg_backend_pid() = ANY(pg_blocking_pids(pid)))"
            )
            if cursor.fetchone()[0]:
                return True
            assert time.monotonic() < deadline, (
                "The competing request neither finished nor blocked."
            )
            time.sleep(0.01)
    return False


@pytest.mark.django_db(transaction=True)
def test_competing_creates_of_a_template_name_leave_the_losing_source_untouched(
    world: World,
) -> None:
    first, other = api_as(world), api_as(world, "via_team")
    a = started(world, b"Label\nOne\n", name="one.csv")
    b = started(world, b"Label\nTwo\n", name="two.csv", principal="via_team")
    path_b = f"/spreadsheet-imports/{b['id']}"
    before = SpreadsheetImport.objects.filter(pk=b["id"]).values().get()

    def save_from_b() -> dict[str, Any]:
        try:
            result = other.post(f"{path_b}/mappings", {"name": "Shared"}, headers=seen(b))
            return problem(result, 422, "validation_failed")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            saved = first.post(
                f"/spreadsheet-imports/{a['id']}/mappings", {"name": "Shared"}, headers=seen(a)
            )
            assert saved.status_code == 201, saved.content
            losing = pool.submit(save_from_b)
            assert blocked_by_this_transaction(losing)
        error = losing.result(timeout=15)
    assert error["errors"][0]["pointer"] == "#/name"
    assert SpreadsheetImport.objects.filter(pk=b["id"]).values().get() == before
    assert first.every(f"/projects/{world.project.pk}/spreadsheet-mappings") == [saved.json()]
    assert AuditEvent.objects.filter(action="spreadsheet_mapping.saved").count() == 1


@pytest.mark.django_db(transaction=True)
def test_applying_a_template_racing_its_deletion_keeps_only_the_snapshot_that_was_read(
    world: World,
) -> None:
    owner, editor = api_as(world), api_as(world, "via_team")
    a = started(world, b"Label\nOne\n", name="one.csv")
    b = started(world, b"Label\nTwo\n", name="two.csv", principal="via_team")
    path_a, path_b = (f"/spreadsheet-imports/{s['id']}" for s in (a, b))
    made = owner.post(f"{path_a}/mappings", {"name": "Shared"}, headers=seen(a))
    assert made.status_code == 201, made.content
    target = made.json()
    reviewed = owner.get(path_a).json()

    def apply() -> Response:
        try:
            return editor.patch(path_b, {"mapping": target["id"]}, headers=seen(b))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            deleted = owner.delete(f"/spreadsheet-mappings/{target['id']}", headers=seen(target))
            assert deleted.status_code == 204, deleted.content
            call = pool.submit(apply)
            blocked_by_this_transaction(call)
        applied = call.result(timeout=15)

    current_a = owner.get(path_a).json()
    assert current_a["mapping"] == reviewed["mapping"]
    assert current_a["revision"] == reviewed["revision"] and current_a["mappings"] == []
    current_b = editor.get(path_b).json()
    if applied.status_code == 200:
        assert current_b["mapping"] == reviewed["mapping"]
        assert current_b["revision"] == b["revision"] + 1
    else:
        assert problem(applied, 422, "validation_failed")["errors"][0]["pointer"] == "#/mapping"
        assert current_b["mapping"] is None and current_b["revision"] == b["revision"]
    assert SpreadsheetImport.objects.get(pk=b["id"]).mapping_source == current_b["mapping"]

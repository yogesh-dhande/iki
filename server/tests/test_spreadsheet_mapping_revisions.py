"""Saving a project template uses the source and, for a replacement, target reviewed."""

import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier
from typing import Any

import pytest
from api_client import Api, problem
from browser import Response
from django.db import connection, connections, transaction
from org_world import World, build_world
from test_spreadsheet_imports import SAMPLE, api_as, settle, started
from workbook_files import xlsx

from iki_server.audit.models import AuditEvent
from iki_server.entities.values import Invalid
from iki_server.spreadsheets import services
from iki_server.spreadsheets.models import SavedMapping, SpreadsheetImport

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("s3")]


@pytest.fixture
def world() -> World:
    return build_world()


def url(source: dict[str, Any]) -> str:
    return f"/spreadsheet-imports/{source['id']}"


def save(
    api: Api, source: dict[str, Any], name: str, *, replace: dict[str, Any] | None = None
) -> Response:
    body = {"name": name, "source_revision": source["mapping_revision"]}
    if replace is not None:
        body.update(replace=replace["id"], target_revision=replace["revision"])
    return api.post(f"{url(source)}/mappings", body)


def backend_pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


def wait_for_blocked_transaction(waiter: int) -> None:
    """The competing request reached the database before this transaction commits."""
    until = time.monotonic() + 10
    with connection.cursor() as cursor:
        while time.monotonic() < until:
            cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [waiter])
            if cursor.fetchone()[0]:
                return
            time.sleep(0.01)
    pytest.fail("The competing transaction did not wait for this transaction.")


def test_a_changed_source_cannot_create_or_overwrite_a_template(world: World) -> None:
    tab_a, tab_b = api_as(world), api_as(world)
    book = xlsx(("A", [["A local label"], ["Sample A"]]), ("B", [["B vendor label"], ["Sample B"]]))
    draft = started(world, book)
    path = url(draft)
    columns = [{"index": 0, "field": "label"}]
    source = tab_a.patch(path, {"sheet": "A", "columns": columns, "locale": {"decimal": "point"}})
    first = save(tab_a, source.json(), "A manifest")
    assert first.status_code == 201, first.content
    template = first.json()
    assert template["headers"] == ["A local label"] and template["locale"]["decimal"] == "point"
    reviewed = tab_a.get(path).json()
    after = tab_b.patch(path, {"sheet": "B", "columns": columns, "locale": {"decimal": "comma"}})
    assert after.status_code == 200, after.content
    current = after.json()
    assert current["sheet"] == "B" and current["mapping_revision"] > reviewed["mapping_revision"]
    listing = f"/projects/{world.project.pk}/spreadsheet-mappings"
    audit = AuditEvent.objects.filter(action__startswith="spreadsheet_mapping.")

    # The target hasn't changed. On the old API the replacement returned 201 and
    # stored B's header and comma convention under A's template name and ID.
    for response in (
        save(tab_a, reviewed, "A manifest", replace=template),
        save(tab_a, reviewed, "Another manifest"),
    ):
        detail = problem(response, 409, "refused")["detail"]
        assert "source" in detail and "“B”" in detail and "header row 1" in detail
        assert f"revision {current['mapping_revision']}" in detail
    assert tab_a.get(listing).json() == [template]
    assert tab_a.get(path).json() == current
    assert list(audit.values_list("action", flat=True)) == ["spreadsheet_mapping.saved"]

    fresh = save(tab_a, tab_a.get(path).json(), "B manifest", replace=template)
    assert fresh.status_code == 201, fresh.content
    updated = fresh.json()
    assert (updated["id"], updated["revision"]) == (template["id"], template["revision"] + 1)
    assert updated["headers"] == ["B vendor label"] and updated["locale"]["decimal"] == "comma"
    assert updated["created_by"] == template["created_by"] == updated["updated_by"]
    assert tab_a.get(listing).json() == [updated]
    assert tab_a.get(path).json()["mapping"]["revision"] == updated["revision"]
    assert audit.count() == 2


@pytest.mark.parametrize(
    "setting",
    [
        {"columns": [{"index": 1, "field": "label"}]},
        {"locale": {"decimal": "comma"}},
        {"header_row": 2},
    ],
)
def test_settings_on_the_same_sheet_invalidate_a_template_review(
    world: World, setting: dict[str, Any]
) -> None:
    a, b = api_as(world), api_as(world)
    draft = started(world, xlsx(("Main", [["Label", "Lot"], ["one", "two"], ["three", "four"]])))
    path = url(draft)
    reviewed = a.get(path).json()
    # Changing the imported row range or starting a preview doesn't change the template.
    assert (
        b.patch(path, {"sheet": "Main", "last_row": 3}).json()["mapping_revision"]
        == reviewed["mapping_revision"]
    )
    assert b.post(f"{path}/preview").json()["mapping_revision"] == reviewed["mapping_revision"]
    after = b.patch(path, {"sheet": "Main", **setting})
    assert after.status_code == 200, after.content
    assert after.json()["mapping_revision"] > reviewed["mapping_revision"]
    assert save(a, reviewed, "Main template").status_code == 409
    assert not SavedMapping.objects.exists()
    assert not AuditEvent.objects.filter(action__startswith="spreadsheet_mapping.").exists()


@pytest.mark.django_db(transaction=True)
def test_another_editor_updated_the_target_while_the_source_stayed_the_same(world: World) -> None:
    editor, admin = api_as(world), api_as(world, "admin")
    draft = started(world, xlsx(("Orders", [["Label"], ["Sample"]])))
    initial = save(editor, draft, "Orders")
    assert initial.status_code == 201, initial.content
    template = initial.json()
    reviewed = editor.get(url(draft)).json()
    project = f"/projects/{world.project.pk}"

    other = admin.post(
        f"{project}/spreadsheet-imports", {"type": SAMPLE, "file": draft["file"]["id"]}
    )
    assert other.status_code == 201, other.content
    settle()
    other_path = url(other.json())
    theirs = admin.patch(other_path, {"sheet": "Orders", "locale": {"decimal": "comma"}}).json()
    waiting: Queue[int] = Queue()

    def stale_request() -> Response:
        try:
            with transaction.atomic():
                waiting.put(backend_pid())
                return save(editor, reviewed, "Orders", replace=template)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            competing = save(admin, theirs, "Revised orders", replace=template)
            assert competing.status_code == 201, competing.content
            future = pool.submit(stale_request)
            wait_for_blocked_transaction(waiting.get(timeout=10))
        refused = future.result(timeout=10)
    changed = competing.json()
    assert changed["created_by"] == template["created_by"] != changed["updated_by"]
    assert changed["updated_by"]["id"] == str(world.users["admin"].pk)
    detail = problem(refused, 409, "refused")["detail"]
    assert "Revised orders" in detail and f"revision {changed['revision']}" in detail
    assert editor.get(f"{project}/spreadsheet-mappings").json() == [changed]
    assert AuditEvent.objects.filter(action="spreadsheet_mapping.changed").count() == 1
    source = editor.get(url(draft)).json()
    assert source["mapping_revision"] == reviewed["mapping_revision"]
    assert source["mapping"]["id"] == template["id"] and source["locale"] == reviewed["locale"]

    current = save(editor, source, "Orders", replace=changed)
    assert current.status_code == 201, current.content
    restored = current.json()
    assert restored["revision"] == changed["revision"] + 1
    assert restored["id"] == template["id"] and restored["locale"] == template["locale"]
    assert restored["created_by"] == restored["updated_by"] == template["created_by"]
    assert editor.get(f"{project}/spreadsheet-mappings").json() == [restored]


def test_writes_require_the_observed_revisions_and_refuse_ill_formed_pairs(world: World) -> None:
    api = api_as(world)
    source = started(world, xlsx(("A", [["Label"], ["sample"]])))
    # A real but otherwise unrelated UUID exercises the required-field checks.
    for body in (
        {},
        {"source_revision": 0},
        {"source_revision": True},
        {"source_revision": "1"},
        {"source_revision": source["mapping_revision"], "replace": source["id"]},
        {"source_revision": source["mapping_revision"], "target_revision": 1},
        {
            "source_revision": source["mapping_revision"],
            "replace": source["id"],
            "target_revision": 0,
        },
    ):
        assert api.post(f"{url(source)}/mappings", {"name": "Saved", **body}).status_code == 422
    assert not SavedMapping.objects.exists()
    assert api.get(url(source)).json()["mapping"] is None
    outside = SavedMapping.objects.create(
        project=world.archived,
        type=SAMPLE,
        name="Another project's template",
        columns=[],
        created_by=world.users["admin"],
        updated_by=world.users["admin"],
    )
    foreign = save(api, source, "Overwrite", replace={"id": str(outside.pk), "revision": 1})
    assert foreign.status_code == 404 and outside.name not in foreign.content.decode()
    outside.refresh_from_db()
    assert outside.revision == 1 and outside.name == "Another project's template"
    assert api.get(url(source)).json()["mapping"] is None
    assert not AuditEvent.objects.filter(action__startswith="spreadsheet_mapping.").exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_creates_of_the_same_name_link_only_the_winning_source(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = [started(world, xlsx((name, [["Label"], [name]]))) for name in ("A", "B")]
    saving = Barrier(2, timeout=10)
    original_save = SavedMapping.save

    def save_together(saved: SavedMapping, *args: Any, **kwargs: Any) -> None:
        saving.wait()
        original_save(saved, *args, **kwargs)

    monkeypatch.setattr(SavedMapping, "save", save_together)

    def create(draft: dict[str, Any]) -> int:
        try:
            return save(api_as(world), draft, "Same name").status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, drafts))
    assert sorted(results) == [201, 422]
    saved = SavedMapping.objects.get()
    for draft, status in zip(drafts, results, strict=True):
        persisted = SpreadsheetImport.objects.get(pk=draft["id"])
        if status == 201:
            assert persisted.mapping_id == saved.pk
        else:
            assert (
                persisted.mapping_id is None
                and persisted.mapping_revision == draft["mapping_revision"]
            )
    assert AuditEvent.objects.filter(action="spreadsheet_mapping.saved").count() == 1


@pytest.mark.django_db(transaction=True)
def test_deleting_a_template_invalidates_linked_sources_and_blocks_a_fresh_link(
    world: World,
) -> None:
    api = api_as(world)
    first = started(world, xlsx(("A", [["Label"], ["Sample A"]])))
    other = started(world, xlsx(("B", [["Label"], ["Sample B"]])))
    template = save(api, first, "Template").json()
    linked = api.get(url(first)).json()
    target = SavedMapping.objects.get(pk=template["id"])
    target_id = target.pk
    applicant = SpreadsheetImport.objects.get(pk=other["id"])
    me = world.users["editor"]
    applying: Queue[int] = Queue()

    def apply() -> None:
        try:
            with transaction.atomic():
                applying.put(backend_pid())
                with pytest.raises(Invalid) as missing:
                    services.change(me, applicant, services.Choice(mapping=target_id))
                assert "mapping" in missing.value.errors
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            services.delete_mapping(me, target)
            future = pool.submit(apply)
            wait_for_blocked_transaction(applying.get(timeout=10))
        future.result(timeout=10)

    assert not SavedMapping.objects.exists()
    assert api.get(url(other)).json() == other
    now = api.get(url(first)).json()
    assert now["mapping"] is None and now["mapping_revision"] == linked["mapping_revision"] + 1
    assert save(api, linked, "Stale").status_code == 409
    assert not SavedMapping.objects.exists()
    assert AuditEvent.objects.filter(action="spreadsheet_mapping.deleted").count() == 1
    assert AuditEvent.objects.filter(action="spreadsheet_mapping.saved").count() == 1

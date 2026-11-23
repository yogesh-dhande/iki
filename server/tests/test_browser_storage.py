# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Storage (ADR 0180) in real browsers: from a freezer to a box's map and round it by
keyboard; a bulk move; a keyboard-mode scanner's code found wherever focus is; receiving a
request's tubes with one of each outcome; labels' print preview and ZPL; and reconciling a
box by scan. Correcting what someone recorded at either asks why, keeps both in the history,
and shows what changed when someone else changed it first; first observations stay one step,
and two people scanning one receipt are both recorded. Each passes axe in both themes and fits
a phone and a tablet either way up."""

import re
from itertools import pairwise
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import APIResponse, Page, Request, Route, expect
from test_browser import (  # noqa: F401 - fixtures
    PAGE_ERRORS,
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    world,
)
from test_requests import ANALYST, ASK
from test_storage import BOX, FREEZER, TUBE

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities import requests
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.org import permissions
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.storage import receipts
from iki_server.storage import services as storage
from iki_server.storage.models import (
    Event,
    ObservationEvent,
    Place,
    Receipt,
    ReceiptStatus,
    Reconciliation,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
#: A phone, a tablet held landscape and upright, and a laptop.
SIZES = [(390, 844), (1024, 768), (768, 1024), (1440, 900)]


def checked(page: Page) -> None:
    """axe, as on every page. Playwright's Firefox crashes in axe on these pages, as on
    others named by `aria-labelledby` (see assert_clean): Chromium and WebKit check them,
    and the DOM is the same in every browser."""
    assert_clean(page, axe_in_firefox=False)


def fits(page: Page) -> None:
    """At each size, nothing is wider than the screen."""
    for width, height in SIZES:
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate(NO_SIDEWAYS_SCROLL), (width, height)


def ref(saved: Any) -> dict[str, Any]:
    """A record as the tests name it: its ID and HRID."""
    record = Entity.objects.get(pk=saved.entity.pk)
    return {"id": record.pk, "hrid": record.hrid}


def make(world: World, type_name: str, values: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """A record, made as its project's Admin would: by the services, beside Playwright."""
    return ref(entities.create(world.users["admin"], type_name, world.project, values, **kw))


def put(world: World, *items: tuple[dict[str, Any], dict[str, Any], str | None]) -> None:
    found = Entity.objects.in_bulk([i for x in items for i in (x[0]["id"], x[1]["id"])])
    storage.move(
        world.users["admin"],
        [storage.Wanted(found[r["id"]], found[c["id"]], p) for r, c, p in items],
        reason="Put away",
    )


def a_freezer(world: World) -> dict[str, Any]:
    """Freezer 3, with a box of three tubes and an empty box on its shelf."""
    freezer = make(world, FREEZER, {"name": "Freezer 3"})
    retains = make(world, BOX, {"name": "Kinase retains", "rows": 9, "columns": 9})
    spare = make(world, BOX, {"name": "Spare box", "rows": 9, "columns": 9})
    tubes = [make(world, TUBE, {"barcode": f"LB-00{n}"}) for n in (11, 12, 23)]
    put(world, (retains, freezer, None), (spare, freezer, None))
    put(world, (tubes[0], retains, "A1"), (tubes[1], retains, "A2"), (tubes[2], retains, "B3"))
    return {"freezer": freezer, "retains": retains, "spare": spare, "tubes": tubes}


def where(record: dict[str, Any]) -> tuple[str, int | None, int | None]:
    place = Place.objects.select_related("container").get(pk=record["id"])
    assert place.container is not None
    return place.container.hrid, place.row, place.column


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_from_a_freezer_to_a_box_by_keyboard_and_a_bulk_move(
    open_as: Any, world: World, color_scheme: str
) -> None:
    kept = a_freezer(world)
    page = open_as("admin", "/storage/", color_scheme)
    places = page.get_by_role("table", name="Places at the top of storage")
    places.get_by_role("link", name="Freezer 3").click()
    page.get_by_role("heading", level=1, name="Freezer 3").wait_for()
    checked(page)
    page.get_by_role("table", name=re.compile(r"^What’s in FRZ-")).get_by_role(
        "link", name=kept["retains"]["hrid"]
    ).click()
    page.get_by_role("heading", level=1, name="Kinase retains").wait_for()
    # Its path leads back up.
    expect(page.get_by_role("link", name="Freezer 3").first).to_be_visible()
    grid = page.get_by_role("grid", name="Kinase retains’s positions")
    expect(grid.get_by_role("gridcell")).to_have_count(81)
    checked(page)

    # Round the map by keyboard: one tab stop, then the arrow keys.
    a1 = grid.get_by_role("gridcell", name=re.compile(r"^A1, "))
    a1.focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowRight")
    focused = grid.get_by_role("gridcell", name=re.compile(r"^B3, "))
    expect(focused).to_be_focused()
    page.keyboard.press("Enter")
    page.wait_for_url(f"**/entities/{kept['tubes'][2]['hrid']}/")
    page.go_back()

    # Select two and move them to the spare box together, from B1 on.
    page.get_by_role("button", name="Select several").click()
    grid = page.get_by_role("grid", name="Kinase retains’s positions")
    grid.get_by_role("gridcell", name=re.compile(r"^A1, ")).click()
    grid.get_by_role("gridcell", name=re.compile(r"^A2, ")).click()
    expect(grid.get_by_role("gridcell", name=re.compile(r"^A2, "))).to_have_attribute(
        "aria-selected", "true"
    )
    selected = page.get_by_role("region", name="Selected")
    selected.get_by_role("button", name="Move…").click()
    dialog = page.get_by_role("dialog", name="Move 2 records")
    to = dialog.get_by_role("textbox", name="Container")
    to.fill(kept["spare"]["hrid"])
    dialog.get_by_role("textbox", name=re.compile(r"^Starting at")).fill("B1")
    dialog.get_by_role("textbox", name="Reason").fill("Consolidating retains")
    dialog.get_by_role("button", name="Check where they go").click()
    plan = dialog.get_by_role("table", name="What moving them does")
    expect(plan.get_by_role("row")).to_have_count(3)
    expect(plan).to_contain_text(f"{kept['spare']['hrid']}, B2")
    checked(page)
    dialog.get_by_role("button", name="Move all 2").click()
    dialog.wait_for(state="hidden")
    assert where(kept["tubes"][0]) == (kept["spare"]["hrid"], 2, 1)
    assert where(kept["tubes"][1]) == (kept["spare"]["hrid"], 2, 2)
    expect(
        page.get_by_role("grid", name="Kinase retains’s positions").get_by_role(
            "gridcell", name="A1, empty"
        )
    ).to_be_visible()
    fits(page)
    page.context.close()


def scan_label(page: Page, code: str) -> None:
    # Playwright types a carriage return as the Enter key. Send the entire label in one
    # keyboard call so there is no artificial delay or round trip before its terminator.
    page.keyboard.type(code + "\r")


def test_a_scanner_finds_a_tube_wherever_focus_is(open_as: Any, world: World) -> None:
    kept = a_freezer(world)
    page = open_as("admin", f"/storage/{kept['retains']['hrid']}/")
    grid = page.get_by_role("grid", name="Kinase retains’s positions")
    grid.wait_for()
    # Focus on a button, not the scan box: a keyboard-mode scanner types fast, then Enter,
    # which mustn't press the button.
    show = page.get_by_role("button", name="Show free positions")
    show.focus()
    scan_label(page, "LB-0023")
    expect(grid.get_by_role("gridcell", name=re.compile(r"^B3, "))).to_be_focused()
    expect(page.get_by_role("status").filter(has_text="is at B3")).to_be_visible()
    expect(show).to_have_attribute("aria-pressed", "false")
    # Typing in the scan box is typing; its Enter finds what's typed.
    search = page.get_by_role("textbox", name="Scan to find")
    search.press_sequentially(kept["spare"]["hrid"])
    expect(search).to_have_value(kept["spare"]["hrid"])
    search.press("Enter")
    page.get_by_role("heading", level=1, name="Spare box").wait_for()
    # From Storage, a scanned tube opens its box with its position marked.
    page.goto("/storage/")
    page.get_by_role("heading", level=1, name="Storage").wait_for()
    page.get_by_role("link", name="Storage", exact=True).first.focus()
    scan_label(page, "LB-0011")
    page.wait_for_url(re.compile(r"/storage/BOX-\d+/\?at=A1"))
    marked = page.get_by_role("grid").get_by_role("gridcell", name=re.compile(r"^A1, "))
    expect(marked).to_have_attribute("aria-current", "location")
    checked(page)
    page.context.close()


def sent_to_a_core(world: World, n: int | tuple[str, ...]) -> dict[str, Any]:
    """A core facility serving the world's project, with an analyst to receive; an assay
    request sent to it with the given tubes; and an empty box of the core's to put them in."""
    core = Project.objects.create(team=world.team, name="Protein Core", description="Assays")
    tech = User.objects.create_user("tech@example.com", "Tech")
    world.users["tech"] = tech
    grant = ProjectMembership.objects.create(project=core, user=tech, role=ProjectRole.EDITOR)
    LabRoleGrant.objects.create(membership=grant, role=ANALYST)
    permissions.forget(tech)
    requests.set_intake(
        core, types=[ASK], from_registry=True, sources=[], by=world.users["org_admin"]
    )
    editor = world.users["editor"]
    barcodes = (f"RC{i:03}" for i in range(n)) if isinstance(n, int) else n
    tubes = [
        ref(
            entities.create(
                editor,
                TUBE,
                world.project,
                {"barcode": code},
                amount={"value": 50, "unit": "uL"},
            )
        )
        for code in barcodes
    ]
    ask = ref(
        entities.create(
            editor,
            ASK,
            world.project,
            {"title": "Intact mass", "tubes": [str(t["id"]) for t in tubes]},
        )
    )
    requests.submit(editor, Entity.objects.get(pk=ask["id"]), core, expected_revision=1)
    incoming = ref(entities.create(tech, BOX, core, {"name": "Incoming", "rows": 2, "columns": 2}))
    return {"ask": ask, "tubes": tubes, "incoming": incoming, "core": core}


def another_receiver(world: World, core: Project, principal: str, name: str) -> User:
    """A second analyst at the core's bench."""
    user = User.objects.create_user(f"{principal}@example.com", name)
    world.users[principal] = user
    grant = ProjectMembership.objects.create(project=core, user=user, role=ProjectRole.EDITOR)
    LabRoleGrant.objects.create(membership=grant, role=ANALYST)
    permissions.forget(user)
    return user


def why(page: Page, reason: str, confirm: str) -> None:
    """Says why in the dialog that asks, and confirms it."""
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("textbox", name=re.compile("^Why are you")).fill(reason)
    dialog.get_by_role("button", name=confirm).click()
    expect(dialog).to_have_count(0)


def outcome(page: Page, code: str, name: str) -> None:
    line = page.get_by_role("listitem").filter(has_text=code)
    line.get_by_role("group", name=re.compile("outcome$")).get_by_role(
        "button", name=name, exact=True
    ).click()


@pytest.mark.parametrize("prefix_gap", [250, 251, 350, 5000])
def test_receiving_does_not_match_the_suffix_of_a_stalled_scan(
    open_as: Any, world: World, prefix_gap: int
) -> None:
    sent = sent_to_a_core(world, ("XRC000", "RC000"))
    ask = sent["ask"]
    page = open_as("tech", f"/storage/receive/{ask['hrid']}/")
    page.get_by_role("button", name="Start receiving").click()
    checklist = page.get_by_role("list", name="Checklist")
    full, suffix = [
        checklist.get_by_role("listitem", name=tube["hrid"], exact=True).get_by_role(
            "button", name=re.compile(r"\bMatched$")
        )
        for tube in sent["tubes"]
    ]
    suffix.focus()
    expect(suffix).to_be_focused()
    receipt = Receipt.objects.get(request_id=ask["id"])
    receipt_path = f"/api/v1/receipts/{receipt.pk}"
    scan_path = f"{receipt_path}/scan"
    submitted: list[object] = []

    def record_scan(request: Request) -> None:
        if request.method == "POST" and request.url.endswith(scan_path):
            submitted.append(request.post_data_json)

    def read_outcomes() -> dict[str, str | None]:
        response = page.request.get(receipt_path)
        assert response.ok, response.status
        body = response.json()
        assert body["status"] == "open" and body["completed_at"] is None
        outcomes = {line["code"]: line["outcome"] for line in body["lines"]}
        assert outcomes == dict(receipt.lines.values_list("code", "outcome"))
        return outcomes

    def scan_with_prefix_gap(code: str, gap_ms: int) -> None:
        # Keep the browser's genuine key events and native button activation. Only the
        # intervals seen by the detector are fixed, so runner load cannot add a second stall.
        keys = [*code, "Enter"]
        seen = page.evaluate_handle(
            """({keys, gap}) => {
                const seen = [];
                let at = Math.ceil(performance.now());
                function stamp(event) {
                    if (event.key !== keys[seen.length]) return;
                    at += seen.length === 1 ? gap : 5;
                    Object.defineProperty(event, "timeStamp", {value: at});
                    seen.push([event.isTrusted ? event.key : null, event.timeStamp]);
                    if (event.key === "Enter") window.removeEventListener("keydown", stamp, true);
                }
                window.addEventListener("keydown", stamp, true);
                return seen;
            }""",
            {"keys": keys, "gap": gap_ms},
        )
        page.keyboard.type(code[0])
        page.wait_for_timeout(gap_ms)
        scan_label(page, code[1:])
        events = seen.json_value()
        assert [key for key, _ in events] == keys
        intervals = [b[1] - a[1] for a, b in pairwise(events)]
        assert intervals == [gap_ms] + [5] * (len(keys) - 2)
        seen.dispose()

    page.on("request", record_scan)
    scan_with_prefix_gap("XRC000", prefix_gap)
    retry = page.get_by_role("alert").filter(has_text="Scan again or type the code.")
    matched = checklist.get_by_role("button", name=re.compile(r"\bMatched$"), pressed=True)
    retry.or_(matched).first.wait_for()
    first_requests = submitted.copy()
    after_gap = read_outcomes()
    message = retry.inner_text() if retry.is_visible() else None
    if prefix_gap > 250:
        scan_with_prefix_gap("XRC000", 5)
    expect(full).to_have_attribute("aria-pressed", "true")
    expect(retry).to_be_hidden()
    final = read_outcomes()
    expected = {"XRC000": "matched", "RC000": None}
    if prefix_gap == 250:
        assert first_requests == [{"code": "XRC000"}] and after_gap == expected
        assert message is None
    else:
        assert first_requests == [] and after_gap == {"XRC000": None, "RC000": None}
        assert message == "A key arrived before the rest of the scan. Scan again or type the code."
    assert submitted == [{"code": "XRC000"}] and final == expected
    expect(suffix).to_have_attribute("aria-pressed", "false")
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_receiving_a_requests_tubes_with_one_of_each_outcome(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 4)
    ask, tubes, incoming = sent["ask"], sent["tubes"], sent["incoming"]
    page = open_as("tech", f"/storage/receive/{ask['hrid']}/", color_scheme)
    page.get_by_role("heading", level=2, name="4 items expected").wait_for()
    checked(page)
    page.get_by_role("button", name="Start receiving").click()
    checklist = page.get_by_role("list", name="Checklist")
    expect(checklist.get_by_role("listitem")).to_have_count(4)

    # A scan matches; a code nobody expected is added as extra.
    scan = page.get_by_role("textbox", name="Scan each tube")
    scan.fill("RC000")
    scan.press("Enter")
    expect(page.get_by_role("status").filter(has_text="matched")).to_be_visible()
    page.get_by_role("heading", level=2, name="What’s expected").click()
    scan_label(page, "UNLABELLED-7")
    expect(checklist.get_by_role("listitem")).to_have_count(5)
    extra = checklist.get_by_role("listitem").filter(has_text="UNLABELLED-7")
    extra.get_by_role("textbox", name=re.compile("^Note on")).fill("No label, no paperwork")
    extra.get_by_role("button", name="Save note").click()
    # The scan recorded it as extra; its first note is saved in one step, with no reason.
    expect(extra.get_by_role("button", name="Save note")).to_be_disabled()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(extra.get_by_role("textbox", name=re.compile("^Note on"))).to_have_value(
        "No label, no paperwork"
    )

    # Damaged and rejected need a note first.
    outcome(page, "RC001", "Damaged")
    damaged = checklist.get_by_role("listitem").filter(has_text="RC001")
    damaged.get_by_role("textbox", name=re.compile("^Note on")).fill("Cap cracked")
    damaged.get_by_role("button", name="Mark damaged").click()
    expect(damaged.get_by_role("button", name="Damaged")).to_have_attribute("aria-pressed", "true")
    outcome(page, "RC002", "Rejected")
    rejected = checklist.get_by_role("listitem").filter(has_text="RC002")
    rejected.get_by_role("textbox", name=re.compile("^Note on")).fill("Thawed on arrival")
    rejected.get_by_role("button", name="Mark rejected").click()
    expect(rejected.get_by_role("button", name="Rejected")).to_have_attribute(
        "aria-pressed", "true"
    )
    outcome(page, "RC003", "Missing")
    expect(
        checklist.get_by_role("listitem")
        .filter(has_text="RC003")
        .get_by_role("button", name="Missing")
    ).to_have_attribute("aria-pressed", "true")
    checked(page)

    # Who handed it over; then where the received ones go.
    page.get_by_role("textbox", name="Handed over by").fill("Courier, waybill 1Z999")
    page.get_by_role("button", name="Save", exact=True).click()
    page.get_by_role("textbox", name=re.compile(r"^Box")).fill(incoming["hrid"])
    page.get_by_role("button", name="Show its free positions").click()
    putaway = page.get_by_role("table", name=re.compile(r"^Where each goes in Incoming"))
    expect(putaway.get_by_role("row")).to_have_count(3)
    expect(putaway.get_by_role("combobox").first).to_have_value("A1")
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("button", name="Complete receiving").click()
    page.get_by_role("heading", level=2, name="Received").wait_for()
    receipt = Receipt.objects.get(request_id=ask["id"])
    assert receipt.status == ReceiptStatus.COMPLETED
    assert {x.code: x.outcome for x in receipt.lines.all()} == {
        "RC000": "matched",
        "RC001": "damaged",
        "RC002": "rejected",
        "RC003": "missing",
        "UNLABELLED-7": "extra",
    }
    assert where(tubes[0]) == (incoming["hrid"], 1, 1)
    assert where(tubes[1]) == (incoming["hrid"], 1, 2)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_labels_print_preview_and_zpl(open_as: Any, world: World, color_scheme: str) -> None:
    kept = a_freezer(world)
    page = open_as("admin", f"/entities/{kept['tubes'][2]['hrid']}/", color_scheme)
    card = page.get_by_role("region", name="Where it is")
    card.get_by_role("navigation", name=re.compile("^Where TUB-")).get_by_role(
        "link", name="Kinase retains"
    ).wait_for()
    expect(card.get_by_role("gridcell", name=re.compile(r"^B3, "))).to_have_attribute(
        "aria-current", "location"
    )
    checked(page)
    card.get_by_role("button", name="Print label").click()
    dialog = page.get_by_role("dialog", name=re.compile("^Print labels for TUB-"))
    face = dialog.get_by_role("img", name=re.compile("^Label for TUB-"))
    expect(face).to_contain_text("LB-0023")
    # Its codes are drawn: a DataMatrix and a Code 128.
    expect(face.locator("img")).to_have_count(2)
    checked(page)
    with page.expect_download() as download:
        dialog.get_by_role("button", name="Download ZPL").click()
    zpl = download.value.path().read_text()
    assert zpl.startswith("^XA") and "^FDLB-0023^FS" in zpl

    page.evaluate("window.print = () => { window.__printed = (window.__printed || 0) + 1 }")
    dialog.get_by_role("radio", name="QR code").check()
    dialog.get_by_role("button", name="Print", exact=True).click()
    page.wait_for_function("window.__printed === 1")
    page.emulate_media(media="print")
    sheet = page.locator(".label-sheet .label")
    expect(sheet).to_have_count(1)
    expect(sheet).to_be_visible()
    expect(page.get_by_role("main")).to_be_hidden()
    page.emulate_media(media="screen")
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_reconciling_a_box_by_scan(open_as: Any, world: World, color_scheme: str) -> None:
    kept = a_freezer(world)
    page = open_as("admin", f"/storage/{kept['retains']['hrid']}/", color_scheme)
    page.get_by_role("button", name="Reconcile by scanning").click()
    page.get_by_role("heading", level=1, name="Reconcile Kinase retains").wait_for()
    scan = page.get_by_role("textbox", name="What’s in A1")
    # A1 has its tube; A2 has B3's; B3 is empty; A2's own isn't anywhere.
    scan.fill("LB-0011")
    scan.press("Enter")
    page.get_by_role("textbox", name="What’s in A2").fill("LB-0023")
    page.get_by_role("textbox", name="What’s in A2").press("Enter")
    page.get_by_role("textbox", name="What’s in A3").wait_for()
    grid = page.get_by_role("grid")
    grid.get_by_role("gridcell", name=re.compile(r"^B3, ")).click()
    page.get_by_role("button", name="B3 is empty").click()
    counts = page.get_by_role("list", name="Found so far")
    expect(counts).to_contain_text("1 Matched")
    expect(counts).to_contain_text("1 Wrong position")
    expect(counts).to_contain_text("1 Missing")
    checked(page)

    to_resolve = page.get_by_role("region", name="To resolve")
    wrong = to_resolve.get_by_role("listitem").filter(has_text=kept["tubes"][2]["hrid"])
    wrong.get_by_role("combobox", name=re.compile("^What to do about")).select_option(
        label="Move it to where it was found"
    )
    wrong.get_by_role("button", name="Resolve").click()
    expect(wrong.get_by_text(re.compile("^Move it to where it was found:"))).to_be_visible()
    missing = to_resolve.get_by_role("listitem").filter(has_text=kept["tubes"][1]["hrid"])
    missing.get_by_role("combobox", name=re.compile("^What to do about")).select_option(
        label="Take it out of storage"
    )
    missing.get_by_role("textbox", name=re.compile("^Note")).fill("Used up; not logged")
    missing.get_by_role("button", name="Resolve").click()
    expect(missing.get_by_text(re.compile("^Take it out of storage:"))).to_be_visible()
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("button", name="Complete the reconciliation").click()
    page.get_by_role("heading", level=2, name="Completed").wait_for()
    checked(page)
    assert Reconciliation.objects.get().status == ReceiptStatus.COMPLETED
    # One change: B3's tube into A2, which A2's own, gone, left free.
    assert where(kept["tubes"][2]) == (kept["retains"]["hrid"], 1, 2)
    assert Place.objects.get(pk=kept["tubes"][1]["id"]).container is None
    assert Entity.objects.filter(pk=kept["tubes"][1]["id"]).exists()
    page.context.close()


def test_rescanning_a_resolved_destination_requires_a_new_decision(
    open_as: Any, world: World
) -> None:
    box = make(world, BOX, {"name": "Recount box", "rows": 1, "columns": 3})
    tube = make(world, TUBE, {"barcode": "RECOUNT42"})
    put(world, (tube, box, "A2"))
    page = open_as("admin", f"/storage/{box['hrid']}/")
    page.get_by_role("button", name="Reconcile by scanning").click()
    expect(page.get_by_role("heading", level=1, name="Reconcile Recount box")).to_be_visible()
    for position in ("A1", "A2"):
        page.get_by_role("button", name=f"{position} is empty").click()
    scan = page.get_by_role("textbox", name="What’s in A3")
    scan.fill("RECOUNT42")
    scan.press("Enter")
    finding = (
        page.get_by_role("region", name="To resolve")
        .get_by_role("listitem")
        .filter(has_text=tube["hrid"])
    )
    expect(finding).to_contain_text("Wrong position")
    choice = finding.get_by_role("combobox")
    choice.select_option(label="Move it to where it was found")
    finding.get_by_role("button", name="Resolve").click()
    complete = page.get_by_role("button", name="Complete the reconciliation")
    expect(complete).to_be_enabled()
    destinations = page.get_by_role("region", name="Complete", exact=True).get_by_role(
        "list", name="Planned destinations"
    )
    planned = destinations.get_by_role("listitem")
    expect(planned).to_have_text(f"{tube['hrid']} → {box['hrid']}, A3")
    assert where(tube) == (box["hrid"], 1, 2)

    # The previously approved move no longer describes what was found. The scan replaces
    # the tube's at A3, so it's asked why.
    page.get_by_role("button", name="A3 is empty").click()
    why(page, "Rescanned: A3 is empty", "Record the new scan")
    expect(finding).to_contain_text("Missing")
    expect(complete).to_be_disabled()
    expect(planned).to_have_count(0)
    expect(finding).to_contain_text("The scan changed. Review this finding and choose again.")
    expect(choice).to_have_value("")
    expect(finding.get_by_text(re.compile("^Move it to where it was found:"))).to_have_count(0)
    page.reload()
    expect(finding).to_contain_text("Missing")
    expect(complete).to_be_disabled()
    expect(choice).to_have_value("")

    reason = "Keep A2 until independently checked"
    choice.select_option(label="Leave the record as it is")
    finding.get_by_role("textbox", name=re.compile("^Note")).fill(reason)
    finding.get_by_role("button", name="Resolve").click()
    # It replaces the earlier, now stale, decision.
    why(page, "The move no longer applies", "Save the decision")
    complete.click()
    expect(page.get_by_role("heading", level=2, name="Completed")).to_be_visible()
    reconciliation = Reconciliation.objects.get(container_id=box["id"])
    assert reconciliation.status == ReceiptStatus.COMPLETED
    decision = reconciliation.lines.get(entity_id=tube["id"])
    assert (decision.resolution, decision.note) == ("kept", reason)
    assert where(tube) == (box["hrid"], 1, 2)
    assert not Event.objects.filter(operation=reconciliation.operation).exists()
    page.context.close()


def test_completing_the_count_requires_the_plan_reviewed_in_this_tab(
    open_as: Any, world: World
) -> None:
    box = make(world, BOX, {"name": "Review box", "rows": 1, "columns": 4})
    tube = make(world, TUBE, {"barcode": "REVIEW42"})
    unchecked = make(world, TUBE, {"barcode": "UNSCANNED42"})
    put(world, (tube, box, "A2"), (unchecked, box, "A1"))
    first = open_as("admin", f"/storage/{box['hrid']}/")
    first.get_by_role("button", name="Reconcile by scanning").click()
    expect(first.get_by_role("heading", level=1, name="Reconcile Review box")).to_be_visible()

    def review_move(tab: Page, slot: str) -> dict[str, Any]:
        tab.get_by_role("gridcell", name=re.compile(f"^{slot}, ")).click()
        scan = tab.get_by_role("textbox", name=f"What’s in {slot}")
        scan.fill("REVIEW42")
        scan.press("Enter")
        # Seen elsewhere before, the tube's scan replaces that one, and asks why.
        if slot != "A3":
            why(tab, f"Found it at {slot}", "Record the new scan")
        line = (
            tab.get_by_role("region", name="To resolve")
            .get_by_role("listitem")
            .filter(has_text=tube["hrid"])
        )
        expect(line).to_contain_text(f"Recorded at A2, found at {slot}.")
        line.get_by_role("combobox").select_option(label="Move it to where it was found")
        with tab.expect_response(
            lambda response: response.request.method == "PATCH" and "/lines/" in response.url
        ) as saved:
            line.get_by_role("button", name="Resolve").click()
            if slot != "A3":
                why(tab, "It was moved during the count", "Save the decision")
        expect(line.get_by_text(re.compile("^Move it to where it was found:"))).to_be_visible()
        snapshot = saved.value.json()
        assert isinstance(snapshot, dict)
        return snapshot

    original = review_move(first, "A3")
    approval = first.get_by_role("region", name="Complete", exact=True)
    note = approval.get_by_role("textbox", name=re.compile("^Note"))
    acknowledge = approval.get_by_role("checkbox", name=re.compile("^Count the 1 not scanned"))
    complete = approval.get_by_role("button", name="Complete the reconciliation")
    original_note = "Completed count after reviewing A3"
    acknowledge.check()
    note.fill(original_note)
    expect(complete).to_be_enabled()

    second = open_as("editor", first.url)
    latest = review_move(second, "A4")

    with first.expect_response("**/reconciliations/*/complete") as refused:
        complete.click()
    assert refused.value.status == 409, (refused.value.status, refused.value.json())
    assert refused.value.request.post_data_json == {
        "plan_revision": original["plan_revision"],
        "missing_rest": True,
        "note": original_note,
    }
    reconciliation = Reconciliation.objects.get(container_id=box["id"])
    assert reconciliation.status == ReceiptStatus.OPEN
    assert reconciliation.note == ""
    assert where(tube) == (box["hrid"], 1, 2)
    assert where(unchecked) == (box["hrid"], 1, 1)
    audit = AuditEvent.objects.filter(action="storage.reconciled", target_id=box["id"])
    events = Event.objects.filter(operation=reconciliation.operation)
    assert not audit.exists() and not events.exists()
    assert reconciliation.lines.get(entity_id=tube["id"]).resolved_by_id == world.users["editor"].pk

    expect(complete).to_be_hidden()
    alert = approval.get_by_role("alert")
    expect(alert).to_contain_text("The count changed while you were")
    expect(
        alert.get_by_role("listitem").filter(has_text="Decision changed by Editor")
    ).to_be_visible()
    expect(approval).to_contain_text("Review the latest count")
    destinations = approval.get_by_role("list", name="Planned destinations")
    expect(destinations).to_be_hidden()
    review = approval.get_by_role("button", name="Review latest count")
    expect(review).to_be_visible()
    review.click()
    expect(destinations.get_by_role("listitem")).to_have_text(f"{tube['hrid']} → {box['hrid']}, A4")
    expect(note).to_have_value("")
    expect(acknowledge).not_to_be_checked()
    expect(complete).to_be_disabled()
    updated_note = "Completed count after reviewing A4"
    note.fill(updated_note)
    acknowledge.check()
    with first.expect_response("**/reconciliations/*/complete") as accepted:
        complete.click()
    assert accepted.value.status == 200
    assert accepted.value.request.post_data_json == {
        "plan_revision": latest["plan_revision"],
        "missing_rest": True,
        "note": updated_note,
    }
    assert latest["plan_revision"] > original["plan_revision"]
    expect(first.get_by_role("heading", level=2, name="Completed")).to_be_visible()
    reconciliation.refresh_from_db()
    assert reconciliation.note == updated_note
    assert reconciliation.completed_by_id == world.users["admin"].pk
    assert where(tube) == (box["hrid"], 1, 4)
    assert Place.objects.get(pk=unchecked["id"]).container is None
    assert set(events.values_list("entity_id", "kind")) == {
        (tube["id"], "moved"),
        (unchecked["id"], "removed"),
    }
    assert audit.get().data["plan_revision"] == latest["plan_revision"]
    second.context.close()
    first.context.close()


# --- Correcting what was recorded ------------------------------------------------------------


def start_receiving(open_as: Any, sent: dict[str, Any], who: str = "tech", **kw: Any) -> Page:
    page: Page = open_as(who, f"/storage/receive/{sent['ask']['hrid']}/", **kw)
    start = page.get_by_role("button", name="Start receiving")
    checklist = page.get_by_role("list", name="Checklist")
    start.or_(checklist).first.wait_for()
    if start.is_visible():
        start.click()
    expect(checklist.get_by_role("listitem")).to_have_count(len(sent["tubes"]))
    return page


def scan_in(page: Page, code: str) -> None:
    scan = page.get_by_role("textbox", name="Scan each tube")
    scan.fill(code)
    scan.press("Enter")


def heard(page: Page) -> Any:
    """What the receiving page says about the last scan."""
    return page.get_by_role("region", name="What’s expected").get_by_role("status")


def receipt_line(page: Page, tube: dict[str, Any]) -> Any:
    return page.get_by_role("list", name="Checklist").get_by_role(
        "listitem", name=tube["hrid"], exact=True
    )


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_correcting_a_receipt_line_asks_why_and_shows_it_in_its_history(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 2)
    tube = sent["tubes"][0]
    page = start_receiving(open_as, sent, color_scheme=color_scheme)

    # A scan is one step: matched, and nothing asked.
    scan_in(page, "RC000")
    line = receipt_line(page, tube)
    matched = line.get_by_role("button", name="Matched")
    expect(matched).to_have_attribute("aria-pressed", "true")
    expect(heard(page)).to_have_text(f"{tube['hrid']}: matched.")
    expect(page.get_by_role("dialog")).to_have_count(0)

    # Changing what was recorded asks why, separately from the condition note.
    line.get_by_role("button", name="Damaged").click()
    dialog = page.get_by_role("dialog", name=f"Change {tube['hrid']} to damaged?")
    expect(dialog).to_be_visible()
    dialog.get_by_role("textbox", name=re.compile("^Condition note")).fill("Cap cracked")
    save = dialog.get_by_role("button", name="Save the change")
    save.click()
    expect(dialog.get_by_role("alert")).to_have_text("Say why you’re making this change.")
    reason = dialog.get_by_role("textbox", name="Why are you changing this?")
    expect(reason).to_be_focused()
    receipt = Receipt.objects.get(request_id=sent["ask"]["id"])
    assert receipt.lines.get(code="RC000").outcome == "matched"
    checked(page)
    reason.fill("Looked again under the lamp")
    save.click()
    expect(dialog).to_have_count(0)
    expect(line.get_by_role("button", name="Damaged")).to_have_attribute("aria-pressed", "true")
    saved = receipt.lines.get(code="RC000")
    assert (saved.outcome, saved.note) == ("damaged", "Cap cracked")

    line.get_by_role("button", name=f"History of {tube['hrid']}").click()
    history = page.get_by_role("list", name=f"History of {tube['hrid']}, newest first")
    expect(history.get_by_role("listitem")).to_have_count(2)
    corrected, recorded = history.get_by_role("listitem").all()
    expect(corrected).to_contain_text("Outcome changed")
    expect(corrected).to_contain_text("Matched")
    expect(corrected).to_contain_text("Damaged · note “Cap cracked”")
    expect(corrected).to_contain_text("· Tech")
    expect(corrected).to_contain_text("“Looked again under the lamp”")
    expect(recorded).to_contain_text("Outcome recorded")
    events = ObservationEvent.objects.filter(receipt=receipt, line_id=saved.pk).order_by("revision")
    first, second = events
    assert (first.actor_id, first.reason) == (world.users["tech"].pk, "")
    assert first.after is not None and first.after["outcome"] == "matched"
    assert (second.actor_id, second.reason) == (
        world.users["tech"].pk,
        "Looked again under the lamp",
    )
    assert second.previous_id == first.pk
    checked(page)
    fits(page)
    page.context.close()


def test_two_people_scanning_different_tubes_on_one_receipt_are_both_recorded(
    open_as: Any, world: World
) -> None:
    sent = sent_to_a_core(world, 2)
    another_receiver(world, sent["core"], "tech2", "Tech Two")
    first = start_receiving(open_as, sent)
    second = start_receiving(open_as, sent, "tech2")
    held: list[tuple[Route, APIResponse]] = []

    def answer_later(route: Route) -> None:
        # The server records the first scan now; its answer reaches the page later.
        held.append((route, route.fetch()))

    first.route("**/api/v1/receipts/*/scan", answer_later)
    scan_in(first, "RC000")
    for _ in range(100):
        if held:
            break
        first.wait_for_timeout(50)
    assert held, "The first scan never reached the server."
    scan_in(second, "RC001")
    expect(heard(second)).to_have_text(f"{sent['tubes'][1]['hrid']}: matched.")
    route, response = held[0]
    route.fulfill(response=response)
    first.unroute("**/api/v1/receipts/*/scan")
    expect(heard(first)).to_have_text(f"{sent['tubes'][0]['hrid']}: matched.")

    receipt = Receipt.objects.get(request_id=sent["ask"]["id"])
    lines = {x.code: (x.outcome, x.checked_by_id) for x in receipt.lines.all()}
    assert lines == {
        "RC000": ("matched", world.users["tech"].pk),
        "RC001": ("matched", world.users["tech2"].pk),
    }
    # Each page shows both: the later answer never takes the other's back.
    for page in (first, second):
        page.reload()
        for tube in sent["tubes"]:
            expect(
                receipt_line(page, tube).get_by_role("button", name="Matched")
            ).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("dialog")).to_have_count(0)
    second.context.close()
    first.context.close()


def test_a_correction_refused_as_stale_shows_who_changed_it_and_keeps_the_reason(
    open_as: Any, world: World
) -> None:
    sent = sent_to_a_core(world, 1)
    colleague = another_receiver(world, sent["core"], "tech2", "Tech Two")
    tube = sent["tubes"][0]
    page = start_receiving(open_as, sent)
    scan_in(page, "RC000")
    line = receipt_line(page, tube)
    expect(line.get_by_role("button", name="Matched")).to_have_attribute("aria-pressed", "true")
    line.get_by_role("button", name="Missing").click()
    dialog = page.get_by_role("dialog")
    reason = dialog.get_by_role("textbox", name="Why are you changing this?")
    reason.fill("Not in the box after all")

    # Meanwhile, a colleague records it as damaged.
    receipt = Receipt.objects.get(request_id=sent["ask"]["id"])
    working = receipt.lines.get(code="RC000")
    receipts.check(
        colleague,
        receipt,
        working.pk,
        "damaged",
        "Cap cracked",
        line_revision=working.line_revision,
        reason="The cap is cracked",
    )
    with page.expect_response("**/lines/*") as refused:
        dialog.get_by_role("button", name="Save the change").click()
    assert refused.value.status == 409
    alert = dialog.get_by_role("alert")
    expect(alert).to_contain_text(f"{tube['hrid']} changed while you were looking at it")
    expect(alert).to_contain_text(
        f"{tube['hrid']}: Now damaged · note “Cap cracked”, checked by Tech Two"
    )
    expect(dialog).to_contain_text("Damaged")
    expect(reason).to_have_value("Not in the box after all")
    working.refresh_from_db()
    assert (working.outcome, working.note) == ("damaged", "Cap cracked")
    # Chromium and WebKit log the refusal as a failed load: that, and nothing else.
    PAGE_ERRORS[page][:] = [e for e in PAGE_ERRORS[page] if "status of 409 (Conflict)" not in e]
    checked(page)

    # Sent again only when the person, having seen it, asks.
    with page.expect_response("**/lines/*") as accepted:
        dialog.get_by_role("button", name="Save the change").click()
    assert accepted.value.status == 200
    expect(dialog).to_have_count(0)
    working.refresh_from_db()
    assert working.outcome == "missing"
    latest = ObservationEvent.objects.filter(line_id=working.pk).order_by("-revision").first()
    assert latest is not None
    assert (latest.actor_id, latest.reason) == (
        world.users["tech"].pk,
        "Not in the box after all",
    )
    page.context.close()


def test_a_count_scan_replacing_an_empty_position_asks_why_and_shows_it_in_the_history(
    open_as: Any, world: World
) -> None:
    box = make(world, BOX, {"name": "Count box", "rows": 1, "columns": 3})
    tube = make(world, TUBE, {"barcode": "COUNT42"})
    put(world, (tube, box, "A1"))
    page = open_as("admin", f"/storage/{box['hrid']}/")
    page.get_by_role("button", name="Reconcile by scanning").click()
    expect(page.get_by_role("heading", level=1, name="Reconcile Count box")).to_be_visible()

    # Fresh observations are one step each.
    scan = page.get_by_role("textbox", name="What’s in A1")
    scan.fill("COUNT42")
    scan.press("Enter")
    page.get_by_role("button", name="A2 is empty").click()
    expect(page.get_by_role("textbox", name="What’s in A3")).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(0)

    # Seeing a tube where an empty position was recorded replaces that, and asks why.
    page.get_by_role("gridcell", name=re.compile(r"^A2, ")).click()
    expect(page.get_by_text(re.compile(r"^Recorded at A2: Empty, scanned by"))).to_be_visible()
    scan = page.get_by_role("textbox", name="What’s in A2")
    scan.fill("STRAY-9")
    scan.press("Enter")
    dialog = page.get_by_role("dialog", name="Replace what’s recorded at A2?")
    expect(dialog.get_by_role("listitem")).to_contain_text("A2: Empty")
    expect(dialog.get_by_role("listitem")).to_contain_text("STRAY-9")
    dialog.get_by_role("button", name="Record the new scan").click()
    expect(dialog.get_by_role("alert")).to_have_text("Say why you’re making this change.")
    checked(page)
    why(page, "A tube was hidden behind the rack", "Record the new scan")
    counts = page.get_by_role("list", name="Found so far")
    expect(counts).to_contain_text("1 Unexpected")

    page.get_by_text("This count’s history").click()
    history = page.get_by_role("list", name="History of this count, newest first")
    # The new scan and the replaced empty one, each with who and why.
    replaced = history.get_by_role("listitem").filter(has_text="Scan replaced")
    expect(replaced).to_contain_text("Empty at A2")
    expect(replaced).to_contain_text("· Admin")
    expect(replaced).to_contain_text("“A tube was hidden behind the rack”")
    seen = history.get_by_role("listitem").filter(has_text="Scan recorded · STRAY-9")
    expect(seen).to_contain_text("STRAY-9 at A2")
    expect(seen).to_contain_text("“A tube was hidden behind the rack”")
    reconciliation = Reconciliation.objects.get(container_id=box["id"])
    events = ObservationEvent.objects.filter(
        reconciliation=reconciliation, reason="A tube was hidden behind the rack"
    )
    assert {(x.actor_id, x.kind) for x in events} == {(world.users["admin"].pk, "scan")}
    checked(page)
    fits(page)
    page.context.close()

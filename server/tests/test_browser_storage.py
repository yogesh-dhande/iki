# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Storage (ADR 0180) in real browsers: from a freezer to a box's map and round it by
keyboard; a bulk move; a keyboard-mode scanner's code found wherever focus is; receiving a
request's tubes with one of each outcome; labels' print preview and ZPL; and reconciling a
box by scan. Each passes axe in both themes and fits a phone and a tablet either way up."""

import re
from typing import Any

import pytest
from browser_scan import observe_scan
from org_world import World
from playwright.sync_api import Page, expect
from test_browser import (  # noqa: F401 - fixtures
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
from iki_server.storage import services as storage
from iki_server.storage.models import Event, Place, Receipt, ReceiptStatus, Reconciliation

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
    box_path = f"/storage/{kept['retains']['hrid']}/"
    page = open_as("admin", box_path)
    with observe_scan(page, "LB-0023", box_path, f"B3 focused on {box_path}"):
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
    with observe_scan(page, "LB-0011", "/storage/", f"{box_path}?at=A1", new_document=True):
        page.goto("/storage/")
        page.get_by_role("heading", level=1, name="Storage").wait_for()
        page.get_by_role("link", name="Storage", exact=True).first.focus()
        scan_label(page, "LB-0011")
        page.wait_for_url(re.compile(r"/storage/BOX-\d+/\?at=A1"))
        marked = page.get_by_role("grid").get_by_role("gridcell", name=re.compile(r"^A1, "))
        expect(marked).to_have_attribute("aria-current", "location")
    checked(page)
    page.context.close()


def test_a_stalled_scan_can_be_retried_without_activating_the_button(
    open_as: Any, world: World
) -> None:
    kept = a_freezer(world)
    box_path = f"/storage/{kept['retains']['hrid']}/"
    page = open_as("admin", box_path)
    show = page.get_by_role("button", name="Show free positions")
    code = "LB-0023"
    retry = page.get_by_role("alert").filter(has_text="Couldn’t read the scan")
    with observe_scan(
        page,
        code,
        box_path,
        "retry with focus still on the button",
        gaps_ms=(5, 51, 5, 51, 5, 5, 5),
    ):
        show.focus()
        scan_label(page, code)
        expect(retry).to_be_visible()
        expect(show).to_be_focused()
        expect(show).to_have_attribute("aria-pressed", "false")
    with observe_scan(
        page, code, box_path, f"B3 focused on {box_path}", gaps_ms=(5, 250, 5, 5, 5, 5, 5)
    ):
        scan_label(page, code)
        expect(page.get_by_role("gridcell", name=re.compile(r"^B3, "))).to_be_focused()
        expect(retry).to_be_hidden()
        expect(show).to_have_attribute("aria-pressed", "false")
    page.context.close()


def sent_to_a_core(world: World, n: int) -> dict[str, Any]:
    """A core facility serving the world's project, with an analyst to receive; an assay
    request sent to it with `n` tubes; and an empty box of the core's to put them in."""
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
    tubes = [
        ref(
            entities.create(
                editor,
                TUBE,
                world.project,
                {"barcode": f"RC{i:03}"},
                amount={"value": 50, "unit": "uL"},
            )
        )
        for i in range(n)
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
    return {"ask": ask, "tubes": tubes, "incoming": incoming}


def outcome(page: Page, code: str, name: str) -> None:
    line = page.get_by_role("listitem").filter(has_text=code)
    line.get_by_role("group", name=re.compile("outcome$")).get_by_role(
        "button", name=name, exact=True
    ).click()


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

    # The previously approved move no longer describes what was found.
    page.get_by_role("button", name="A3 is empty").click()
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
    expect(approval.get_by_role("alert")).to_contain_text("Review the latest count")
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

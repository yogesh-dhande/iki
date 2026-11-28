# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Storage (ADR 0180) in real browsers: from a freezer to a box's map and round it by
keyboard; a bulk move, to the wells it showed or none; a keyboard-mode scanner's code found
wherever focus is; receiving a request's tubes with one of each outcome; labels' print
preview and ZPL; and reconciling a box by scan. Correcting what someone recorded at either
asks why, keeps both in the history, and shows what changed when someone else changed it
first; first observations stay one step, and two people scanning one receipt are both
recorded. A label two readable tubes share asks which, with each one's project and position;
one a concealed tube shares is refused in the same words whatever's behind it; a typed Iki
ID, a draw and a move confirm a tube by its own identity; the scanner's exact text is kept
in the receipt's history; and a label's vendor barcode is an opt-in extra the server checks.
A count's unregistered label seen in another slot asks whether it's another tube or that one
moved, and a move names its line and asks why. Completing a receipt waits for the box its
tubes go in, puts each where it was shown, and keeps one in custody only when that's chosen
for it, which Storage then lists as awaiting put-away. Each passes axe in both themes and
fits a phone and a tablet either way up."""

import json
import re
import time
from itertools import pairwise
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from org_world import World
from playwright.sync_api import APIResponse, Page, Request, Route, ViewportSize, expect
from test_browser import (  # noqa: F401 - fixtures
    NEXT_FRAMES,
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
from test_count_observation_history import review
from test_count_unregistered_slots import by_slot, reading
from test_requests import ANALYST, ASK
from test_storage import BOX, FREEZER, TUBE

from iki_server.accounts.models import User
from iki_server.amounts.models import Movement
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
from iki_server.storage.scan_identity import ScanRefused

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
#: A phone, a tablet held landscape and upright, and a laptop.
SIZES = [(390, 844), (1024, 768), (768, 1024), (1440, 900)]


def checked(page: Page) -> None:
    """axe and the page's errors, as on every page."""
    assert_clean(page)


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


def put(
    world: World,
    *items: tuple[dict[str, Any], dict[str, Any], str | None],
    scanned: tuple[dict[str, Any], ...] = (),
) -> None:
    """Puts each away as its project's Admin; those `scanned` with their own Iki label read,
    for a type that asks for a scan before a move."""
    found = Entity.objects.in_bulk([i for x in items for i in (x[0]["id"], x[1]["id"])])
    storage.move(
        world.users["admin"],
        [storage.Wanted(found[r["id"]], found[c["id"]], p) for r, c, p in items],
        reason="Put away",
        confirmations={found[r["id"]].pk: iki_label(r) for r in scanned},
    )


def iki_label(record: dict[str, Any]) -> str:
    """What the record's own Iki label reads: IKI: and its UUID's 32 hex digits."""
    return "IKI:" + UUID(str(record["id"])).hex.upper()


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


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_move_puts_tubes_in_the_wells_it_showed_or_moves_none(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """Moving commits the plan shown (ADR 0207). Here, between checking where two tubes go
    and moving them, another bench puts a tube in the first well shown: nothing moves, the
    dialog says where each would go now and shows it, and moving them again puts them
    there. On a phone, in both schemes."""
    kept = a_freezer(world)
    tubes, retains, spare = kept["tubes"], kept["retains"]["hrid"], kept["spare"]["hrid"]
    page = open_as("admin", f"/storage/{retains}/", color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Select several").click()
    grid = page.get_by_role("grid", name="Kinase retains’s positions")
    grid.get_by_role("gridcell", name=re.compile(r"^A1, ")).click()
    grid.get_by_role("gridcell", name=re.compile(r"^A2, ")).click()
    page.get_by_role("region", name="Selected").get_by_role("button", name="Move…").click()
    dialog = page.get_by_role("dialog", name="Move 2 records")
    dialog.get_by_role("textbox", name="Container").fill(spare)
    dialog.get_by_role("textbox", name="Reason").fill("Consolidating retains")
    dialog.get_by_role("button", name="Check where they go").click()
    plan = dialog.get_by_role("table", name="What moving them does")
    expect(plan.get_by_role("row").nth(1)).to_contain_text(f"{spare}, A1")
    expect(plan.get_by_role("row").nth(2)).to_contain_text(f"{spare}, A2")

    put(world, (tubes[2], kept["spare"], "A1"))  # at another bench
    dialog.get_by_role("button", name="Move all 2").click()
    note = dialog.get_by_role("alert").filter(has_text="Where they go changed")
    expect(note).to_contain_text(f"{tubes[0]['hrid']}: goes to {spare}, A2, not {spare}, A1.")
    expect(note).to_contain_text(f"{tubes[1]['hrid']}: goes to {spare}, A3, not {spare}, A2.")
    expect(plan.get_by_role("row").nth(1)).to_contain_text(f"{spare}, A2")
    expect(plan.get_by_role("row").nth(2)).to_contain_text(f"{spare}, A3")
    assert where(tubes[0]) == (retains, 1, 1) and where(tubes[1]) == (retains, 1, 2)
    expected_refusals(page, "409 (Conflict)")
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    checked(page)

    dialog.get_by_role("button", name="Move all 2").click()
    dialog.wait_for(state="hidden")
    assert where(tubes[0]) == (spare, 1, 2) and where(tubes[1]) == (spare, 1, 3)
    assert where(tubes[2]) == (spare, 1, 1)
    page.context.close()


def scan_label(page: Page, code: str, *, prefix_gap_ms: int = 5) -> None:
    """Send a trusted keyboard burst, fixing only the intervals seen by the scanner."""
    gaps = [prefix_gap_ms if i == 0 else 5 for i in range(len(code))]
    prefix = "Storage scanner keydowns: "
    # Window capture runs before the app's document listener. Keep the native target,
    # modifiers and button activation. Report the sequence before Enter can navigate.
    page.evaluate(
        """({gaps, prefix}) => {
            const events = [];
            let at = Math.ceil(performance.now());
            function stamp(event) {
                if (event.key === "Shift") return;
                if (events.length) at += gaps[events.length - 1] ?? 5;
                const raw = event.timeStamp;
                Object.defineProperty(event, "timeStamp", {value: at});
                events.push({
                    key: event.key, trusted: event.isTrusted, raw, at: event.timeStamp,
                    target: event.target instanceof Element ? event.target.localName : null,
                });
                if (event.key === "Enter") {
                    window.removeEventListener("keydown", stamp, true);
                    console.debug(prefix + JSON.stringify(events));
                }
            }
            window.addEventListener("keydown", stamp, true);
        }""",
        {"gaps": gaps, "prefix": prefix},
    )
    with page.expect_event(
        "console", predicate=lambda message: message.text.startswith(prefix)
    ) as log:
        if prefix_gap_ms == 5:
            page.keyboard.type(code + "\r")
        else:
            page.keyboard.type(code[0])
            page.wait_for_timeout(prefix_gap_ms)
            page.keyboard.type(code[1:] + "\r")
    events = json.loads(log.value.text.removeprefix(prefix))
    assert [event["key"] for event in events] == [*code, "Enter"], events
    assert all(event["trusted"] for event in events), events
    assert [b["at"] - a["at"] for a, b in pairwise(events)] == gaps, events


def test_a_scanner_finds_a_tube_wherever_focus_is(open_as: Any, world: World) -> None:
    kept = a_freezer(world)
    page: Page = open_as("admin", f"/storage/{kept['retains']['hrid']}/")
    started = monotonic()

    def log(message: str) -> None:
        print(f"Storage scan +{monotonic() - started:.3f}s {message}")  # noqa: T201

    def log_request(request: Request, status: str) -> None:
        path = urlsplit(request.url).path
        if path in {"/api/v1/storage/scan", "/api/v1/storage/scan/resolve"}:
            log(f"{request.method} {path} {status}")

    page.on("request", lambda req: log_request(req, "sent"))
    page.on("response", lambda response: log_request(response.request, str(response.status)))
    page.on("requestfailed", lambda req: log_request(req, f"failed: {req.failure}"))
    page.on(
        "framenavigated",
        lambda frame: log(f"navigated {frame.url}") if frame == page.main_frame else None,
    )
    log(f"opened {page.url}")
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

    page.on("request", record_scan)
    scan_label(page, "XRC000", prefix_gap_ms=prefix_gap)
    retry = page.get_by_role("alert").filter(has_text="Scan again or type the code.")
    matched = checklist.get_by_role("button", name=re.compile(r"\bMatched$"), pressed=True)
    retry.or_(matched).first.wait_for()
    first_requests = submitted.copy()
    after_gap = read_outcomes()
    message = retry.inner_text() if retry.is_visible() else None
    if prefix_gap > 250:
        scan_label(page, "XRC000")
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
    putaway = page.get_by_role("table", name=re.compile(rf"^{incoming['hrid']} Incoming · "))
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
    tube = kept["tubes"][2]
    page = open_as("admin", f"/entities/{tube['hrid']}/", color_scheme)
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
    face = dialog.get_by_role("img", name=f"Label for {tube['hrid']}")
    # By default, its unique Iki code alone, beside its HRID: not the vendor barcode.
    expect(face).to_contain_text(tube["hrid"])
    expect(face.locator("img")).to_have_count(1)
    expect(face.locator(".label-bar")).to_have_count(0)
    checked(page)
    unique = "IKI:" + str(tube["id"]).replace("-", "").upper()
    with page.expect_download() as download:
        dialog.get_by_role("button", name="Download ZPL").click()
    zpl = download.value.path().read_text()
    assert zpl.startswith("^XA") and f"^FD{unique}^FS" in zpl and "^BXN" in zpl
    # No Code 128: its barcode shows only as its title's text.
    assert "^BCN" not in zpl

    # The vendor barcode, asked for, is a separate Code 128, named as the vendor's.
    dialog.get_by_role("checkbox", name="Also print the vendor barcode (Code 128)").check()
    face = dialog.get_by_role("img", name=f"Label for {tube['hrid']}, with vendor barcode LB-0023")
    expect(face.locator("img")).to_have_count(2)
    expect(face).to_contain_text("Vendor LB-0023")
    with page.expect_download() as download:
        dialog.get_by_role("button", name="Download ZPL").click()
    zpl = download.value.path().read_text()
    assert f"^FD{unique}^FS" in zpl and "^BCN" in zpl

    # A cryo tube's label has no room for it: the Iki code and the whole HRID.
    dialog.get_by_role("combobox", name="Size").select_option("cryo")
    vendor = dialog.get_by_role("checkbox", name="Also print the vendor barcode (Code 128)")
    expect(vendor).to_be_disabled()
    expect(vendor).not_to_be_checked()
    face = dialog.get_by_role("img", name=f"Label for {tube['hrid']}")
    expect(face).to_have_class(re.compile("label-cryo"))
    expect(face.locator("img")).to_have_count(1)
    expect(face.locator(".label-hrid")).to_have_text(tube["hrid"])
    checked(page)

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


#: Where receiving is done: a laptop at the bench, and a phone held at it.
BENCH: dict[str, ViewportSize] = {
    "laptop": {"width": 1440, "height": 900},
    "phone": {"width": 390, "height": 844},
}
#: The receipt's lines being read again: what each change asks for once it's had its answer.
READ_AGAIN = re.compile(r"/api/v1/entities/[^/]+/receipts$")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("screen", ["laptop", "phone"])
@pytest.mark.parametrize("press", ["across", "after"])
@pytest.mark.parametrize("change", ["note", "save", "scan"])
def test_an_outcome_stays_under_the_press_while_the_line_above_changes(
    open_as: Any, world: World, change: str, press: str, screen: str, color_scheme: str
) -> None:
    """The second line's Missing stays where it was first drawn while the line above it
    changes: its note opens (Damaged pressed, with no note yet), its save's answer arrives
    (held back till then), or a scanner's scan of it is answered. "across" presses Missing
    before the change and lets go after it; "after" taps, once it has, where Missing was
    first drawn. Either records missing, as the server has it, and nothing else. It didn't:
    the line above grew as its note editor opened or its save came back (its note editor,
    its Clear, who checked it), so Missing moved 22-79 px down under the press, which ended
    on something else and recorded nothing, and a tap where it had been landed above it."""
    sent = sent_to_a_core(world, 3)
    above, below = sent["tubes"][:2]
    page = start_receiving(open_as, sent, color_scheme=color_scheme)
    # Drawn at the bench's size from the start, so nothing's still settling from a resize.
    page.set_viewport_size(BENCH[screen])
    page.reload()
    # And with the request's title under the heading, which arrives on its own.
    page.get_by_text("Intact mass", exact=True).filter(visible=True).first.wait_for()
    receipt = Receipt.objects.get(request_id=sent["ask"]["id"])
    first = receipt_line(page, above)
    # Named with its tick once it's pressed.
    missing = receipt_line(page, below).get_by_role("button", name=re.compile(r"\bMissing$"))
    missing.wait_for()

    def by_keyboard(name: str) -> None:
        # Pressed where it is, without scrolling the page or moving the pointer.
        first.get_by_role("button", name=name, exact=True).evaluate(
            "button => button.focus({preventScroll: true})"
        )
        page.keyboard.press("Space")

    held: list[Route] = []
    lines = f"**/api/v1/receipts/{receipt.pk}/lines/*"
    if change == "save":
        page.route(lines, lambda route: held.append(route))
        by_keyboard("Matched")
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert held, "The save never reached the server."

    def arrive() -> None:
        """The change to the line above, once the page has drawn it."""
        if change == "note":
            by_keyboard("Damaged")
        else:
            with page.expect_request(
                lambda r: r.method == "GET" and bool(READ_AGAIN.search(r.url))
            ):
                if change == "save":
                    held[0].continue_()
                    page.unroute(lines)
                else:
                    scan_label(page, "RC000")
        page.evaluate(NEXT_FRAMES)

    missing.scroll_into_view_if_needed()
    page.evaluate(NEXT_FRAMES)
    drawn = missing.bounding_box()
    assert drawn
    x, y = drawn["x"] + drawn["width"] / 2, drawn["y"] + drawn["height"] / 2
    if press == "across":
        page.mouse.move(x, y)
        page.mouse.down()
        arrive()
        pressed = missing.bounding_box()
        page.mouse.up()
    else:
        arrive()
        pressed = missing.bounding_box()
        page.mouse.click(x, y)

    def recorded() -> dict[str, str | None]:
        response = page.request.get(f"/api/v1/receipts/{receipt.pk}")
        assert response.ok, response.status
        return {line["code"]: line["outcome"] for line in response.json()["lines"]}

    wanted = {"RC000": None if change == "note" else "matched", "RC001": "missing", "RC002": None}
    deadline = time.monotonic() + 5
    while (now := recorded()) != wanted and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    page.evaluate(NEXT_FRAMES)
    settled = missing.bounding_box()
    moved = f"Missing drawn at {drawn}, then at {pressed} when let go, then at {settled}."
    assert now == wanted, moved
    expect(missing).to_have_attribute("aria-pressed", "true")
    page.evaluate(NEXT_FRAMES)
    # Where it was first drawn, when let go and once its own outcome is shown.
    assert pressed == drawn, moved
    assert missing.bounding_box() == drawn, moved
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    checked(page)
    page.context.close()


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


# --- Which tube a scan identifies -------------------------------------------------------------

#: A vendor barcode two projects' tubes carry: unique only per project and type.
SHARED = "SHARED-42"
#: What Iki says when a scan involves a tube the person can't read.
REFUSED = str(ScanRefused())
# A tube whose type asks for its own scan before a draw and before a move.
RETAIN_TUBE = "acme/protein.RetainTube"
# A typed Iki ID refused: none has it, or a tube the person can't read does. The same words.
IKI_ID_REFUSED = (
    "No tube you can open has this Iki ID. Check the ID, or ask an authorized reviewer."
)


def expected_refusals(page: Page, status: str) -> None:
    """Chromium and WebKit log a refusal the test expects as a failed load: that, and
    nothing else, is left out of the errors `checked` looks for."""
    PAGE_ERRORS[page][:] = [e for e in PAGE_ERRORS[page] if f"status of {status}" not in e]


def twins(
    world: World, *, concealed: bool = False, kind: str = TUBE, apart: bool = False
) -> dict[str, Any]:
    """Two tubes with one vendor barcode side by side in the Kinase Atlas's box: its own at
    A1, a `kind` with 50 µL, and a supplier's at A2, shared into the Kinase Atlas, where the
    admin also reads the supplier's project; or, concealed, taken out of the Kinase Atlas
    again once it's in the box, so the admin can't read it or its project. `apart` leaves
    the supplier's out of storage, for a test to put beside it later."""
    supplier = Project.objects.create(team=world.team, name="Supplier")
    member = world.users["team_member"]
    ProjectMembership.objects.create(project=supplier, user=member, role=ProjectRole.ADMIN)
    if not concealed:
        ProjectMembership.objects.create(
            project=supplier, user=world.users["admin"], role=ProjectRole.VIEWER
        )
        permissions.forget(world.users["admin"])
    ProjectMembership.objects.create(project=world.project, user=member, role=ProjectRole.EDITOR)
    permissions.forget(member)
    retains = make(world, BOX, {"name": "Kinase retains", "rows": 9, "columns": 9})
    spare = make(world, BOX, {"name": "Spare box", "rows": 9, "columns": 9})
    ours = make(world, kind, {"barcode": SHARED}, amount={"value": 50, "unit": "uL"})
    theirs = ref(entities.create(member, TUBE, supplier, {"barcode": SHARED}))
    theirs_entity = Entity.objects.get(pk=theirs["id"])
    entities.link(member, theirs_entity, world.project)
    put(world, (ours, retains, "A1"), scanned=(ours,))
    if not apart:
        put(world, (theirs, retains, "A2"))
    if concealed:
        entities.unlink(member, theirs_entity, world.project)
    return {"retains": retains, "spare": spare, "ours": ours, "theirs": theirs}


def open_box(open_as: Any, box: dict[str, Any], color_scheme: str = "light") -> Page:
    page: Page = open_as("admin", f"/storage/{box['hrid']}/", color_scheme)
    page.get_by_role("grid", name="Kinase retains’s positions").wait_for()
    # Focus on a button, not the scan box: a scanner's keys still reach the scan.
    page.get_by_role("button", name="Show free positions").focus()
    return page


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_label_two_readable_tubes_share_asks_which_with_their_projects_and_positions(
    open_as: Any, world: World, color_scheme: str
) -> None:
    kept = twins(world)
    ours, theirs = kept["ours"], kept["theirs"]
    page = open_box(open_as, kept["retains"], color_scheme)
    scan_label(page, SHARED)
    chooser = page.get_by_role("region", name=f"“{SHARED}” matches more than one tube")
    expect(chooser).to_be_visible()
    choices = chooser.get_by_role("button", name=re.compile("^TUB-"))
    expect(choices).to_have_count(2)
    mine = chooser.get_by_role("button", name=re.compile(f"^{ours['hrid']}"))
    expect(mine).to_contain_text("Kinase Atlas")
    expect(mine).to_contain_text("Kinase retains, A1")
    supplied = chooser.get_by_role("button", name=re.compile(f"^{theirs['hrid']}"))
    expect(supplied).to_contain_text("Supplier")
    expect(supplied).to_contain_text("Kinase retains, A2")
    # Said, and where the keyboard goes next.
    expect(chooser.get_by_role("heading")).to_be_focused()
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    supplied.click()
    grid = page.get_by_role("grid", name="Kinase retains’s positions")
    expect(grid.get_by_role("gridcell", name=re.compile(r"^A2, "))).to_be_focused()
    expect(
        page.get_by_role("status").filter(has_text=f"{theirs['hrid']} is at A2.")
    ).to_be_visible()
    expect(chooser).to_have_count(0)

    # A typed Iki ID is matched against Iki IDs only: the vendor barcode isn't one.
    page.get_by_role("button", name="Type its printed Iki ID instead").click()
    typed = page.get_by_role("textbox", name="Iki ID")
    typed.fill(SHARED)
    typed.press("Enter")
    expect(page.get_by_role("alert").filter(has_text=IKI_ID_REFUSED)).to_be_visible()
    expect(page.get_by_role("region", name=re.compile("matches more than one"))).to_have_count(0)
    expected_refusals(page, "409 (Conflict)")
    typed.fill(ours["hrid"])
    typed.press("Enter")
    expect(grid.get_by_role("gridcell", name=re.compile(r"^A1, "))).to_be_focused()
    expect(page.get_by_role("status").filter(has_text=f"{ours['hrid']} is at A1.")).to_be_visible()
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_label_a_concealed_tube_shares_is_refused_without_saying_more(
    open_as: Any, world: World, color_scheme: str
) -> None:
    kept = twins(world, concealed=True)
    ours, theirs = kept["ours"], kept["theirs"]
    page = open_box(open_as, kept["retains"], color_scheme)
    scan_label(page, SHARED)
    refusal = page.get_by_role("alert").filter(has_text=REFUSED)
    expect(refusal).to_have_text(REFUSED)
    # No count, no candidates, nothing focused or selected; the Iki ID is the way on.
    expect(page.get_by_text(re.compile("more than one"))).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^TUB-"))).to_have_count(0)
    assert theirs["hrid"] not in page.content()
    typed = page.get_by_role("textbox", name="Iki ID")
    expect(typed).to_be_visible()
    expected_refusals(page, "409 (Conflict)")
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    # The concealed tube's own Iki ID, and one no tube has, are refused in the same words.
    for hrid in (theirs["hrid"], "TUB-999999"):
        typed.fill(hrid)
        typed.press("Enter")
        expect(page.get_by_role("alert").filter(has_text=IKI_ID_REFUSED)).to_have_text(
            IKI_ID_REFUSED
        )
        expect(page.get_by_text(REFUSED)).to_have_count(0)
        expect(page.get_by_role("button", name=re.compile("^TUB-"))).to_have_count(0)
    expected_refusals(page, "409 (Conflict)")
    typed.fill(ours["hrid"])
    typed.press("Enter")
    grid = page.get_by_role("grid", name="Kinase retains’s positions")
    expect(grid.get_by_role("gridcell", name=re.compile(r"^A1, "))).to_be_focused()
    expect(page.get_by_role("status").filter(has_text=f"{ours['hrid']} is at A1.")).to_be_visible()
    page.context.close()


def test_receiving_keeps_the_scanners_exact_text_in_its_history(open_as: Any, world: World) -> None:
    sent = sent_to_a_core(world, 1)
    tube = sent["tubes"][0]
    page = start_receiving(open_as, sent)
    # As a scanner sent it, with the spaces around it: what's recorded is exactly that.
    scan_in(page, " RC000 ")
    expect(heard(page)).to_have_text(f"{tube['hrid']}: matched.")
    line = receipt_line(page, tube)
    line.get_by_role("button", name=f"History of {tube['hrid']}").click()
    history = page.get_by_role("list", name=f"History of {tube['hrid']}, newest first")
    recorded = history.get_by_role("listitem").first
    expect(recorded).to_contain_text("Outcome recorded")
    expect(recorded).to_contain_text("identified by its vendor barcode on this checklist")
    expect(recorded.locator(".scan-raw")).to_have_text(" RC000 ", use_inner_text=False)
    assert recorded.locator(".scan-raw").evaluate("e => e.textContent") == " RC000 "
    receipt = Receipt.objects.get(request_id=sent["ask"]["id"])
    event = ObservationEvent.objects.get(receipt=receipt, kind="outcome")
    assert event.after is not None
    assert event.after["scan"] == {
        "code": " RC000 ",
        "identity_basis": "vendor",
        "scope": "receipt",
        "chosen": False,
    }
    checked(page)
    page.context.close()


def test_drawing_from_and_moving_a_tube_whose_label_another_shares(
    open_as: Any, world: World
) -> None:
    kept = twins(world, kind=RETAIN_TUBE)
    ours, spare = kept["ours"], kept["spare"]
    page: Page = open_as("admin", f"/entities/{ours['hrid']}/")
    card = page.get_by_role("region", name="Volume")
    card.get_by_role("button", name="Draw").click()
    dialog = page.get_by_role("alertdialog")
    dialog.get_by_role("textbox", name="How much").fill("5")
    dialog.get_by_role("textbox", name="Scan its label").fill(SHARED)
    dialog.get_by_role("button", name="Draw", exact=True).click()
    # The tube beside it has the same label: it can't say which is in hand.
    expect(dialog.get_by_text(REFUSED)).to_be_visible()
    assert not Movement.objects.filter(entity_id=ours["id"], kind="drawn").exists()
    dialog.get_by_role("radio", name=re.compile("^Type its printed Iki ID")).check()
    dialog.get_by_role("textbox", name="Iki ID").fill(ours["hrid"])
    expected_refusals(page, "422 (Unprocessable Content)")
    checked(page)
    dialog.get_by_role("button", name="Draw", exact=True).click()
    expect(page.get_by_text(f"{ours['hrid']} has 45 uL now.")).to_be_visible()

    # A retain tube asks for its own scan before a move too.
    page.goto(f"/storage/{kept['retains']['hrid']}/")
    move = select_and_move(page, ours)
    scan = move.get_by_role("textbox", name="Scan each label to confirm")
    scan.fill(SHARED)
    scan.press("Enter")
    shared = move.get_by_role("region", name=f"“{SHARED}” matches more than one tube")
    expect(shared).to_be_visible()
    # Both are shown, and neither can be picked: a shared label confirms neither.
    expect(shared.get_by_role("listitem")).to_have_count(2)
    expect(shared.get_by_role("button", name=re.compile("^(TUB|RTB)-"))).to_have_count(0)
    confirmed = move.get_by_role("list", name="Confirmed")
    expect(confirmed).to_contain_text(f"{ours['hrid']} not scanned yet")
    checked(page)
    typed = move.get_by_role("textbox", name="Iki ID")
    typed.fill(ours["hrid"])
    typed.press("Enter")
    expect(confirmed).to_contain_text(f"{ours['hrid']} confirmed")
    move.get_by_role("textbox", name="Container").fill(spare["hrid"])
    move.get_by_role("textbox", name="Reason").fill("Consolidating retains")
    move.get_by_role("button", name="Check where they go").click()
    expect(move.get_by_role("table", name="What moving them does")).to_contain_text(
        f"{spare['hrid']}, A1"
    )
    move.get_by_role("button", name="Move it").click()
    move.wait_for(state="hidden")
    assert where(ours) == (spare["hrid"], 1, 1)
    page.context.close()


def test_draw_holds_still_as_the_box_map_comes_above_it(open_as: Any, world: World) -> None:
    """A tube's page draws where it is, then its box's map, the last of its reads, in the card
    above Volume. A press on Draw as the map comes ends on Draw, and opens it: the map comes
    once it's let go. It didn't: the map pushed Draw down under the press, which ended on
    the card and drew nothing (Firefox lost the click in a full run)."""
    kept = twins(world)
    ours, retains = kept["ours"], kept["retains"]
    page: Page = open_as("admin", f"/entities/{ours['hrid']}/")
    # Tall enough that Draw is in the window unscrolled: a browser's scroll anchoring then
    # can't keep it in place for the map, as it would below the fold.
    page.set_viewport_size({"width": 1440, "height": 1600})
    draw = page.get_by_role("region", name="Volume").get_by_role("button", name="Draw")
    grid = page.get_by_role(
        "grid", name=f"Kinase retains’s positions, with {ours['hrid']}’s marked"
    )
    # The first load's map is in before its read is held: held as it came, the reload would
    # abort it and the reload's own read would wait for ever.
    expect(grid).to_be_visible()
    held: list[Route] = []
    box_read = f"**/api/v1/storage/containers/{retains['hrid']}"
    page.route(box_read, lambda route: held.append(route))
    with page.expect_request(box_read):
        page.reload()
    page.get_by_role("navigation", name=f"Where {ours['hrid']} is").wait_for()
    draw.wait_for()
    page.evaluate(NEXT_FRAMES)
    assert grid.count() == 0
    assert page.evaluate("window.scrollY") == 0
    drawn = draw.bounding_box()
    assert drawn
    page.mouse.move(drawn["x"] + drawn["width"] / 2, drawn["y"] + drawn["height"] / 2)
    page.mouse.down()
    with page.expect_response(box_read):
        held[0].continue_()
    page.unroute(box_read)
    # The map's read is in: it would be drawn by now.
    page.evaluate(NEXT_FRAMES)
    page.evaluate(NEXT_FRAMES)
    pressed = draw.bounding_box()
    page.mouse.up()
    assert pressed == drawn, f"Draw drawn at {drawn}, then at {pressed} when let go."
    dialog = page.get_by_role("alertdialog")
    expect(dialog.get_by_role("textbox", name="How much")).to_be_visible()
    # And once it's let go, the map.
    expect(grid).to_be_visible()
    checked(page)
    page.context.close()


def select_and_move(page: Page, record: dict[str, Any]) -> Any:
    """Selects the tube at A1 on the box's map and opens Move for it."""
    page.get_by_role("button", name="Select several").click()
    grid = page.get_by_role("grid", name=re.compile("’s positions$"))
    grid.get_by_role("gridcell", name=re.compile(r"^A1, ")).click()
    page.get_by_role("region", name="Selected").get_by_role("button", name="Move…").click()
    return page.get_by_role("dialog", name=f"Move {record['hrid']}")


def test_a_move_whose_label_became_shared_since_its_scan_moves_nothing(
    open_as: Any, world: World
) -> None:
    kept = twins(world, kind=RETAIN_TUBE, apart=True)
    ours, theirs, retains, spare = kept["ours"], kept["theirs"], kept["retains"], kept["spare"]
    page: Page = open_as("admin", f"/storage/{retains['hrid']}/")
    move = select_and_move(page, ours)
    # Alone in its box, its vendor label is enough.
    scan = move.get_by_role("textbox", name="Scan each label to confirm")
    scan.fill(SHARED)
    scan.press("Enter")
    confirmed = move.get_by_role("list", name="Confirmed")
    expect(confirmed).to_contain_text(f"{ours['hrid']} confirmed")
    move.get_by_role("textbox", name="Container").fill(spare["hrid"])
    move.get_by_role("textbox", name="Reason").fill("Consolidating retains")
    move.get_by_role("button", name="Check where they go").click()
    expect(move.get_by_role("table", name="What moving them does")).to_contain_text(
        f"{spare['hrid']}, A1"
    )
    # Before it's moved, the supplier's tube with the same label is put beside it.
    put(world, (theirs, retains, "A2"))
    move.get_by_role("button", name="Move it").click()
    now = move.get_by_role("region", name=f"“{SHARED}” matches more than one tube")
    expect(now).to_contain_text(
        "This label now matches more than one tube, so nothing was moved and "
        f"{ours['hrid']} isn’t confirmed."
    )
    expect(now.get_by_role("button", name=re.compile("^(TUB|RTB)-"))).to_have_count(0)
    expect(move.get_by_role("alert").filter(has_text="Nothing was moved")).to_be_visible()
    # Nothing was written, its tick is taken back, and the move it was is still there.
    assert where(ours) == (retains["hrid"], 1, 1)
    expect(confirmed).to_contain_text(f"{ours['hrid']} not scanned yet")
    # What moving it does is checked again: it can't move until it's scanned.
    plan = move.get_by_role("table", name="What moving them does")
    expect(plan).to_contain_text(f"Scan {ours['hrid']} to confirm")
    expect(move.get_by_role("button", name="Move it")).to_have_count(0)
    expect(move.get_by_role("textbox", name="Container")).to_have_value(spare["hrid"])
    expected_refusals(page, "422 (Unprocessable Content)")
    checked(page)
    typed = move.get_by_role("textbox", name="Iki ID")
    typed.fill(ours["hrid"])
    typed.press("Enter")
    expect(confirmed).to_contain_text(f"{ours['hrid']} confirmed")
    expect(plan).not_to_contain_text(f"Scan {ours['hrid']} to confirm")
    move.get_by_role("button", name="Move it").click()
    move.wait_for(state="hidden")
    assert where(ours) == (spare["hrid"], 1, 1)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_label_without_a_vendor_barcode_says_why_it_cant_add_one(
    open_as: Any, world: World, color_scheme: str
) -> None:
    kept = a_freezer(world)
    page = open_as("admin", f"/storage/{kept['spare']['hrid']}/", color_scheme)
    page.get_by_role("button", name="Print its label").click()
    dialog = page.get_by_role("dialog", name=re.compile("^Print labels for BOX-"))
    face = dialog.get_by_role("img", name=re.compile("^Label for BOX-"))
    expect(face.locator("img")).to_have_count(1)
    vendor = dialog.get_by_role("checkbox", name="Also print the vendor barcode (Code 128)")
    vendor.check()
    # The server's reason, for the preview as for a print: a box has no vendor barcode.
    problem = dialog.get_by_role("alert").filter(has_text="These labels can’t be printed as chosen")
    expect(problem).to_contain_text("There is no vendor barcode on this record.")
    expect(dialog.get_by_role("button", name="Print", exact=True)).to_have_attribute(
        "aria-disabled", "true"
    )
    expected_refusals(page, "422 (Unprocessable Content)")
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    vendor.uncheck()
    expect(problem).to_have_count(0)
    expect(face.locator("img")).to_have_count(1)
    page.context.close()


# --- Unregistered labels in a count ----------------------------------------------------------

#: A hand-written label two tubes in one box carry: no record has it.
BUFFER = "BUFFER A"
#: The label as the page quotes it, in typographic single quotes.
QUOTED = "\u2018BUFFER A\u2019"
#: An API request from a signed-in page: its session, and its page's CSRF token.
SEND = """async ([method, url, body]) => {
  const token = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
  const answer = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRFToken": token },
    body: body === null ? undefined : JSON.stringify(body),
  });
  return [answer.status, await answer.json()];
}"""


def send(page: Page, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """`path` under /api/v1, as `page`'s person: what the server answers, and its body."""
    status, answer = page.evaluate(SEND, [method, f"/api/v1{path}", body])
    return status, answer


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_an_unregistered_label_seen_in_another_slot_asks_which_tube_it_is(
    open_as: Any, world: World, color_scheme: str
) -> None:
    box = make(world, BOX, {"name": "Counting box", "rows": 1, "columns": 6})
    # A second operator, signed in beside the first, who sends a rack file through the API.
    editor = open_as("editor", f"/storage/{box['hrid']}/")
    status, count = send(editor, "POST", f"/storage/containers/{box['id']}/reconciliations")
    assert status == 201, count
    path = f"/reconciliations/{count['id']}"
    # One rack read: the same label at A1 and A2, a line for each.
    status, seeded = send(
        editor, "POST", f"{path}/scans", {"scans": [reading("A1", BUFFER), reading("A2", BUFFER)]}
    )
    assert status == 200, seeded
    before = by_slot(seeded)
    a1, a2 = before["A1"], before["A2"]
    assert a1["id"] != a2["id"]
    page = open_as("admin", f"/storage{path}/", color_scheme)
    expect(page.get_by_role("heading", level=1, name="Reconcile Counting box")).to_be_visible()

    def scan_a3() -> Any:
        page.get_by_role("gridcell", name=re.compile(r"^A3, ")).click()
        scan = page.get_by_role("textbox", name="What’s in A3")
        scan.fill(BUFFER)
        scan.press("Enter")
        chooser = page.get_by_role("region", name=f"{QUOTED} at A3 is…")
        expect(chooser).to_be_visible()
        expect(chooser.get_by_role("heading")).to_be_focused()
        return chooser

    # Nothing is saved until the person says which tube it is; cancelling saves nothing.
    recorded = ObservationEvent.objects.count()
    chooser = scan_a3()
    expect(chooser.get_by_role("button")).to_have_count(4)
    expect(chooser.get_by_role("button", name=re.compile("^Another tube"))).to_be_visible()
    for slot in ("A1", "A2"):
        expect(
            chooser.get_by_role("button", name=re.compile(f"^The tube from {slot}, moved here"))
        ).to_contain_text(f"Scanned at {slot} by Editor")
    expected_refusals(page, "409")
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    chooser.get_by_role("button", name="Cancel").click()
    expect(chooser).to_have_count(0)
    expect(page.get_by_text("Nothing was recorded at A3.")).to_be_visible()
    assert ObservationEvent.objects.count() == recorded

    # A move names its line, and asks why.
    chooser = scan_a3()
    chooser.get_by_role("button", name=re.compile("^The tube from A1, moved here")).click()
    expect(page.get_by_role("dialog", name="Move the tube from A1 to A3?")).to_be_visible()
    # Its title follows the choice: the same dialog throughout.
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("listitem")).to_contain_text(f"{BUFFER} was scanned at A1")
    reason = dialog.get_by_role("textbox", name="Why are you changing this?")
    expect(reason).to_be_focused()
    move = dialog.get_by_role("button", name="Record the move")
    move.click()
    expect(dialog.get_by_role("alert")).to_have_text("Say why you’re making this change.")
    expected_refusals(page, "409")
    checked(page)
    why_moved = "Took the A1 tube out and put it back in A3"
    reason.fill(why_moved)

    # Meanwhile, a second operator reads A1 again: the move is asked about again.
    status, reread = send(
        editor,
        "POST",
        f"{path}/scans",
        {"scans": [reading("A1", BUFFER)], "reviewed_lines": [review(a1)]},
    )
    assert status == 200, reread
    with page.expect_response("**/api/v1/reconciliations/*/scans") as refused:
        move.click()
    assert refused.value.status == 409
    assert refused.value.request.post_data_json == {
        "scans": [
            {
                "position": "A3",
                "code": BUFFER,
                "unregistered": {"action": "move", "line_id": a1["id"]},
            }
        ],
        "reviewed_lines": [review(a1)],
        "reason": why_moved,
    }
    stale = dialog.get_by_role("alert").filter(has_text="A1 changed while you were looking at it")
    expect(stale).to_contain_text("Scan changed by Editor")
    expect(page.get_by_role("dialog", name=f"Record {QUOTED} at A3?")).to_be_visible()
    again = dialog.get_by_role("region", name=f"{QUOTED} at A3 is…")
    expect(again).to_be_visible()
    expect(reason).to_have_value(why_moved)
    expect(dialog.get_by_role("button", name="Record the new scan")).to_have_attribute(
        "aria-disabled", "true"
    )
    expected_refusals(page, "409")
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    again.get_by_role("button", name=re.compile("^The tube from A1, moved here")).click()
    expect(dialog.get_by_text(f"{QUOTED} at A3: the tube from A1, moved here.")).to_be_focused()
    fresh = by_slot(reread)["A1"]
    with page.expect_response("**/api/v1/reconciliations/*/scans") as accepted:
        move.click()
    assert accepted.value.status == 200
    assert accepted.value.request.post_data_json["reviewed_lines"] == [review(fresh)]
    expect(dialog).to_have_count(0)
    expect(page.get_by_text("A3: BUFFER A, moved from A1.")).to_be_visible()

    # Another tube with the label is one step: no reason, and the others stay.
    page.get_by_role("gridcell", name=re.compile(r"^A4, ")).click()
    page.get_by_role("textbox", name="What’s in A4").fill(BUFFER)
    page.get_by_role("textbox", name="What’s in A4").press("Enter")
    four = page.get_by_role("region", name=f"{QUOTED} at A4 is…")
    expect(
        four.get_by_role("button", name=re.compile("^The tube from A2, moved here"))
    ).to_be_visible()
    expect(
        four.get_by_role("button", name=re.compile("^The tube from A3, moved here"))
    ).to_be_visible()
    four.get_by_role("button", name=re.compile("^Another tube")).click()
    expect(page.get_by_text("A4: BUFFER A, recorded as another tube.")).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(0)

    # The moved line kept its ID; its sibling at A2 is as it was.
    after = by_slot(send(editor, "GET", path)[1])
    assert set(after) == {"A2", "A3", "A4"}
    assert after["A3"]["id"] == a1["id"] and after["A2"] == a2
    assert after["A4"]["id"] not in {a1["id"], a2["id"]}
    events = list(reversed(send(editor, "GET", f"{path}/history")[1]["items"]))
    moved, added = events[-2:]
    assert (moved["line_id"], moved["reason"]) == (a1["id"], why_moved)
    assert moved["actor"]["id"] == str(world.users["admin"].pk)
    assert moved["before"]["position"] == "A1" and moved["after"]["position"] == "A3"
    assert moved["after"]["unregistered_action"] == "move"
    assert added["line_id"] == after["A4"]["id"] and added["reason"] == ""
    assert added["after"]["unregistered_action"] == "additional"
    # A2's only observation is the rack read that recorded it.
    assert [e["id"] for e in events if e["line_id"] == a2["id"]] == [a2["observation"]["id"]]

    page.get_by_text("This count’s history").click()
    trail = page.get_by_role("list", name="History of this count, newest first")
    expect(
        trail.get_by_role("listitem").filter(has_text="Recorded as another tube")
    ).to_contain_text("BUFFER A at A4")
    shown = trail.get_by_role("listitem").filter(has_text="Moved from A1")
    expect(shown).to_contain_text("· Admin")
    expect(shown).to_contain_text(f"“{why_moved}”")
    # Each line is named with where it is: three read the same label.
    to_resolve = page.get_by_role("region", name="To resolve")
    for slot in ("A2", "A3", "A4"):
        expect(
            to_resolve.get_by_role("combobox", name=f"What to do about BUFFER A at {slot}")
        ).to_be_visible()
    expected_refusals(page, "409")
    checked(page)
    fits(page)
    page.context.close()
    editor.context.close()


# --- Where each received tube goes -----------------------------------------------------------


def received_and_handed_over(
    open_as: Any, sent: dict[str, Any], color_scheme: str
) -> tuple[Page, str]:
    """Each of `sent`'s tubes scanned in and its handover saved: ready for its decisions."""
    page = start_receiving(open_as, sent, color_scheme=color_scheme)
    for i, tube in enumerate(sent["tubes"]):
        scan_in(page, f"RC{i:03}")
        expect(receipt_line(page, tube).get_by_role("button", name="Matched")).to_have_attribute(
            "aria-pressed", "true"
        )
    page.get_by_role("textbox", name="Handed over by").fill("Courier, waybill 1Z999")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    receipt = Receipt.objects.get(request_id=sent["ask"]["id"])
    return page, f"/api/v1/receipts/{receipt.pk}"


def press_and_release(page: Page, target: Any) -> None:
    """A press and release on the control, as a hand would: whether or not it's available."""
    box = target.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.up()


def held_by(page: Page, path: str) -> dict[str, Any]:
    """Read back through the API: the receipt, and what's in the caller's custody."""
    receipt = page.request.get(path)
    out = page.request.get("/api/v1/storage/checked-out")
    waiting = page.request.get("/api/v1/storage/awaiting-put-away")
    assert receipt.ok and out.ok and waiting.ok
    return {"receipt": receipt.json(), "out": out.json(), "waiting": waiting.json()["items"]}


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_completing_waits_for_the_box_and_puts_each_tube_where_it_was_shown(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 2)
    tubes, incoming = sent["tubes"], sent["incoming"]
    page, path = received_and_handed_over(open_as, sent, color_scheme)
    complete = page.get_by_role("button", name="Complete receiving")
    plan = page.get_by_role("table", name=re.compile(r"^Where each goes$| · \d+ free$"))
    choices = [plan.get_by_role("combobox", name=f"Where {t['hrid']} goes") for t in tubes]
    # Nothing is decided for them: not put away, and not kept in custody.
    for choice in choices:
        expect(choice).to_have_value("")
    expect(complete).to_be_disabled()

    # The real box lookup, held back: its answer isn't known yet.
    held: list[Route] = []
    page.route("**/api/v1/storage/scan/resolve", lambda route: held.append(route))
    page.get_by_role("textbox", name=re.compile("^Box")).fill(incoming["hrid"])
    page.get_by_role("button", name="Show its free positions").click()
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    assert held, "The box lookup never left the page."
    expect(complete).to_be_disabled()
    expect(complete).to_have_accessible_description("Finding the box and its free positions…")
    before = held_by(page, path)
    sent_complete: list[Request] = []
    page.on(
        "request",
        lambda r: sent_complete.append(r) if r.url.endswith(path + "/complete") else None,
    )
    press_and_release(page, complete)
    complete.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    assert sent_complete == []
    assert held_by(page, path) == before
    assert before["receipt"]["status"] == "open"
    assert before["out"] == [] and before["waiting"] == []
    checked(page)

    # Released: the allocation shown is what's reviewed, one well changed by hand.
    held[0].continue_()
    page.unroute("**/api/v1/storage/scan/resolve")
    expect(plan).to_have_accessible_name(f"{incoming['hrid']} Incoming · 4 free")
    expect(choices[0]).to_have_value("A1")
    expect(choices[1]).to_have_value("A2")
    shown = choices[0].get_by_role("option", name=f"Put away in {incoming['hrid']}, A1")
    expect(shown).to_have_count(1)
    choices[1].select_option("B2")
    expect(complete).to_have_accessible_description(f"Ready: 2 put away in {incoming['hrid']}.")
    expect(complete).to_be_enabled()

    # Another box typed but not shown: what's planned is no longer what the field says.
    field = page.get_by_role("textbox", name=re.compile("^Box"))
    field.fill("BOX-999999")
    expect(complete).to_be_disabled()
    expect(complete).to_have_accessible_description(
        f"Show the free positions in BOX-999999 first, or put the text back to {incoming['hrid']}."
    )
    expect(choices[1]).to_have_value("B2")
    press_and_release(page, complete)
    page.wait_for_timeout(300)
    assert sent_complete == []
    field.fill(incoming["hrid"])
    expect(complete).to_be_enabled()
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    with page.expect_response(f"**{path}/complete") as answered:
        complete.click()
    assert answered.value.status == 200
    page.get_by_role("heading", level=2, name="Received").wait_for()
    done = held_by(page, path)["receipt"]
    assert done["status"] == "completed"
    placed = {
        line["code"]: (line["put_away"]["hrid"], line["put_away"]["position"])
        for line in done["lines"]
    }
    assert placed == {"RC000": (incoming["hrid"], "A1"), "RC001": (incoming["hrid"], "B2")}
    assert not any(line["received_unplaced"] for line in done["lines"])
    assert where(tubes[0]) == (incoming["hrid"], 1, 1)
    assert where(tubes[1]) == (incoming["hrid"], 2, 2)
    page.get_by_role("region", name="Received", exact=True).locator("summary").first.click()
    report = page.get_by_role("table", name="What this receipt checked")
    expect(report.get_by_role("row", name=re.compile(f"^{tubes[1]['hrid']}"))).to_contain_text(
        "Incoming, B2"
    )
    checked(page)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_tube_kept_in_custody_is_received_without_a_location_and_awaits_put_away(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 1)
    tube = sent["tubes"][0]
    page, path = received_and_handed_over(open_as, sent, color_scheme)
    complete = page.get_by_role("button", name="Complete receiving")
    choice = page.get_by_role("combobox", name=f"Where {tube['hrid']} goes")
    expect(complete).to_be_disabled()
    expect(complete).to_have_accessible_description(
        f"Decide for {tube['hrid']}: put it away in a free position, or keep it in your custody."
    )
    choice.select_option(label="Keep in my custody")
    expect(complete).to_have_accessible_description("Ready: 1 kept in your custody.")
    checked(page)
    with page.expect_response(f"**{path}/complete") as answered:
        complete.click()
    assert answered.value.status == 200
    body = answered.value.request.post_data_json
    assert body is not None
    assert body["put_away"] == [] and len(body["keep_in_custody"]) == 1
    page.get_by_role("heading", level=2, name="Received").wait_for()
    now = held_by(page, path)
    (line,) = now["receipt"]["lines"]
    assert (line["received_unplaced"], line["put_away"]) == (True, None)
    assert [(x["id"], x["custodian"]["id"]) for x in now["waiting"]] == [
        (str(tube["id"]), str(world.users["tech"].pk))
    ]
    page.get_by_role("region", name="Received", exact=True).locator("summary").first.click()
    expect(
        page.get_by_role("table", name="What this receipt checked").get_by_role(
            "row", name=re.compile(f"^{tube['hrid']}")
        )
    ).to_contain_text("Not put away: kept in custody")

    # Storage lists it until someone puts it away.
    page.goto("/storage/")
    waiting = page.get_by_role("region", name="Received, not yet put away")
    row = waiting.get_by_role("row", name=re.compile(f"^{tube['hrid']}"))
    expect(row).to_contain_text("You")
    checked(page)
    fits(page)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_well_taken_meanwhile_is_refused_on_its_tube_and_read_again(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 3)
    tubes, incoming = sent["tubes"], sent["incoming"]
    colleague = another_receiver(world, sent["core"], "tech2", "Tech Two")
    theirs = ref(entities.create(colleague, TUBE, sent["core"], {"barcode": "OTHER-1"}))
    page, path = received_and_handed_over(open_as, sent, color_scheme)
    complete = page.get_by_role("button", name="Complete receiving")
    page.get_by_role("textbox", name=re.compile("^Box")).fill(incoming["hrid"])
    page.get_by_role("button", name="Show its free positions").click()
    plan = page.get_by_role("table", name=re.compile(rf"^{incoming['hrid']} Incoming · \d+ free$"))
    expect(plan).to_have_accessible_name(f"{incoming['hrid']} Incoming · 4 free")
    choices = [plan.get_by_role("combobox", name=f"Where {t['hrid']} goes") for t in tubes]
    # The first chosen by hand, the second where it's shown, the third kept in custody.
    choices[0].select_option("B2")
    expect(choices[1]).to_have_value("A1")
    choices[2].select_option(label="Keep in my custody")
    expect(complete).to_have_accessible_description(
        f"Ready: 2 put away in {incoming['hrid']} and 1 kept in your custody."
    )

    # Meanwhile the second operator puts their tube in A1, the well shown for the second.
    other = open_as("tech2", "/storage/", color_scheme)
    status, body = send(
        other,
        "POST",
        "/storage/moves",
        {
            "items": [{"record": str(theirs["id"]), "to": str(incoming["id"]), "position": "A1"}],
            "reason": "Put away",
            "confirmations": {str(theirs["id"]): iki_label(theirs)},
        },
    )
    assert status == 200, body
    sent_completions = completions(page, path)
    with page.expect_response(f"**{path}/put-away/preview") as preview:
        complete.click()
    assert preview.value.status == 200

    # The newly available well is shown before anything is sent for completion.
    refusal = page.get_by_role("region", name="Complete", exact=True).get_by_role("alert")
    expect(refusal).to_contain_text(f"{tubes[1]['hrid']}: Previously A1, now A2")
    expect(plan).to_have_accessible_name(f"{incoming['hrid']} Incoming · 3 free")
    expect(choices[1]).to_have_value("A2")
    assert sent_completions == []
    for choice in choices:
        expect(choice.get_by_role("option", name=re.compile(r", A1$"))).to_have_count(0)
    taken = page.get_by_role("grid").get_by_role("gridcell", name=re.compile(r"^A1, "))
    expect(taken).to_have_accessible_name(re.compile(f"^A1, {theirs['hrid']}"))
    expect(choices[0]).to_have_value("B2")
    expect(choices[2]).to_have_value("keep")
    expect(complete).to_be_enabled()
    # Nothing was completed, placed or taken into custody.
    now = held_by(page, path)
    assert now["receipt"]["status"] == "open"
    assert now["out"] == [] and now["waiting"] == []
    box = page.request.get(f"/api/v1/storage/containers/{incoming['id']}")
    assert box.ok
    assert {o["position"]: o["ref"]["hrid"] for o in box.json()["contents"] if o["ref"]} == {
        "A1": theirs["hrid"]
    }
    checked(page)

    # A second press accepts the new position along with the original manual decisions.
    with (
        page.expect_response(f"**{path}/put-away/preview") as reviewed,
        page.expect_response(f"**{path}/complete") as answered,
    ):
        complete.click()
    assert answered.value.status == 200
    assert len(sent_completions) == 1
    approved = reviewed.value.json()["plan"]
    submission = sent_completions[0].post_data_json
    assert isinstance(submission, dict)
    assert approved and submission["plan"] == approved
    page.get_by_role("heading", level=2, name="Received").wait_for()
    done = held_by(page, path)
    lines = {line["code"]: line for line in done["receipt"]["lines"]}
    assert (lines["RC000"]["put_away"]["hrid"], lines["RC000"]["put_away"]["position"]) == (
        incoming["hrid"],
        "B2",
    )
    assert (lines["RC001"]["put_away"]["hrid"], lines["RC001"]["put_away"]["position"]) == (
        incoming["hrid"],
        "A2",
    )
    assert (lines["RC002"]["put_away"], lines["RC002"]["received_unplaced"]) == (None, True)
    assert [x["id"] for x in done["waiting"]] == [str(tubes[2]["id"])]
    other.context.close()
    page.context.close()


def completions(page: Page, path: str) -> list[Request]:
    """Every completion the page sends for the receipt at `path`, from now on."""
    sent: list[Request] = []
    page.on("request", lambda r: sent.append(r) if r.url.endswith(path + "/complete") else None)
    return sent


def centre(target: Any) -> tuple[float, float]:
    """The control's centre, scrolled into view: where a press on it lands."""
    target.scroll_into_view_if_needed()
    box = target.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    assert target.evaluate(
        "(el, [x, y]) => el.contains(document.elementFromPoint(x, y))", [x, y]
    ), "Something else is where the press would land."
    return x, y


def hold(page: Page, glob: str, method: str) -> list[Route]:
    """Holds each `method` request matching `glob` until the test lets it go."""
    held: list[Route] = []

    def handle(route: Route) -> None:
        if route.request.method == method:
            held.append(route)
        else:
            route.continue_()

    page.route(glob, handle)
    return held


def until_held(page: Page, held: list[Route]) -> None:
    for _ in range(100):
        if held:
            return
        page.wait_for_timeout(50)
    raise AssertionError("The request never left the page.")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_press_held_while_the_wells_appear_completes_nothing(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 2)
    tubes, incoming = sent["tubes"], sent["incoming"]
    page, path = received_and_handed_over(open_as, sent, color_scheme)
    complete = page.get_by_role("button", name="Complete receiving")
    plan = page.get_by_role("table", name=re.compile(r"^Where each goes$| · \d+ free$"))
    first = plan.get_by_role("combobox", name=f"Where {tubes[0]['hrid']} goes")
    held = hold(page, "**/api/v1/storage/scan/resolve", "POST")
    page.get_by_role("textbox", name=re.compile("^Box")).fill(incoming["hrid"])
    page.get_by_role("button", name="Show its free positions").click()
    until_held(page, held)
    expect(complete).to_have_accessible_description("Finding the box and its free positions…")
    sending = completions(page, path)
    before = held_by(page, path)

    # Pressed while it can't complete; the box's wells appear under the press.
    page.mouse.move(*centre(complete))
    page.mouse.down()
    held[0].continue_()
    page.unroute("**/api/v1/storage/scan/resolve")
    expect(first).to_have_value("A1")
    expect(complete).to_be_enabled()
    expect(complete).to_have_accessible_description(f"Ready: 2 put away in {incoming['hrid']}.")
    # Released on Complete, wherever it is now: the press began before the plan was shown.
    page.mouse.move(*centre(complete))
    page.mouse.up()
    page.wait_for_timeout(500)
    assert sending == []
    assert held_by(page, path) == before
    expect(complete).to_be_enabled()
    checked(page)

    # A fresh press completes what's shown.
    with page.expect_response(f"**{path}/complete") as answered:
        page.mouse.move(*centre(complete))
        page.mouse.down()
        page.mouse.up()
    assert answered.value.status == 200
    page.get_by_role("heading", level=2, name="Received").wait_for()
    assert len(sending) == 1
    done = held_by(page, path)["receipt"]
    assert done["status"] == "completed"
    assert {line["code"]: line["put_away"]["position"] for line in done["lines"]} == {
        "RC000": "A1",
        "RC001": "A2",
    }
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_wells_are_offered_only_in_a_usable_box_and_for_a_tube_that_can_be_put_away(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 2)
    tubes, incoming, tech = sent["tubes"], sent["incoming"], world.users["tech"]
    old = ref(entities.create(tech, BOX, sent["core"], {"name": "Old", "rows": 1, "columns": 2}))
    entities.set_archived(tech, Entity.objects.get(pk=old["id"]), True, reason="Retired")
    page, path = received_and_handed_over(open_as, sent, color_scheme)
    # The second tube is archived by its owner while it's being received.
    entities.set_archived(
        world.users["editor"], Entity.objects.get(pk=tubes[1]["id"]), True, reason="Superseded"
    )
    page.reload()
    complete = page.get_by_role("button", name="Complete receiving")
    plan = page.get_by_role("table", name=re.compile(r"^Where each goes$| · \d+ free$"))
    choices = [plan.get_by_role("combobox", name=f"Where {t['hrid']} goes") for t in tubes]
    field = page.get_by_role("textbox", name=re.compile("^Box"))

    # An archived box: nothing goes in it, and it says so where the box is shown.
    field.fill(old["hrid"])
    page.get_by_role("button", name="Show its free positions").click()
    put_away = page.get_by_role("region", name="Put away")
    expect(put_away.get_by_role("note")).to_have_text(
        f"{old['hrid']} is archived: choose another box, or keep them in your custody."
    )
    expect(plan).to_have_accessible_name(f"{old['hrid']} Old · 2 free")
    for choice in choices:
        expect(choice).to_have_value("")
        expect(choice.get_by_role("option")).to_have_text(["Not decided", "Keep in my custody"])
    expect(complete).to_be_disabled()
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})

    # A box that can be used: a well for the first tube, none for the archived one.
    field.fill(incoming["hrid"])
    page.get_by_role("button", name="Show its free positions").click()
    expect(plan).to_have_accessible_name(f"{incoming['hrid']} Incoming · 4 free")
    expect(put_away.get_by_role("note")).to_have_count(0)
    expect(choices[0]).to_have_value("A1")
    expect(choices[1]).to_have_value("")
    expect(choices[1]).to_have_accessible_description(
        f"{tubes[1]['hrid']} is archived: restore it to put it away, or keep it in your custody."
    )
    expect(choices[1].get_by_role("option")).to_have_text(["Not decided", "Keep in my custody"])
    expect(complete).to_have_accessible_description(
        f"Decide for {tubes[1]['hrid']}: put it away in a free position, or keep it in your "
        "custody."
    )
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    choices[1].select_option(label="Keep in my custody")
    with page.expect_response(f"**{path}/complete") as answered:
        complete.click()
    assert answered.value.status == 200
    page.get_by_role("heading", level=2, name="Received").wait_for()
    now = held_by(page, path)
    lines = {line["code"]: line for line in now["receipt"]["lines"]}
    assert lines["RC000"]["put_away"]["position"] == "A1"
    assert (lines["RC001"]["put_away"], lines["RC001"]["received_unplaced"]) == (None, True)
    assert where(tubes[0]) == (incoming["hrid"], 1, 1)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_completing_waits_for_each_write_and_approves_the_revision_that_includes_them(
    open_as: Any, world: World, color_scheme: str
) -> None:
    sent = sent_to_a_core(world, 1)
    tube = sent["tubes"][0]
    page = start_receiving(open_as, sent, color_scheme=color_scheme)
    receipt = Receipt.objects.get(request_id=sent["ask"]["id"])
    path = f"/api/v1/receipts/{receipt.pk}"
    complete = page.get_by_role("button", name="Complete receiving")
    sending = completions(page, path)
    scan_in(page, "RC000")
    line = receipt_line(page, tube)
    expect(line.get_by_role("button", name="Matched")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("combobox", name=f"Where {tube['hrid']} goes").select_option(
        label="Keep in my custody"
    )
    expect(complete).to_be_enabled()

    # The handover on its way: Complete waits for it, and says so.
    held = hold(page, f"**{path}", "PATCH")
    page.get_by_role("textbox", name="Handed over by").fill("Courier, waybill 1Z999")
    page.get_by_role("button", name="Save", exact=True).click()
    until_held(page, held)
    expect(complete).to_be_disabled()
    expect(complete).to_have_accessible_description("Waiting for the handover to be saved.")
    expect(page.get_by_role("region", name="Complete", exact=True)).to_contain_text(
        "Waiting for the handover to be saved."
    )
    press_and_release(page, complete)
    page.wait_for_timeout(300)
    assert sending == []
    checked(page)
    fits(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    held[0].continue_()
    page.unroute(f"**{path}")
    expect(page.get_by_text("Saved.", exact=True)).to_be_visible()

    # A note on its way, likewise.
    held = hold(page, f"**{path}/lines/*", "PATCH")
    line.get_by_role("textbox", name=re.compile("^Note on")).fill("Label smudged")
    line.get_by_role("button", name="Save note").click()
    until_held(page, held)
    expect(complete).to_have_accessible_description(
        f"Waiting for the note on {tube['hrid']} to be saved."
    )
    checked(page)
    held[0].continue_()
    page.unroute(f"**{path}/lines/*")
    expect(complete).to_have_accessible_description("Ready: 1 kept in your custody.")

    # Read again just before it's sent: the revision approved includes both writes.
    order: list[str] = []
    page.on(
        "request",
        lambda r: (
            order.append(f"{r.method} {r.url.split('/api/v1')[-1]}")
            if r.url.endswith(path) or r.url.endswith(path + "/complete")
            else None
        ),
    )
    with page.expect_response(f"**{path}/complete") as answered:
        complete.click()
    assert answered.value.status == 200
    page.get_by_role("heading", level=2, name="Received").wait_for()
    short = path.removeprefix("/api/v1")
    assert order[:2] == [f"GET {short}", f"POST {short}/complete"]
    body = answered.value.request.post_data_json
    assert body is not None
    receipt.refresh_from_db()
    assert body["plan_revision"] == receipt.plan_revision - 1
    (done,) = held_by(page, path)["receipt"]["lines"]
    assert (done["note"], done["received_unplaced"]) == ("Label smudged", True)
    assert held_by(page, path)["receipt"]["handed_over_by"] == "Courier, waybill 1Z999"
    page.context.close()

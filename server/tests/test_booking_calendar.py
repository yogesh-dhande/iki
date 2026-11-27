# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The equipment plugin's booking calendar (ADR 0117), in each browser, under the
policy Iki's component frames have (ADR 0084): it draws the bookings it's given by day,
asks for a month at a time and for the rest of one, and opens only a booking it shows.

The frame is served here, not by Iki, so it needs no plugin installed: its policy is the
component frame's, and it runs Iki's hardening script first and the module by its
integrity, as Iki's frame does. A driver stands in for Iki's bridge."""

import base64
import datetime as dt
import hashlib
import json
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, browser_name, playwright  # noqa: F401

from iki_server.web import frames

pytestmark = pytest.mark.built_web_app

MODULE = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "equipment"
    / "components"
    / "booking-calendar.js"
)
POLICY = (
    "default-src 'none'; script-src " + frames.HARDEN_SOURCE + " {origin}/calendar.js "
    "{origin}/driver.js; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'; "
    "sandbox allow-scripts"
)
PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
    '<link rel="icon" href="data:,"><title>Bookings</title>\n'
    + frames.HARDEN_TAG
    + '<script type="module" src="/calendar.js" integrity="{integrity}" '
    'crossorigin="anonymous"></script>\n'
    '<script type="module" src="/driver.js" crossorigin="anonymous"></script>\n'
    "</head><body><main></main></body></html>\n"
)
#: Sets the element's place and entities as Iki's bridge does, and keeps what it asks for.
DRIVER = b"""
window.refused = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.refused.push(`${e.violatedDirective} ${e.blockedURI}`);
});
window.sent = [];
const asked = new URLSearchParams(location.search);
const name = asked.get("element") ?? "equipment-booking-calendar";
await customElements.whenDefined(name);
const element = document.createElement(name);
for (const name of ["iki-refine", "iki-more", "iki-open", "iki-select"]) {
  element.addEventListener(name, (e) => window.sent.push({ type: name, detail: e.detail }));
}
document.querySelector("main").append(element);
window.give = (entities) => { element.entities = entities; };
element.place = {
  slot: asked.get("slot") ?? "entity.tab",
  label: asked.get("label") ?? "Bookings",
};
window.give(JSON.parse(decodeURIComponent(location.hash.slice(1))));
"""


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    module = MODULE.read_bytes()
    integrity = "sha256-" + base64.b64encode(hashlib.sha256(module).digest()).decode()
    page = PAGE.replace("{integrity}", integrity).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            origin = f"http://{self.headers['Host']}"
            path = self.path.split("#")[0].split("?")[0]
            body, kind = {
                "/frame": (page, "text/html; charset=utf-8"),
                "/calendar.js": (module, "text/javascript"),
                "/driver.js": (DRIVER, "text/javascript"),
            }.get(path, (b"", ""))
            if not kind:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            if kind.startswith("text/html"):
                self.send_header("Content-Security-Policy", POLICY.replace("{origin}", origin))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def booking(n: int, starts: dt.datetime, purpose: str) -> dict[str, Any]:
    instrument = "00000000-0000-7000-8000-0000000000i1"
    return {
        "id": f"00000000-0000-7000-8000-00000000b00{n}",
        "hrid": f"BKG-00000{n}",
        "title": purpose,
        "type": "iki/equipment.Booking",
        "revision": 1,
        "project": {"id": "p", "name": "Kinase Atlas", "slug": "kinase-atlas"},
        "values": {
            "purpose": purpose,
            "instrument": instrument,
            "starts_at": starts.isoformat(),
            "ends_at": (starts + dt.timedelta(hours=2)).isoformat(),
        },
        "refs": {
            instrument: {
                "hrid": "INS-000001",
                "title": "Plate reader",
                "type": "iki/equipment.Instrument",
                "archived": False,
                "purged": False,
            }
        },
    }


def given(items: list[dict[str, Any]], **state: Any) -> dict[str, Any]:
    return {
        "items": items,
        "more": False,
        "limited": False,
        "loading": False,
        "error": None,
        "refinement": None,
        **state,
    }


def sent(page: Page) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = page.evaluate("window.sent")
    return found


def test_a_month_of_bookings_by_day_asking_only_for_what_it_shows(
    browser: Browser, frame_server: str
) -> None:
    now = dt.datetime.now(dt.UTC)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (month + dt.timedelta(days=32)).replace(day=1)
    after = (following + dt.timedelta(days=32)).replace(day=1)
    items = [
        booking(1, month.replace(day=10, hour=9), "Kinase assay"),
        booking(2, month.replace(day=20, hour=14), "Thermal shift"),
    ]
    context = browser.new_context(locale="en-GB", timezone_id="UTC")
    page = context.new_page()
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{frame_server}/frame#{urllib.parse.quote(json.dumps(given(items)))}")

    # It asks for this month first: it can only narrow what its place shows.
    def months(start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
        iso = "%Y-%m-%dT%H:%M:%S.000Z"
        return {
            "type": "iki-refine",
            "detail": {
                "where": {
                    "all": [
                        # Every booking on the month's days, from one ending in it to
                        # one starting in it.
                        {"field": "starts_at", "op": "lt", "value": end.strftime(iso)},
                        {"field": "ends_at", "op": "gt", "value": start.strftime(iso)},
                        ACTIVE,
                    ]
                },
                "sort": {"field": "starts_at", "descending": False},
            },
        }

    table = page.get_by_role("table", name=f"Bookings: {month.strftime('%B %Y')}")
    table.wait_for()
    assert sent(page) == [months(month, following)]
    page.get_by_role("status").get_by_text(f"2 bookings in {month.strftime('%B %Y')}").wait_for()
    assert page.get_by_role("columnheader").all_inner_texts() == [
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun",
    ]
    assay = page.get_by_role("button", name="Open BKG-000001: 09:00–11:00 Kinase assay")
    assay.wait_for()
    assert page.get_by_role("cell").filter(has=assay).inner_text().startswith("10")
    violations = check(page)
    assert violations == [], violations

    # Opening one is Iki's to do, by its HRID.
    assay.click()
    assert sent(page)[-1] == {"type": "iki-open", "detail": {"entity": "BKG-000001"}}
    # The next month is another refinement; the rest of a month, more of it.
    page.get_by_role("button", name="Next month").click()
    assert sent(page)[-1] == months(following, after)
    page.evaluate("(e) => window.give(e)", given(items, more=True))
    assert sent(page)[-1] == {"type": "iki-more", "detail": None}
    # At its limit, it says it shows only the first it may.
    page.evaluate("(e) => window.give(e)", given(items, limited=True))
    page.get_by_role("status").get_by_text("the first 2 it can show").wait_for()
    # What its search was refused for, it says.
    page.evaluate("(e) => window.give(e)", given([], error="The search was refused."))
    page.get_by_role("status").get_by_text("The search was refused.").wait_for()

    assert errors == []
    assert page.evaluate("window.refused") == []
    # Only the frame and its two scripts: it reaches nothing.
    assert sorted({u.split("#")[0] for u in requests}) == [
        f"{frame_server}/calendar.js",
        f"{frame_server}/driver.js",
        f"{frame_server}/frame",
    ]
    context.close()


#: What a calendar asks for until it's asked to show cancelled bookings too.
ACTIVE = {"field": "cancelled", "op": "eq", "value": False}
ANALYST = "00000000-0000-7000-8000-0000000000a1"
REQUEST = "00000000-0000-7000-8000-0000000000r1"
UNREADABLE = "00000000-0000-7000-8000-0000000000r2"
FOR_THE_ANALYST = ", for Priya Shah"
FOR_THE_REQUEST = " · REQ-000004 16S rRNA V4 amplicon panel"


@pytest.mark.parametrize(
    ("element", "slot", "label", "names"),
    [
        ("equipment-booking-calendar", "entity.tab", "Bookings", True),
        ("equipment-booking-calendar", "person.home", "Your bookings", False),
        ("equipment-bookings-made", "person.home", "Bookings you made for others", True),
    ],
    ids=["instrument", "your-bookings", "made-for-others"],
)
def test_each_booking_says_whom_and_what_its_for(
    browser: Browser, frame_server: str, element: str, slot: str, label: str, names: bool
) -> None:
    """Whom it's for, except on your own home, where every booking is yours; and the
    record it's for, only as its reader may read it."""
    month = dt.datetime.now(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    run = booking(1, month.replace(day=8, hour=9), "16S amplicon run")
    run["values"] |= {"booked_by": ANALYST, "work": REQUEST}
    run["refs"][REQUEST] = {
        "hrid": "REQ-000004",
        "title": "16S rRNA V4 amplicon panel",
        "type": "iki/requests.PlainRequest",
        "archived": False,
        "purged": False,
    }
    run["people"] = {ANALYST: {"name": "Priya Shah"}}
    # For a record its reader can't read: the server gives no ref, so nothing's said of it.
    qc = booking(2, month.replace(day=9, hour=14), "Library QC")
    qc["values"] |= {"work": UNREADABLE}
    qc["people"] = {}
    context = browser.new_context(locale="en-GB", timezone_id="UTC")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    query = urllib.parse.urlencode({"element": element, "slot": slot, "label": label})
    page.goto(f"{frame_server}/frame?{query}#{urllib.parse.quote(json.dumps(given([run, qc])))}")

    page.get_by_role("table", name=f"{label}: {month.strftime('%B %Y')}").wait_for()
    whom = FOR_THE_ANALYST if names else ""
    text = f"09:00–11:00 16S amplicon run (Plate reader){whom}{FOR_THE_REQUEST}"
    opens = page.get_by_role("button", name=f"Open BKG-000001: {text}", exact=True)
    opens.wait_for()
    assert opens.inner_text() == text
    qc_text = "14:00–16:00 Library QC (Plate reader)"
    assert page.get_by_role("button", name=f"Open BKG-000002: {qc_text}", exact=True).is_visible()
    assert page.get_by_text("Priya Shah").count() == (1 if names else 0)
    violations = check(page)
    assert violations == [], violations
    opens.click()
    assert sent(page)[-1] == {"type": "iki-open", "detail": {"entity": "BKG-000001"}}
    assert errors == []
    assert page.evaluate("window.refused") == []
    context.close()


def test_cancelled_bookings_are_left_out_until_asked_for_then_marked_cancelled(
    browser: Browser, frame_server: str
) -> None:
    """As a resource calendar drops a cancelled meeting: a cancelled booking holds no time,
    so the calendar asks for active ones; asked, it shows the rest struck through and
    labelled Cancelled, in words as well as by its look."""
    month = dt.datetime.now(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    run = booking(1, month.replace(day=8, hour=9), "16S amplicon run")
    off = booking(2, month.replace(day=8, hour=14), "Shotgun metagenomics run")
    off["values"]["cancelled"] = True
    context = browser.new_context(locale="en-GB", timezone_id="UTC")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # What a place gives before the calendar's month narrows it may hold a cancelled one.
    page.goto(f"{frame_server}/frame#{urllib.parse.quote(json.dumps(given([run, off])))}")
    page.get_by_role("table", name=f"Bookings: {month.strftime('%B %Y')}").wait_for()
    assert ACTIVE in sent(page)[-1]["detail"]["where"]["all"]
    shown = page.get_by_role("checkbox", name="Show cancelled")
    assert not shown.is_checked()
    page.get_by_role("status").get_by_text(f"1 booking in {month.strftime('%B %Y')}").wait_for()
    assert page.get_by_role("button", name="Open BKG-000002", exact=False).count() == 0

    shown.check()
    assert ACTIVE not in sent(page)[-1]["detail"]["where"]["all"]
    page.evaluate("(e) => window.give(e)", given([run, off]))
    text = "14:00–16:00 Shotgun metagenomics run (Plate reader)"
    cancelled = page.get_by_role("button", name=f"Open BKG-000002: Cancelled: {text}", exact=True)
    cancelled.wait_for()
    assert cancelled.inner_text() == f"Cancelled {text}"
    assert cancelled.locator("s").inner_text() == text
    status = page.get_by_role("status")
    status.get_by_text(f"1 booking in {month.strftime('%B %Y')}, 1 cancelled").wait_for()
    violations = check(page)
    assert violations == [], violations
    cancelled.click()
    assert sent(page)[-1] == {"type": "iki-open", "detail": {"entity": "BKG-000002"}}

    shown.uncheck()
    assert ACTIVE in sent(page)[-1]["detail"]["where"]["all"]
    assert cancelled.count() == 0
    assert errors == []
    assert page.evaluate("window.refused") == []
    context.close()


def check(page: Page) -> list[Any]:
    """What axe finds against WCAG 2.2 AA, in the component's own document."""
    page.evaluate(AXE.read_text())
    found: list[Any] = page.evaluate(RUN_AXE, WCAG_22_AA)
    return found

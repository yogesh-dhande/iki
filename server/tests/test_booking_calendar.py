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
await customElements.whenDefined("equipment-booking-calendar");
const element = document.createElement("equipment-booking-calendar");
for (const name of ["iki-refine", "iki-more", "iki-open", "iki-select"]) {
  element.addEventListener(name, (e) => window.sent.push({ type: name, detail: e.detail }));
}
document.querySelector("main").append(element);
window.give = (entities) => { element.entities = entities; };
element.place = { slot: "entity.tab", label: "Bookings" };
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
            path = self.path.split("#")[0]
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
                        {"field": "starts_at", "op": "gte", "value": start.strftime(iso)},
                        {"field": "starts_at", "op": "lt", "value": end.strftime(iso)},
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


def check(page: Page) -> list[Any]:
    """What axe finds against WCAG 2.2 AA, in the component's own document."""
    page.evaluate(AXE.read_text())
    found: list[Any] = page.evaluate(RUN_AXE, WCAG_22_AA)
    return found

# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The oligo plugin's components, in each browser, under the policy Iki's component frames
have (ADR 0084): the Chemistry tab draws an oligonucleotide's nucleotides from the
positions its actions computed, and the Pairing tab a duplex's strands antiparallel. Each
is named for screen readers as it's drawn for the eye, and passes axe's WCAG 2.2 AA
checks in light and dark.

The frame is served here, not by Iki, so it needs no plugin installed: its policy is the
component frame's (iki_server.components.views.frame), and it runs Iki's hardening script
first and the module by its integrity, as Iki's frame does."""

import base64
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
from test_browser import (  # noqa: F401 - fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    browser_name,
    playwright,
    watch,
)

from iki_server.web import frames

# Browser tests: CI runs them once, where it installs the browsers (see ci.yml's matrix).
pytestmark = pytest.mark.built_web_app

COMPONENTS = Path(__file__).resolve().parents[2] / "plugins" / "oligo" / "components"
ELEMENTS = {"chemistry.js": "oligo-chemistry", "duplex.js": "oligo-duplex"}
#: What the frame's policy allows (iki_server.components.views.frame), with the test's
#: own driver in place of Iki's bridge.
POLICY = (
    "default-src 'none'; script-src " + frames.HARDEN_SOURCE + " {origin}/{module} "
    "{origin}/driver.js; style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; media-src data: blob:; worker-src blob:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'; object-src 'none'; sandbox allow-scripts"
)
PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
    # As Iki's frame has it (iki_server.components.views), so dark mode is dark.
    '<meta name="color-scheme" content="light dark"><title>Oligo</title>\n'
    + frames.HARDEN_TAG
    + '<script type="module" src="/{module}" integrity="{integrity}" '
    'crossorigin="anonymous"></script>\n'
    '<script type="module" src="/driver.js?{module}" crossorigin="anonymous"></script>\n'
    "</head><body></body></html>\n"
)
#: Sets the element's entity as the bridge does, and notes any request the policy refused.
DRIVER = b"""
window.refused = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.refused.push(`${e.violatedDirective} ${e.blockedURI}`);
});
const name = {"chemistry.js": "oligo-chemistry", "duplex.js": "oligo-duplex"}[
  new URL(import.meta.url).search.slice(1)
];
await customElements.whenDefined(name);
const element = document.createElement(name);
element.entity = JSON.parse(decodeURIComponent(location.hash.slice(1)));
document.body.append(element);
"""


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    modules = {name: (COMPONENTS / name).read_bytes() for name in ELEMENTS}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            origin = f"http://{self.headers['Host']}"
            path, _, query = self.path.split("#")[0].partition("?")
            name = path.removeprefix("/frame/")
            if path.startswith("/frame/") and name in modules:
                digest = hashlib.sha256(modules[name]).digest()
                integrity = "sha256-" + base64.b64encode(digest).decode()
                body = PAGE.replace("{module}", name).replace("{integrity}", integrity).encode()
                kind = "text/html; charset=utf-8"
            elif path.removeprefix("/") in modules:
                body, kind = modules[path.removeprefix("/")], "text/javascript"
            elif path == "/driver.js" and query in modules:
                body, kind = DRIVER, "text/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # A sandboxed frame is no one's origin: its modules come to it cross-origin.
            self.send_header("Access-Control-Allow-Origin", "*")
            if kind.startswith("text/html"):
                policy = POLICY.replace("{origin}", origin).replace("{module}", name)
                self.send_header("Content-Security-Policy", policy)
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


def _brief(symbol: str, name: str, *, modified: bool) -> dict[str, Any]:
    return {"symbol": symbol, "name": name, "modified": modified}


MOE = _brief("MOE", "2′-O-Methoxyethylribose", modified=True)
DEOXY = _brief("dR", "2′-Deoxyribose", modified=False)
PS = _brief("sP", "Phosphorothioate", modified=True)
#: A 5′-6-FAM, MOE-wing gapmer's first three nucleotides, as the plugin's actions compute
#: its positions.
GAPMER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "hrid": "OLI-000001",
    "type": "iki/oligo.Oligonucleotide",
    "values": {
        "name": "a gapmer",
        "positions": {
            "nucleotides": [
                {
                    "position": 1,
                    "base": "C",
                    "sugar": MOE,
                    "nucleobase": _brief("5meC", "5-Methylcytosine", modified=True),
                    "linker": PS,
                    "others": [],
                },
                {
                    "position": 2,
                    "base": "A",
                    "sugar": MOE,
                    "nucleobase": _brief("A", "Adenine", modified=False),
                    "linker": PS,
                    "others": [],
                },
                {
                    "position": 3,
                    "base": "G",
                    "sugar": DEOXY,
                    "nucleobase": _brief("G", "Guanine", modified=False),
                    "linker": None,
                    "others": [],
                },
            ],
            "ends": {"5′": [_brief("5FAM", "6-FAM", modified=True)]},
        },
    },
}
#: Patisiran's strands, as register_duplex finds them pairing.
PATISIRAN = {
    "id": "00000000-0000-0000-0000-000000000002",
    "hrid": "DUP-000001",
    "type": "iki/oligo.Duplex",
    "values": {
        "duplex_length": 19,
        "mismatches": 0,
        "pairing": {
            "length": 19,
            "mismatches": 0,
            "sense": {"bases": "GUAACCAAGAGUAUUCCAUTT", "from": 1, "to": 19},
            "antisense": {"bases": "AUGGAAUACUCUUGGUUACTT", "from": 1, "to": 19},
            "sense_overhang_5": 0,
            "sense_overhang_3": 2,
            "antisense_overhang_5": 0,
            "antisense_overhang_3": 2,
            "pairs": [{"sense": i, "antisense": 20 - i, "paired": True} for i in range(1, 20)],
        },
    },
}


#: Each page's uncaught errors. Not its console's: Firefox logs its own favicon request,
#: which the frame's policy blocks, as an error.
ERRORS: dict[Page, list[str]] = {}


def _open(browser: Browser, frame_server: str, module: str, entity: object, **options: Any) -> Page:
    context = browser.new_context(**options)
    page = watch(context.new_page())
    ERRORS[page] = []
    page.on("pageerror", lambda error: ERRORS[page].append(str(error)))
    page.goto(f"{frame_server}/frame/{module}#{urllib.parse.quote(json.dumps(entity))}")
    return page


def test_the_chemistry_tab_names_each_nucleotide(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, "chemistry.js", GAPMER)
    strand = page.get_by_role("list", name="Nucleotides, 5′ to 3′")
    strand.wait_for(timeout=30_000)
    items = strand.get_by_role("listitem")
    assert items.count() == 4
    assert items.nth(0).get_attribute("aria-label") == "5′ end: 6-FAM"
    first = page.get_by_role(
        "listitem",
        name="Position 1; 5-Methylcytosine, modified on 2′-O-Methoxyethylribose, modified; "
        "then Phosphorothioate, modified",
    )
    assert first.count() == 1
    assert page.get_by_role("listitem", name="Position 3; Guanine on 2′-Deoxyribose").count() == 1
    page.get_by_role("group").get_by_text("Each position").click()
    rows = page.get_by_role("row")
    assert rows.count() == 4
    assert page.get_by_role("rowheader", name="2").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_the_pairing_tab_says_what_it_draws(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, "duplex.js", PATISIRAN)
    heading = page.get_by_role("heading", name="Pairing")
    heading.wait_for(timeout=30_000)
    assert page.get_by_text(
        "19 base pairs, no mismatches. Sense strand: 0-nucleotide 5′ and 2-nucleotide 3′ "
        "overhang. Antisense strand: 0-nucleotide 5′ and 2-nucleotide 3′ overhang."
    ).is_visible()
    assert page.get_by_role("definition").filter(has_text="sense 1–19, antisense 1–19").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_a_duplex_whose_strands_dont_pair_says_so(browser: Browser, frame_server: str) -> None:
    entity = {"hrid": "DUP-000002", "values": {"pairing": {"length": 0, "pairs": []}}}
    page = _open(browser, frame_server, "duplex.js", entity)
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert "don't pair" in (status.text_content() or "")
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("module", "entity"), [("chemistry.js", GAPMER), ("duplex.js", PATISIRAN)])
def test_they_pass_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: str, module: str, entity: object
) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = _open(browser, frame_server, module, entity, color_scheme=scheme, bypass_csp=True)
    page.get_by_role("heading").first.wait_for(timeout=30_000)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

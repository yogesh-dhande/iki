# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The plasmid plugin's Map tab, in each browser, under the policy Iki's component frames
have (ADR 0084): a plasmid drawn round a circle from the parts its annotation found, said
in words for screen readers and tabulated beneath, so nothing is told by colour alone. It
passes axe's WCAG 2.2 AA checks in light and dark.

The frame is served here, not by Iki, so it needs no plugin installed: its policy is the
component frame's (iki_server.components.views.frame), and it runs Iki's hardening script
first and the module by its integrity, as Iki's frame does.
"""

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

COMPONENTS = Path(__file__).resolve().parents[2] / "plugins" / "plasmid" / "components"
MODULE = "plasmid-map.js"
ELEMENT = "plasmid-map"
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
    '<meta name="color-scheme" content="light dark"><title>Plasmid</title>\n'
    + frames.HARDEN_TAG
    + '<script type="module" src="/{module}" integrity="{integrity}" '
    'crossorigin="anonymous"></script>\n'
    '<script type="module" src="/driver.js" crossorigin="anonymous"></script>\n'
    "</head><body></body></html>\n"
)
#: Sets the element's entity as the bridge does, and notes any request the policy refused.
DRIVER = b"""
window.refused = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.refused.push(`${e.violatedDirective} ${e.blockedURI}`);
});
await customElements.whenDefined("plasmid-map");
const element = document.createElement("plasmid-map");
element.entity = JSON.parse(decodeURIComponent(location.hash.slice(1)));
document.body.append(element);
"""


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    module = (COMPONENTS / MODULE).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            origin = f"http://{self.headers['Host']}"
            path = self.path.split("#")[0].split("?")[0]
            if path == f"/frame/{MODULE}":
                digest = hashlib.sha256(module).digest()
                integrity = "sha256-" + base64.b64encode(digest).decode()
                body = PAGE.replace("{module}", MODULE).replace("{integrity}", integrity).encode()
                kind = "text/html; charset=utf-8"
            elif path == f"/{MODULE}":
                body, kind = module, "text/javascript"
            elif path == "/driver.js":
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
                self.send_header(
                    "Content-Security-Policy",
                    POLICY.replace("{origin}", origin).replace("{module}", MODULE),
                )
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


def _feature(
    kind: str, name: str, start: int, end: int, strand: str, role: str, identity: str, label: str
) -> dict[str, Any]:
    return {
        "type": kind,
        "name": name,
        "start": start,
        "end": end,
        "strand": strand,
        "qualifiers": {"label": label, "role": role, "identity": identity},
    }


#: pcDNA3.1(+) (INSDC MN996867.1, 5,428 bp) as the plugin annotates it: ten parts of the
#: library, two of them overlapping (the SV40 origin lies inside the SV40 early promoter),
#: and two on the reverse strand. ``server/tests/test_plasmid_annotation.py`` checks these
#: coordinates against the record.
PCDNA = {
    "id": "00000000-0000-0000-0000-000000000001",
    "hrid": "PLS-000001",
    "type": "iki/plasmid.Plasmid",
    "values": {
        "name": "pcDNA3.1(+)",
        "length": 5428,
        "annotated_with": "iki/plasmid features 1.0 (34 parts), at least 90% identity",
        "annotated_features": [
            _feature(
                "promoter",
                "CMV promoter",
                302,
                818,
                "forward",
                "promoter",
                "100.0%",
                "CMV promoter and enhancer (human cytomegalovirus immediate early)",
            ),
            _feature(
                "promoter", "T7 promoter", 863, 881, "forward", "promoter", "100.0%", "T7 promoter"
            ),
            _feature("rep_origin", "f1 ori", 1298, 1726, "forward", "origin", "94.1%", "f1 ori"),
            _feature(
                "promoter",
                "SV40 early promoter",
                1731,
                2012,
                "forward",
                "promoter",
                "99.7%",
                "SV40 early promoter",
            ),
            _feature(
                "rep_origin", "SV40 ori", 1969, 2044, "forward", "origin", "98.7%", "SV40 ori"
            ),
            _feature(
                "CDS",
                "NeoR/KanR",
                2136,
                2930,
                "forward",
                "selection",
                "100.0%",
                "NeoR/KanR (nptII, neomycin phosphotransferase II)",
            ),
            _feature(
                "polyA_signal",
                "SV40 poly(A) signal",
                3104,
                3178,
                "forward",
                "terminator",
                "100.0%",
                "SV40 poly(A) signal",
            ),
            _feature(
                "promoter",
                "lac promoter",
                3317,
                3400,
                "reverse",
                "promoter",
                "100.0%",
                "lac promoter",
            ),
            _feature(
                "rep_origin",
                "ori",
                3676,
                4261,
                "reverse",
                "origin",
                "99.3%",
                "ori (ColE1/pMB1/pBR322/pUC origin of replication)",
            ),
            _feature(
                "CDS",
                "AmpR",
                4432,
                5292,
                "reverse",
                "resistance",
                "100.0%",
                "AmpR (bla, beta-lactamase)",
            ),
        ],
    },
}
#: A part that wraps the origin, as one of a circular plasmid's may (ADR 0138).
AROUND = {
    "hrid": "PLS-000002",
    "values": {
        "name": "a construct",
        "length": 4000,
        "annotated_features": [
            _feature("CDS", "AmpR", 3900, 120, "forward", "resistance", "99.5%", "AmpR")
        ],
    },
}

#: Each page's uncaught errors. Not its console's: Firefox logs its own favicon request,
#: which the frame's policy blocks, as an error.
ERRORS: dict[Page, list[str]] = {}


def _open(browser: Browser, frame_server: str, entity: object, **options: Any) -> Page:
    context = browser.new_context(**options)
    page = watch(context.new_page())
    ERRORS[page] = []
    page.on("pageerror", lambda error: ERRORS[page].append(str(error)))
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    page.get_by_role("heading", name="Map").wait_for(timeout=30_000)
    return page


def test_the_map_says_in_words_what_it_draws(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, PCDNA)
    assert page.get_by_text(
        "5,428 base pairs, with 10 parts of the library found along it: CMV promoter and "
        "enhancer (human cytomegalovirus immediate early), 302 to 818 on the forward strand"
    ).is_visible()
    # The drawing itself is hidden from screen readers, since the words and the table say it.
    assert page.locator("svg[aria-hidden=true]").count() == 1
    assert page.get_by_text(
        "iki/plasmid features 1.0 (34 parts), at least 90% identity"
    ).is_visible()
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_every_part_is_in_the_table_with_its_coordinates(
    browser: Browser, frame_server: str
) -> None:
    page = _open(browser, frame_server, PCDNA)
    rows = page.get_by_role("row")
    assert rows.count() == 11  # its header and the ten parts
    assert page.get_by_role("rowheader", name="AmpR (bla, beta-lactamase)").count() == 1
    beta = rows.filter(has_text="beta-lactamase")
    cells = beta.get_by_role("cell")
    assert [cells.nth(i).text_content() for i in range(5)] == [
        "4,432–5,292",
        "861 bp",
        "reverse",
        "Bacterial resistance",
        "100.0%",
    ]
    # Overlapping parts are both there: the SV40 origin lies inside the SV40 promoter.
    assert page.get_by_role("rowheader", name="SV40 ori").count() == 1
    assert page.get_by_role("rowheader", name="SV40 early promoter").count() == 1
    page.context.close()


def test_a_part_that_wraps_the_origin_gives_both_stretches(
    browser: Browser, frame_server: str
) -> None:
    page = _open(browser, frame_server, AROUND)
    assert page.get_by_role("cell", name="3,900–4,000, 1–120").count() == 1
    assert page.get_by_role("cell", name="221 bp").count() == 1
    page.context.close()


def test_a_plasmid_not_annotated_yet_says_so_rather_than_drawing_nothing(
    browser: Browser, frame_server: str
) -> None:
    context = browser.new_context()
    page = watch(context.new_page())
    entity = {"hrid": "PLS-000003", "values": {}}
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert "drawn once the plasmid is annotated" in (status.text_content() or "")
    context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_passes_wcag_22_aa(browser: Browser, frame_server: str, scheme: str) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = _open(browser, frame_server, PCDNA, color_scheme=scheme, bypass_csp=True)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

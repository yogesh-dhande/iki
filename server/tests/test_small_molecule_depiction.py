# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The small_molecule plugin's depiction component, in each browser, under the policy
Iki's component frames have (ADR 0084): RDKit.js, bundled into its module, draws a
compound from its SMILES, with no request leaving the frame.

The frame is served here, not by Iki, so it needs no plugin installed: its policy is
the component frame's (iki_server.components.views.frame), and it runs Iki's hardening
script first and the module by its integrity, as Iki's frame does."""

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
from playwright.sync_api import Browser
from test_browser import browser, browser_name, playwright  # noqa: F401 - fixtures

from iki_server.web import frames

# Browser tests: CI runs them once, where it installs the browsers (see ci.yml's matrix).
pytestmark = pytest.mark.built_web_app

MODULE = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "small_molecule"
    / "components"
    / "depiction.js"
)
#: What the frame's policy allows (iki_server.components.views.frame), with the test's
#: own driver in place of Iki's bridge.
POLICY = (
    "default-src 'none'; script-src " + frames.HARDEN_SOURCE + " {origin}/depiction.js "
    "{origin}/driver.js 'wasm-unsafe-eval'; style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; media-src data: blob:; worker-src blob:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'; object-src 'none'; sandbox allow-scripts"
)
PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Depiction</title>\n'
    + frames.HARDEN_TAG
    + '<script type="module" src="/depiction.js" integrity="{integrity}" '
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
await customElements.whenDefined("small-molecule-depiction");
const element = document.createElement("small-molecule-depiction");
element.entity = JSON.parse(decodeURIComponent(location.hash.slice(1)));
document.body.append(element);
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
                "/depiction.js": (module, "text/javascript"),
                "/driver.js": (DRIVER, "text/javascript"),
            }.get(path, (b"", ""))
            if not kind:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # A sandboxed frame is no one's origin: its modules come to it cross-origin.
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


def test_draws_a_compound_from_its_smiles_reaching_nothing(
    browser: Browser, frame_server: str
) -> None:
    # A real browser test, each browser in turn: it loads RDKit's WebAssembly (3 MiB).
    entity = {
        "id": "00000000-0000-0000-0000-000000000001",
        "hrid": "CPD-000001",
        "type": "iki/small_molecule.Compound",
        "values": {"smiles": "CC(=O)Oc1ccccc1C(=O)O", "name": "aspirin"},
    }
    context = browser.new_context()
    page = context.new_page()
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{frame_server}/frame#{urllib.parse.quote(json.dumps(entity))}")
    image = page.get_by_role("img", name="The structure of aspirin: CC(=O)Oc1ccccc1C(=O)O")
    image.wait_for(timeout=60_000)
    page.wait_for_function(
        "() => { const i = document.querySelector('small-molecule-depiction')"
        ".shadowRoot.querySelector('img'); return i.complete && i.naturalWidth > 0; }",
        timeout=30_000,
    )
    assert errors == []
    assert page.evaluate("window.refused") == []
    # Only the frame and its two scripts: RDKit's WebAssembly came in the module.
    assert sorted({u.split("#")[0] for u in requests if not u.startswith("blob:")}) == [
        f"{frame_server}/depiction.js",
        f"{frame_server}/driver.js",
        f"{frame_server}/frame",
    ]
    context.close()


def test_a_structure_rdkit_cant_read_says_so(browser: Browser, frame_server: str) -> None:
    entity = {"hrid": "CPD-000002", "values": {"smiles": "C1CC"}}
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{frame_server}/frame#{urllib.parse.quote(json.dumps(entity))}")
    alert = page.get_by_role("alert")
    alert.wait_for(timeout=60_000)
    assert alert.text_content() == "CPD-000002 can't be drawn: RDKit can't read its SMILES."
    context.close()

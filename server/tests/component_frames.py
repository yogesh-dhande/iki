"""Plugins' components in a frame served as Iki serves theirs (ADR 0084): under the
component frame's policy (iki_server.components.views.frame), running Iki's hardening
script first and the module by its integrity. The frame is served here, not by Iki, so a
component's test needs no plugin installed; a driver of the test's own sets the element's
`entity` as Iki's bridge does, notes any request the policy refused, and keeps the actions
it asks to run (`window.ran`); a test sets its `place` itself, as the page would."""

import base64
import hashlib
import json
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page
from test_browser import watch

from iki_server.web import frames

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"
#: What the frame's policy allows, with the test's own driver in place of Iki's bridge.
POLICY = (
    "default-src 'none'; script-src " + frames.HARDEN_SOURCE + " {origin}/{module} "
    "{origin}/driver.js; style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; media-src data: blob:; worker-src blob:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'; object-src 'none'; sandbox allow-scripts"
)
PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
    # As Iki's frame has it (iki_server.components.views), so dark mode is dark.
    '<meta name="color-scheme" content="light dark"><title>Component</title>\n'
    + frames.HARDEN_TAG
    + '<script type="module" src="/{module}" integrity="{integrity}" '
    'crossorigin="anonymous"></script>\n'
    '<script type="module" src="/driver.js?{module}" crossorigin="anonymous"></script>\n'
    "</head><body></body></html>\n"
)
DRIVER = """
window.refused = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.refused.push(`${e.violatedDirective} ${e.blockedURI}`);
});
const name = %s[new URL(import.meta.url).search.slice(1)];
await customElements.whenDefined(name);
const element = document.createElement(name);
element.entity = JSON.parse(decodeURIComponent(location.hash.slice(1)));
// The actions it asks to run, as Iki's bridge would pass them to the page (ADR 0169).
window.ran = [];
element.addEventListener("iki-run", (e) => window.ran.push(e.detail?.action));
document.body.append(element);
"""
#: Each page's uncaught errors. Not its console's: Firefox logs its own favicon request,
#: which the frame's policy blocks, as an error.
ERRORS: dict[Page, list[str]] = {}


@contextmanager
def serving(directory: Path, elements: dict[str, str]) -> Iterator[str]:
    """Serves each module in ``directory`` that ``elements`` names, with its element's
    name, at ``/frame/<module>``; yields the server's origin."""
    modules = {name: (directory / name).read_bytes() for name in elements}
    driver = (DRIVER % json.dumps(elements)).encode()

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
                body, kind = driver, "text/javascript"
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
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def open_frame(browser: Browser, origin: str, module: str, entity: object, **options: Any) -> Page:
    """A new page with ``module``'s element showing ``entity``."""
    context = browser.new_context(**options)
    page = watch(context.new_page())
    ERRORS[page] = []
    page.on("pageerror", lambda error: ERRORS[page].append(str(error)))
    page.goto(f"{origin}/frame/{module}#{urllib.parse.quote(json.dumps(entity))}")
    return page


def place(page: Page, element: str, value: object) -> None:
    """Sets the element's ``place``, as Iki's bridge does once the page says where it is."""
    page.wait_for_function(f"document.querySelector({json.dumps(element)}) !== null")
    page.evaluate(
        "([name, value]) => { document.querySelector(name).place = value; }", [element, value]
    )

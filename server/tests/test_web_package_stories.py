# ruff: noqa: F811 - browser and playwright are shared pytest fixtures.
"""An external page loads the published entries and uses them without an Iki server."""

from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Literal

import pytest
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401

pytestmark = pytest.mark.built_web_app
PACKAGES = Path(__file__).resolve().parents[2] / "web" / "packages"
STORIES = sorted(
    f"{path.parents[1].name}/{path.stem}" for path in PACKAGES.glob("*/stories/*.html")
)


@pytest.fixture(scope="module")
def gallery_url() -> Iterator[str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(PACKAGES))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("story", STORIES)
def test_standalone_story(
    browser: Browser,
    gallery_url: str,
    theme: Literal["light", "dark"],
    story: str,
) -> None:
    context = browser.new_context(
        color_scheme=theme,
        locale="en-GB",
        timezone_id="UTC",
        viewport={"width": 390, "height": 844},
    )
    try:
        page = context.new_page()
        errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: requests.append(req.url))
        page.on("response", lambda res: errors.append(res.url) if res.status >= 400 else None)
        package, component = story.split("/")
        page.goto(f"{gallery_url}/{package}/stories/{component}.html")

        if component == "button":
            page.get_by_role("button", name="Save").press("Enter")
            expect(page.get_by_role("status")).to_have_text("Saved.")
            page.get_by_role("button", name="Cancel").click()
            expect(page.get_by_role("status")).to_have_text("Canceled.")
            expect(page.get_by_role("button", name="Archive")).to_be_disabled()
        elif component == "booking-calendar":
            page.get_by_role("button", name="Open demo-1:", exact=False).click()
            expect(page.get_by_text("Selected demo-1", exact=True)).to_be_visible()
            page.get_by_role("button", name="Next month").click()
            expect(page.get_by_role("table", name="Protocol room: April 2026")).to_be_visible()
        elif component == "tokens":
            expect(page.get_by_role("heading", name="Design tokens")).to_be_visible()
            assert page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
            ) == ("#141517" if theme == "dark" else "#ffffff")
            assert page.evaluate("""async () => {
                const fonts = await document.fonts.load('13px "Inter Variable"');
                return fonts.length > 0 && fonts.every(font => font.status === 'loaded');
            }""")

        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.add_script_tag(path=AXE)
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        assert not errors, errors
        assert all(url.startswith(f"{gallery_url}/") for url in requests), requests
        if component in {"booking-calendar", "button", "tokens", "plasmid-map"}:
            assert not any(url.endswith(".wasm") for url in requests), requests
    finally:
        context.close()

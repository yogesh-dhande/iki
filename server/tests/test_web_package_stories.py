# ruff: noqa: F811 - browser and playwright are shared pytest fixtures.
"""An external page loads the published entries and uses them without an Iki server."""

from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Literal

import pytest
from check_test_prerequisites import populated, story_assets
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401

pytestmark = pytest.mark.built_web_app
PACKAGES = Path(__file__).resolve().parents[2] / "web" / "packages"
# The rendering engines have their own readiness, interaction and accessibility tests.
ENGINE_STORIES = {"science/chemical-depiction", "science/structure-viewer"}
STORIES = sorted(
    {f"{path.parents[1].name}/{path.stem}" for path in PACKAGES.glob("*/stories/*.html")}
    - ENGINE_STORIES
)


@pytest.fixture(scope="module")
def gallery_url() -> Iterator[str]:
    story_assets(PACKAGES)
    populated(AXE, "pnpm install --frozen-lockfile")
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
        elif component == "box-map":
            cell = page.get_by_role("gridcell", name="A1, Demo 1 Blank")
            cell.press("ArrowRight")
            next_cell = page.get_by_role("gridcell", name="A2, empty")
            expect(next_cell).to_be_focused()
            next_cell.press("Space")
            expect(next_cell).to_have_attribute("aria-selected", "true")
            expect(page.get_by_role("status")).to_have_text("A2 selected")
        elif component == "sequence-input":
            page.get_by_role("textbox", name="Synthetic sequence").fill("ACG")
            expect(page.get_by_role("status")).to_have_text("3 characters")
        elif component == "quantity-input":
            page.get_by_role("textbox", name="Example volume").fill("1.5")
            page.get_by_role("combobox", name="Unit for Example volume").select_option("__other")
            page.get_by_role("textbox", name="UCUM unit code").fill("L")
            expect(page.get_by_role("status")).to_have_text("1.5 L")
        elif component == "page-header":
            page.get_by_role("button", name="About Review queue").click()
            dialog = page.get_by_role("dialog", name="About Review queue")
            expect(dialog.get_by_role("link", name="Review guide")).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).to_have_count(0)
            page.get_by_role("button", name="Export").press("Enter")
            expect(page.get_by_role("status")).to_have_text("Requested export")
        elif component == "sidebar":
            page.get_by_role("button", name="Guides").press("Enter")
            page.get_by_role("link", name="Importing data").click()
            expect(page.get_by_role("status")).to_have_text("Selected #import")
        elif component == "oligo-chemistry":
            page.get_by_text("Each position", exact=True).press("Enter")
            expect(page.get_by_role("rowheader", name="12", exact=True)).to_be_visible()
        elif component == "oligo-duplex":
            expect(page.get_by_text("sense 1–12, antisense 1–12")).to_be_visible()
            page.get_by_text("Strand details", exact=True).press("Enter")
            expect(page.get_by_text("sense 1–12, antisense 1–12")).to_be_hidden()
        elif component == "complex-composition":
            page.get_by_text("Its 3 bonds", exact=True).press("Enter")
            expect(page.get_by_text("Insulin B chain, copy 1, residue 19")).to_be_visible()
        elif component == "sequence-viewer":
            page.get_by_role("button", name="Show Segment").click()
            expect(page.get_by_text("Showing Segment", exact=True)).to_be_visible()
            expect(page.get_by_role("list", name="Tracks")).to_contain_text("Example score")
        elif component == "plasmid-map":
            page.get_by_role("button", name="Linear").press("Enter")
            expect(page.get_by_role("status")).to_have_text("Showing the linear view")
            expect(page.get_by_role("table", name="Parts found along it")).to_contain_text(
                "350–400, 1–30"
            )
        elif component == "plasmid-thumbnail":
            page.get_by_role("button", name="Open the example map").press("Enter")
            expect(page.get_by_role("status")).to_have_text("Opened the example map")
        elif component == "alignment-viewer":
            grid = page.get_by_role("application", name="Alignment of Synthetic reads")
            grid.press("n")
            expect(page.locator(".alignment-at")).to_contain_text("reference, column 4")
            page.get_by_role("checkbox", name="Select Sample A").check()
            expect(page.locator("output")).to_have_text("Selected: a")
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
        override = "dark" if theme == "light" else "light"
        page.evaluate("(theme) => document.documentElement.dataset.theme = theme", override)
        expect(page.locator("body")).to_have_css(
            "background-color",
            "rgb(20, 21, 23)" if override == "dark" else "rgb(255, 255, 255)",
        )
        if component in {"alignment-viewer", "plasmid-thumbnail", "sequence-viewer"}:
            expect(page.locator("button.button:enabled").first).to_have_css(
                "color", "rgb(232, 233, 235)" if override == "dark" else "rgb(29, 31, 33)"
            )
            assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        assert not errors, errors
        assert all(url.startswith(f"{gallery_url}/") for url in requests), requests
        assert not any(url.endswith(".wasm") for url in requests), requests
    finally:
        context.close()

# ruff: noqa: F811 - pytest injects these fixtures by name.
"""Standalone molecular viewers use their own engine and release their old scene on replacement."""

from typing import Literal

import pytest
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401
from test_web_package_stories import gallery_url  # noqa: F401

pytestmark = pytest.mark.built_web_app


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_molecular_drawing_updates(
    browser: Browser, gallery_url: str, theme: Literal["light", "dark"]
) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=theme)
    try:
        page = context.new_page()
        urls: list[str] = []
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: urls.append(req.url))
        page.on("response", lambda res: errors.append(res.url) if res.status >= 400 else None)
        page.goto(f"{gallery_url}/science/stories/chemical-depiction.html")
        image = page.get_by_role("img", name="The structure of aspirin:", exact=False)
        expect(image).to_be_visible(timeout=60_000)
        page.evaluate("""() => document.addEventListener('depiction-error', e => {
            window.depictionError = e.detail;
        }, {once: true})""")
        page.add_script_tag(path=AXE)
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        page.get_by_label("SMILES").fill("C1CC")
        expect(page.get_by_role("alert")).to_contain_text("RDKit can't read its SMILES")
        assert "RDKit can't read its SMILES" in page.evaluate("window.depictionError")
        page.get_by_label("SMILES").fill("O")
        expect(page.get_by_role("img")).to_have_accessible_name("The structure of compound: O")
        assert not errors, errors
        assert all(url.startswith((f"{gallery_url}/", f"blob:{gallery_url}/")) for url in urls), (
            urls
        )
        assert all("structure-viewer" not in url and not url.endswith(".wasm") for url in urls)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_molstar_scene_and_replacement(
    browser: Browser, gallery_url: str, theme: Literal["light", "dark"]
) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=theme)
    try:
        page = context.new_page()
        urls: list[str] = []
        errors: list[str] = []
        page.on("request", lambda req: urls.append(req.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda res: errors.append(res.url) if res.status >= 400 else None)
        page.goto(f"{gallery_url}/science/stories/structure-viewer.html")
        viewer = page.locator("iki-structure-viewer")
        page.wait_for_function(
            """() => {
            const viewer = document.querySelector('iki-structure-viewer');
            return viewer.getState?.() ||
                viewer.shadowRoot?.textContent.includes('WebGL is unavailable');
        }""",
            timeout=70_000,
        )
        page.add_script_tag(path=AXE)
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        if viewer.get_by_text("WebGL is unavailable", exact=False).is_visible():
            assert browser.browser_type.name == "firefox"
        else:
            assert viewer.evaluate("e => e.getState().camera.position.length") == 3
            assert viewer.evaluate("""async e => {
                await e.setColour(0, '#bb4422');
                e.setHidden(0, true);
                return e.getState().structures[0].hidden;
            }""")
            assert (
                viewer.evaluate("""async e => {
                const ready = new Promise(done =>
                    e.addEventListener('structure-ready', done, {once: true}));
                e.data = {...e.data};
                await ready;
                return e.getState().structures[0];
            }""")
                == {}
            )
            # An empty Mol* viewport puts the project logo back in the tab order.
            viewer.get_by_role("button", name="State Tree").click()
            viewer.get_by_role("button", name="Remove All").click()
            viewer.get_by_role("button", name="State Tree").click()
            logo = viewer.get_by_role("link", name="Mol* project website", exact=True)
            expect(logo).to_be_visible()
            expect(logo).to_have_attribute("href", "https://molstar.org")
            assert logo.evaluate("link => link.tabIndex") == 0
            logo.focus()
            expect(logo).to_be_focused()
            assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
            viewer.evaluate("e => { e.data = null; }")
            expect(viewer.locator("canvas")).to_have_count(0)
        assert not errors, errors
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert all(url.startswith(f"{gallery_url}/") for url in urls), urls
        assert all("chemical-depiction" not in url and not url.endswith(".wasm") for url in urls)
    finally:
        context.close()

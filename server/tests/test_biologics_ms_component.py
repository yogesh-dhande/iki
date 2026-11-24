# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The LC-MS comparison is legible on a phone in both color schemes."""

import runpy

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, serving
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401 - fixtures

PLUGIN = PLUGINS / "biologics_ms"
MODULE = "comparison.js"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_published_trastuzumab_peak_and_theoretical_mass(browser: Browser, scheme: str) -> None:
    compare = runpy.run_path(str(PLUGIN / "scripts/characterize.py"))["mass_rows"]
    report = compare(
        PLUGIN / "tests/data/trastuzumab-intact.tsv",
        {"values": {"molecular_weight": 145197.41}},
        {},
        32,
        "average",
        35.0,
    )
    with serving(PLUGIN / "components", {MODULE: "biologics-ms-comparison"}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {"values": {"comparison": report}},
            color_scheme=scheme,
            viewport={"width": 390, "height": 844},
            bypass_csp=True,
        )
        expect(page.get_by_role("heading", name="G0F/G0F")).to_be_visible()
        expect(page.get_by_text("148,058.4 Da")).to_be_visible()
        expect(page.get_by_text("Within tolerance")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
        assert page.evaluate("window.refused") == []
        assert ERRORS[page] == []
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        page.context.close()

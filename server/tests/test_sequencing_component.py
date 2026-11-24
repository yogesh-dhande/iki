# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The FASTQ report fits a phone in either color scheme."""

import runpy

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, serving
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401 - fixtures

PLUGIN = PLUGINS / "sequencing"
MODULE = "fastq-quality.js"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_public_yeast_fastq_quality_is_readable(browser: Browser, scheme: str) -> None:
    paths = sorted((PLUGIN / "tests/data").glob("*.fastq"))
    statistics = runpy.run_path(str(PLUGIN / "scripts/fastq.py"))["statistics"](*paths)
    report = [
        {"mate": mate, "file": path.name, **values}
        for mate, (path, values) in enumerate(zip(paths, statistics, strict=True), 1)
    ]
    with serving(PLUGIN / "components", {MODULE: "fastq-quality"}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {"values": {"statistics": report}},
            viewport={"width": 390, "height": 844},
            color_scheme=scheme,
            bypass_csp=True,
        )
        expect(page.get_by_role("heading", name="Read 1")).to_be_visible()
        expect(page.get_by_role("heading", name="Read 2")).to_be_visible()
        expect(page.get_by_text("90%", exact=True)).to_be_visible()
        expect(page.get_by_text("14%", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
        assert page.evaluate("window.refused") == []
        assert ERRORS[page] == []
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        page.context.close()

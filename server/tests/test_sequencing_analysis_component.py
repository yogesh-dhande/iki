# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""A sequencing result, what its selection keeps and where every row is, at phone width."""

import csv
import io
import runpy

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, serving
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401 - fixtures

PLUGIN = PLUGINS / "sequencing_analysis"
MODULE = "results.js"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_selected_gene_count_is_visible_and_accessible(browser: Browser, scheme: str) -> None:
    parse = runpy.run_path(str(PLUGIN / "scripts/import_result.py"))["count_rows"]
    with (PLUGIN / "tests/data/GSE176296-counts.tsv").open() as source:
        report = parse(source, "WTJ.1", csv.writer(io.StringIO()))
    with serving(PLUGIN / "components", {MODULE: "sequencing-results"}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {
                "values": {
                    "format": "counts",
                    "summary": report,
                    "pipeline_context": {"pipeline": "featureCounts", "version": "GEO GSE176296"},
                }
            },
            color_scheme=scheme,
            viewport={"width": 390, "height": 844},
            bypass_csp=True,
        )
        expect(page.get_by_text("WTJ.1", exact=True)).to_be_visible()
        expect(page.get_by_text("YDL248W", exact=True)).to_be_visible()
        expect(page.get_by_text("679", exact=True)).to_be_visible()
        expect(page.get_by_text("RNA-seq counts", exact=True)).to_be_visible()
        expect(page.get_by_text(report["projection"], exact=True)).to_be_visible()
        expect(
            page.get_by_text("The import run also keeps these rows as selected.tsv.")
        ).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
        assert page.evaluate("window.refused") == []
        assert ERRORS[page] == []
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        page.context.close()

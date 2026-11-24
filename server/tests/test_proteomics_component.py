# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The proteomics report is usable in both color schemes at 390 px."""

import runpy

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, serving
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401 - fixtures

PLUGIN = PLUGINS / "proteomics"
MODULE = "search-results.js"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_protein_abundance_and_peptide_are_visible(browser: Browser, scheme: str) -> None:
    parse = runpy.run_path(str(PLUGIN / "scripts/import_search.py"))["parsed"]
    with (PLUGIN / "tests/data/PRIDE-16649.mztab").open() as source:
        rows = list(parse("mztab", source))
    with serving(PLUGIN / "components", {MODULE: "proteomics-search-results"}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {
                "values": {
                    "version": "PRIDE 1.0 rc5",
                    "summary": {
                        "counts": {"protein_group": 2, "spectrum_match": 2},
                        "preview": rows,
                        "unmatched_accessions": [],
                    },
                }
            },
            color_scheme=scheme,
            viewport={"width": 390, "height": 844},
            bypass_csp=True,
        )
        expect(page.get_by_text("Protein groups", exact=True)).to_be_visible()
        expect(page.get_by_text("QQVLDR", exact=True)).to_be_visible()
        expect(page.get_by_text("0.853", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
        assert page.evaluate("window.refused") == []
        assert ERRORS[page] == []
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        page.context.close()


def test_preview_shows_target_counts_exclusions_and_calculation_errors(browser: Browser) -> None:
    report = {
        "counts": {"protein_group": 1},
        "excluded_counts": {"contaminant": {"protein_group": 1}},
        "unmatched_accessions": [],
        "preview": [
            {
                "accessions": ["P00330"],
                "quantities": {"Assay A": "NaN", "Assay B": None, "Assay C": 0.0},
            },
            {
                "accessions": ["CON__P12345"],
                "quantities": {},
                "flags": ["Potential contaminant"],
            },
        ],
    }
    with serving(PLUGIN / "components", {MODULE: "proteomics-search-results"}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {"values": {"version": "synthetic", "summary": report}},
            viewport={"width": 390, "height": 844},
            bypass_csp=True,
        )
        for text in (
            "Target protein groups",
            "Contaminant protein groups",
            "Potential contaminant",
            "Calculation error (NaN)",
            "Missing",
            "0",
        ):
            expect(page.get_by_text(text, exact=True)).to_be_visible()
        page.context.close()

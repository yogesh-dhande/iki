# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The LC-MS comparison is legible on a phone in both color schemes."""

import runpy
from pathlib import Path

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
        {"values": {"molecular_weight": 145199.44}},
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
            {
                "values": {
                    "comparison": report,
                    "report_format": "iki_mass_table_1",
                    "source_software": "Deconvolution software 4.1",
                }
            },
            color_scheme=scheme,
            viewport={"width": 390, "height": 844},
            bypass_csp=True,
        )
        expect(page.get_by_role("heading", name="G0F/G0F")).to_be_visible()
        expect(
            page.get_by_text("Iki mass table 1 · from Deconvolution software 4.1")
        ).to_be_visible()
        expect(page.get_by_text("148,058.4 Da")).to_be_visible()
        expect(page.get_by_text("Within tolerance")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
        assert page.evaluate("window.refused") == []
        assert ERRORS[page] == []
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        page.context.close()


def test_peptide_site_labels_distinguish_the_terminus_from_its_first_residue(
    browser: Browser, tmp_path: Path
) -> None:
    compare = runpy.run_path(str(PLUGIN / "scripts/characterize.py"))["peptide_rows"]
    source = tmp_path / "synthetic.tsv"
    source.write_text(
        "chain\tstart\tsequence\tmodifications\nheavy\t2\tKAP\t0:UNIMOD:1;1:UNIMOD:1\n"
    )
    report = compare(source, {"heavy": "AKAPG"})
    with serving(PLUGIN / "components", {MODULE: "biologics-ms-comparison"}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {"values": {"comparison": report}},
            viewport={"width": 390, "height": 844},
            bypass_csp=True,
        )
        page.get_by_text("Mapped peptides", exact=True).click()
        row = page.get_by_role("listitem")
        expect(row).to_contain_text("UNIMOD:1 at the peptide N-terminus (residue 2)")
        expect(row).to_contain_text("UNIMOD:1 at residue 2")
        page.context.close()

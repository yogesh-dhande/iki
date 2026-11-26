# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The QC release components on a phone in both schemes, under the component CSP."""

import re
from typing import Any

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, place, serving
from playwright.sync_api import Browser, Page, expect
from test_browser import (  # noqa: F401 - fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    playwright,
)

COMPONENTS = PLUGINS / "qc_release" / "components"
PHONE = {"width": 390, "height": 844}
# Published USP Sodium Chloride assay, official December 1, 2015. All lot results
# and investigation details below are synthetic.
USP = "https://dev.uspnf.com/sites/default/files/usp_pdf/EN/USPNF/sodium-chloride.pdf"
SODIUM_CHLORIDE: dict[str, Any] = {
    "attribute": "Sodium chloride, dried basis",
    "method": "USP Sodium Chloride Assay",
    "unit": "%",
    "lower": 99.0,
    "upper": 100.5,
}
REPORT: dict[str, Any] = {
    "format": "iki/coa/v1",
    "title": "Certificate of analysis",
    "testing_on": "2026-11-22",
    "lot": {"id": "lot", "hrid": "GLOT-000001", "revision": 2, "lot_number": "Synthetic lot A"},
    "specification": {
        "id": "spec",
        "hrid": "QSV-000001",
        "revision": 2,
        "version": "USP 2015",
        "effective_from": "2015-12-01",
        "source": USP,
    },
    "tests": [
        {
            **SODIUM_CHLORIDE,
            "id": "test-failed",
            "hrid": "QRT-000001",
            "revision": 1,
            "value": 98.8,
            "outcome": "oos",
        },
        {
            **SODIUM_CHLORIDE,
            "id": "test-repeat",
            "hrid": "QRT-000002",
            "revision": 1,
            "value": 99.1,
            "outcome": "pass",
            "repeat_of": "test-failed",
        },
    ],
    "investigations": [
        {
            "id": "investigation",
            "hrid": "QOI-000001",
            "revision": 5,
            "test": "test-failed",
            "resolution": "invalidated",
            "findings": "The recorded dilution factor came from a different sample.",
        }
    ],
}


def accessible(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
    assert page.evaluate("window.refused") == []
    assert ERRORS[page] == []
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []


def test_the_certificate_works_under_the_component_policy(browser: Browser) -> None:
    with serving(COMPONENTS, {"certificate.js": "release-certificate"}) as origin:
        page = open_frame(
            browser,
            origin,
            "certificate.js",
            {"values": {"report": REPORT, "review": "released"}},
            viewport=PHONE,
        )
        expect(page.get_by_role("status")).to_have_text("Issued certificate")
        assert page.evaluate("window.refused") == []
        assert ERRORS[page] == []
        page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_certificate_preserves_the_original_result_and_investigation(
    browser: Browser, scheme: str
) -> None:
    with serving(COMPONENTS, {"certificate.js": "release-certificate"}) as origin:
        page = open_frame(
            browser,
            origin,
            "certificate.js",
            {"values": {"report": REPORT, "review": "recorded"}},
            viewport=PHONE,
            color_scheme=scheme,
            bypass_csp=True,  # axe needs its own script in the frame.
        )
        expect(page.get_by_role("status")).to_have_text("Draft awaiting signed issue")
        expect(page.get_by_text("USP 2015 · QSV-000001 · revision 2")).to_be_visible()
        expect(page.get_by_text("98.8 %")).to_be_visible()
        expect(page.get_by_text("Out of specification")).to_be_visible()
        expect(page.get_by_text("QOI-000001 · revision 5")).to_be_visible()
        expect(page.get_by_text("99.1 %")).to_be_visible()
        expect(page.get_by_text(REPORT["investigations"][0]["findings"])).to_be_visible()
        accessible(page)

        for state in ("signed_off", "released"):
            page.evaluate(
                "values => { document.querySelector('release-certificate').entity = {values}; }",
                {"report": REPORT, "review": state},
            )
            expect(page.get_by_role("status")).to_have_text("Issued certificate")
        page.context.close()


@pytest.mark.parametrize(
    ("element", "row", "text", "action", "button"),
    [
        (
            "lot-release",
            {
                "hrid": "QRC-000001",
                "title": "Lot release",
                "values": {"version": "USP 2015", "testing_on": "2026-11-22", "sealed": False},
            },
            "Testing open",
            "iki/qc_release.open_release",
            "Open release testing",
        ),
        (
            "lot-release",
            {
                "hrid": "QRC-000001",
                "title": "Lot release",
                "values": {
                    "version": "USP 2015",
                    "testing_on": "2026-11-22",
                    "sealed": True,
                    "context": "Release",
                    "cycle": 1,
                    "superseded": True,
                },
            },
            "Release cycle 1 · Specification USP 2015 · 2026-11-22 · Followed by a later cycle",
            "iki/qc_release.open_release",
            "Open release testing",
        ),
        (
            "release-tests",
            {"hrid": "QRT-000001", "values": REPORT["tests"][0]},
            "Out of specification",
            "iki/qc_release.record_test",
            "Record a release test",
        ),
        (
            "case-certificate",
            {
                "hrid": "QCA-000001",
                "title": "Certificate of analysis",
                "values": {"version": "USP 2015", "testing_on": "2026-11-22"},
            },
            "Specification USP 2015",
            None,
            None,
        ),
        (
            "test-investigations",
            {
                "hrid": "QOI-000001",
                "title": "OOS investigation",
                "values": {"resolution": "invalidated"},
            },
            "Invalidated",
            None,
            None,
        ),
    ],
)
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_release_navigation_and_actions_fit_the_lot_workflow(
    browser: Browser,
    scheme: str,
    element: str,
    row: dict[str, Any],
    text: str,
    action: str | None,
    button: str | None,
) -> None:
    with serving(COMPONENTS, {"release-records.js": element}) as origin:
        page = open_frame(
            browser,
            origin,
            "release-records.js",
            {"values": {}},
            viewport=PHONE,
            color_scheme=scheme,
            bypass_csp=True,  # axe needs its own script in the frame.
        )
        place(page, element, {"runs": [action] if action else []})
        page.evaluate(
            """name => {
              const component = document.querySelector(name);
              window.opened = [];
              component.addEventListener("iki-open", e => window.opened.push(e.detail.entity));
              component.entities = { items: [], loading: false };
            }""",
            element,
        )
        if button is not None:
            page.get_by_role("button", name=button).click()
            assert page.evaluate("window.ran") == [action]
        page.evaluate(
            "([name, item]) => { document.querySelector(name).entities = {items: [item]}; }",
            [element, row],
        )
        expect(page.get_by_text(re.compile(text))).to_be_visible()
        if element == "lot-release":
            # A lot with a case is tested again in another cycle or context.
            expect(page.get_by_role("button", name="Open release testing")).to_have_count(0)
            expect(page.get_by_role("button", name="Open another release cycle")).to_be_visible()
        page.get_by_role("button", name=f"Open {row['hrid']}").click()
        assert page.evaluate("window.opened") == [row["hrid"]]
        accessible(page)
        page.context.close()

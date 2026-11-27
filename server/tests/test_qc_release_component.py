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
    "format": "iki/coa-evidence/v1",
    "title": "Certificate evidence",
    "issue": {"state": "released"},
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
        # USP Sodium Chloride's Identification A, a requirement it meets or doesn't.
        {
            "id": "test-identity",
            "hrid": "QRT-000003",
            "revision": 1,
            "attribute": "Identification A (sodium)",
            "method": "USP <191> Identification Tests—General, Sodium",
            "kind": "conforms",
            "acceptance": "Meets the requirements",
            "result": "conforms",
            "observation": "Synthetic: an intense yellow flame.",
            "outcome": "pass",
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


#: What a research certificate states: the final result, and that it's for research.
COA: dict[str, Any] = {
    "format": "iki/coa/v2",
    "title": "Research certificate of analysis",
    "intended_use": "For research use. Issued on one reviewer's sign-off, not released by a "
    "quality unit.",
    "lot": {"lot_number": "Synthetic lot A", "hrid": "GLOT-000001", "revision": 3},
    "specification": {
        "version": "USP 2015",
        "hrid": "QSV-000001",
        "revision": 2,
        "effective_from": "2015-12-01",
        "source": USP,
    },
    "testing_on": "2026-11-22",
    "results": [{**SODIUM_CHLORIDE, "value": 99.1}],
    "issue": {"state": "issued"},
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
        expect(page.get_by_role("status")).to_have_text("Signed certificate record")
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
        expect(page.get_by_role("heading", level=2)).to_have_text("Certificate record")
        expect(page.get_by_role("status")).to_have_text("Certificate record awaiting signatures")
        expect(page.get_by_text("USP 2015 · QSV-000001 · revision 2")).to_be_visible()
        expect(page.get_by_text("98.8 %")).to_be_visible()
        expect(page.get_by_text("Out of specification")).to_be_visible()
        expect(page.get_by_text("QOI-000001 · revision 5")).to_be_visible()
        expect(page.get_by_text("99.1 %")).to_be_visible()
        expect(page.get_by_text(REPORT["investigations"][0]["findings"])).to_be_visible()
        identity = page.get_by_role("listitem").filter(has_text="Identification A (sodium)")
        expect(identity.get_by_text("Meets the requirements", exact=True)).to_be_visible()
        expect(identity.get_by_text("Conforms", exact=True)).to_be_visible()
        expect(identity.get_by_text("Synthetic: an intense yellow flame.")).to_be_visible()
        accessible(page)

        # It's final in the state its procedure named, and no other.
        for state, shown in (
            ("signed_off", "Certificate record awaiting signatures"),
            ("released", "Signed certificate record"),
        ):
            page.evaluate(
                "values => { document.querySelector('release-certificate').entity = {values}; }",
                {"report": REPORT, "review": state},
            )
            expect(page.get_by_role("status")).to_have_text(shown)
        page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_certificate_states_its_final_results_apart_from_its_evidence(
    browser: Browser, scheme: str
) -> None:
    with serving(COMPONENTS, {"certificate.js": "release-certificate"}) as origin:
        page = open_frame(
            browser,
            origin,
            "certificate.js",
            {"values": {"coa": COA, "digest": "0" * 64, "review": "recorded"}},
            viewport=PHONE,
            color_scheme=scheme,
            bypass_csp=True,  # axe needs its own script in the frame.
        )
        expect(page.get_by_role("heading", level=2)).to_have_text(COA["title"])
        expect(page.get_by_role("status")).to_have_text("Draft awaiting signed issue")
        expect(page.get_by_text(COA["intended_use"])).to_be_visible()
        results = page.get_by_role("heading", name="Results")
        expect(results).to_be_visible()
        # A certificate of analysis holds no evidence to show.
        expect(page.get_by_role("heading", name="Evidence")).to_have_count(0)
        stated = page.get_by_role("listitem").filter(has_text="99.1 %").first
        expect(stated.get_by_text("99–100.5 %")).to_be_visible()
        accessible(page)
        page.evaluate(
            "values => { document.querySelector('release-certificate').entity = {values}; }",
            {"coa": COA, "review": "issued"},
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
            expect(page.get_by_role("button", name="Open release testing")).to_have_count(0)
        page.get_by_role("button", name=f"Open {row['hrid']}").click()
        assert page.evaluate("window.opened") == [row["hrid"]]
        accessible(page)
        page.context.close()

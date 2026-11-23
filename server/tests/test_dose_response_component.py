# ruff: noqa: F811 - imported browser fixtures are test parameters
"""A saved curve, its exclusions and source revisions under the component frame's policy."""

from collections.abc import Iterator
from typing import Any

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, serving
from playwright.sync_api import Browser
from test_browser import (  # noqa: F401 - browser fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    browser_name,
    playwright,
)

pytestmark = pytest.mark.built_web_app

MODULE = "dose-response.js"
ACTOR = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    with serving(PLUGINS / "plate_assays" / "components", {MODULE: "assay-dose-response"}) as url:
        yield url


@pytest.fixture
def fit() -> dict[str, Any]:
    # Synthetic UI data. The test checks the 4PL midpoint invariant from Ritz et al.,
    # eq. 2, https://doi.org/10.1371/journal.pone.0146021: response at ED50 = (c+d)/2.
    points = [
        {
            "reading_hrid": f"R-{i:06}",
            "revision": 2,
            "well": f"A{i}",
            "dose": dose,
            "replicate": 1,
            "response": response,
            "exclusion_reason": "Reader flagged saturation" if i == 4 else None,
            "excluded_by": ACTOR if i == 4 else None,
        }
        for i, (dose, response) in enumerate(
            [(0.1, 91), (1, 50), (10, 9), (1, 98), (0.3, 76), (3, 24)], start=1
        )
    ]
    return {
        "fit_status": "fitted",
        "fit_version": "Synthetic fit for display",
        "normalization": "none",
        "endpoint_kind": "IC50",
        "qualifier": "within_range",
        "concentration_unit": "uM",
        "response_unit": "AU",
        "channel": "Absorbance",
        "endpoint_value": 1,
        "ci_low": 0.8,
        "ci_high": 1.2,
        "bottom": 0,
        "top": 100,
        "hill_slope": -1,
        "ci_method": "Synthetic interval for display",
        "points": points,
    }


def test_curve_and_source_revisions(
    browser: Browser, frame_server: str, fit: dict[str, Any]
) -> None:
    page = open_frame(browser, frame_server, MODULE, {"values": fit})
    page.get_by_role("img", name="Concentration–response plot").wait_for()
    assert page.get_by_text("1 uM", exact=True).is_visible()
    assert page.get_by_text("Within tested range").is_visible()
    assert page.get_by_text("95% confidence interval 0.8–1.2 uM").is_visible()
    assert page.locator("circle.included").count() == 5
    assert page.locator("path.excluded").count() == 1
    # The stored sign determines direction on screen. The middle of a log-spaced
    # 0.1–10 plot is dose 1, where this synthetic point equals the fitted midpoint.
    trace = page.locator("polyline.curve")
    assert trace.evaluate("(line) => line.points[0].y < line.points[line.points.length-1].y")
    midpoint = trace.evaluate("(line) => line.points[(line.points.length-1)/2].y")
    assert float(
        page.locator("circle.included").nth(1).get_attribute("cy") or "nan"
    ) == pytest.approx(midpoint)
    summary = page.get_by_text("Source measurements (6)")
    summary.focus()
    summary.press("Enter")
    row = page.get_by_role("row").filter(has_text="R-000004")
    assert "revision 2" in row.inner_text()
    assert f"Excluded by {ACTOR} — Reader flagged saturation" in row.inner_text()
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_extrapolated_and_unreportable_results(
    browser: Browser, frame_server: str, fit: dict[str, Any]
) -> None:
    fit.update(qualifier="above_range", endpoint_value=24, ci_low=12, ci_high=40)
    page = open_frame(browser, frame_server, MODULE, {"values": fit})
    page.get_by_text("Above tested range").wait_for()
    assert page.get_by_text("24 uM").is_visible()
    # A record change replaces the previous curve, while keeping all raw points available.
    fit.update(fit_status="unreportable", fit_reason="Flat response", endpoint_value=None)
    page.locator("assay-dose-response").evaluate("(el, values) => {el.entity = {values}}", fit)
    assert page.get_by_role("status").text_content() == "Endpoint unavailable. Flat response"
    assert page.locator("polyline.curve").count() == 0
    assert page.locator("circle.included").count() == 5
    assert not page.get_by_text("95% confidence interval", exact=False).count()
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_curve_at_mobile_width_passes_wcag(
    browser: Browser, frame_server: str, fit: dict[str, Any], scheme: str
) -> None:
    page = open_frame(
        browser,
        frame_server,
        MODULE,
        {"values": fit},
        viewport={"width": 390, "height": 844},
        color_scheme=scheme,
        bypass_csp=True,
    )
    page.get_by_text("Source measurements (6)").click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

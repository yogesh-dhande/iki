# ruff: noqa: F811 - test_browser fixtures are imported by name.
"""The plate map, served under the plugin frame policy. All readings are synthetic."""

from collections.abc import Iterator
from typing import Any

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, place, serving
from playwright.sync_api import Browser
from test_browser import (  # noqa: F401 - fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    browser_name,
    playwright,
)

pytestmark = pytest.mark.built_web_app

MODULE = "plate-map.js"
ELEMENT = "assay-plate-map"
SAMPLE_ID = "00000000-0000-4000-8000-000000000001"
ASSIGN = "iki/plate_assays.assign_dilution_series"
PREVIEW = "iki/plate_assays.preview_reader"
COMMIT = "iki/plate_assays.commit_reader"


@pytest.fixture(scope="module")
def plate_frame() -> Iterator[str]:
    with serving(PLUGINS / "plate_assays" / "components", {MODULE: ELEMENT}) as origin:
        yield origin


def synthetic_plate(size: str, corner: str) -> dict[str, Any]:
    return {
        "refs": {SAMPLE_ID: {"hrid": "SMP-000001", "title": "Synthetic sample"}},
        "values": {
            "size": size,
            "assignments": [
                {"well": "a01", "control": "Synthetic blank", "replicate": 1},
                {
                    "well": corner,
                    "sample": SAMPLE_ID,
                    "concentration": 1,
                    "concentration_unit": "nM",
                    "replicate": 2,
                },
            ],
            "preview": {
                "readings": [
                    {
                        "well": corner,
                        "sample": SAMPLE_ID,
                        "concentration": 1,
                        "concentration_unit": "nM",
                        "replicate": 2,
                        "channel": "Absorbance",
                        "value": 0.5,
                        "unit": "AU",
                        "source_row": 4,
                    }
                ],
                "issues": [],
            },
        },
    }


# ANSI/SLAS 4-2004, sections 4.1.1 and 4.2.1 specify the two rectangular layouts.
# https://www.slas.org/SLAS/assets/File/public/standards/ANSI_SLAS_4-2004_WellPositions.pdf
@pytest.mark.parametrize(("count", "corner"), [(96, "H12"), (384, "P24")])
def test_plate_geometry_assignments_and_preview(
    browser: Browser, plate_frame: str, count: int, corner: str
) -> None:
    plate = synthetic_plate(f"plate_{count}", corner)
    page = open_frame(browser, plate_frame, MODULE, plate)
    grid = page.get_by_role("table", name="Well positions")
    grid.wait_for()
    assert grid.locator("tbody td").count() == count
    assert grid.get_by_role("cell", name="A1, control Synthetic blank").text_content() == "C"
    assert (
        grid.get_by_role(
            "cell", name=f"{corner}, sample SMP-000001 · Synthetic sample"
        ).text_content()
        == "S"
    )
    assert grid.get_by_role("cell", name="A2, Unassigned").text_content() == "·"
    assignments = page.get_by_role("table", name="Well assignments")
    row = assignments.get_by_role("row").filter(has_text=corner)
    assert row.get_by_role("cell", name="1 nM").is_visible()
    assert row.get_by_role("cell", name="2", exact=True).is_visible()
    preview = page.get_by_role("table", name="Mapped reader preview")
    assert preview.get_by_role("cell", name="0.5 AU").is_visible()
    assert preview.get_by_role("cell", name="SMP-000001 · Synthetic sample").is_visible()
    assert preview.get_by_role("cell", name="1 nM").is_visible()
    assert page.get_by_role("button").count() == 0
    place(page, ELEMENT, {"runs": [ASSIGN, PREVIEW, COMMIT]})
    page.get_by_role("button", name="Assign a dilution series").click()
    page.get_by_role("button", name="Commit measurements").click()
    assert page.evaluate("window.ran") == [ASSIGN, COMMIT]
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_preview_issues_and_repeated_wells(browser: Browser, plate_frame: str) -> None:
    plate = synthetic_plate("plate_96", "A1")
    plate["values"]["preview"]["issues"] = ["A1 has two assignments."]
    page = open_frame(browser, plate_frame, MODULE, plate)
    place(page, ELEMENT, {"runs": [PREVIEW, COMMIT]})
    assert page.get_by_role("cell", name="A1, Multiple assignments").text_content() == "!"
    assert (
        page.get_by_role("list", name="Reader preview issues")
        .get_by_text("A1 has two assignments.")
        .is_visible()
    )
    assert page.get_by_role("button", name="Commit measurements").count() == 0
    page.get_by_role("button", name="Refresh reader preview").click()
    assert page.evaluate("window.ran") == [PREVIEW]
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_map_accessibility_at_phone_width(browser: Browser, plate_frame: str, scheme: str) -> None:
    page = open_frame(
        browser,
        plate_frame,
        MODULE,
        synthetic_plate("plate_384", "P24"),
        color_scheme=scheme,
        viewport={"width": 390, "height": 844},
        bypass_csp=True,
    )
    place(page, ELEMENT, {"runs": [PREVIEW, COMMIT]})
    page.get_by_role("table", name="Well positions").wait_for()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

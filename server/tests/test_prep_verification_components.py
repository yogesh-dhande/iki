# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""A physical prep's Verification tab, open and page events in its sandboxed frame."""

from collections.abc import Iterator
from typing import Any

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, place, serving
from playwright.sync_api import Browser, Page
from test_browser import (  # noqa: F401 - fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    browser_name,
    playwright,
)

pytestmark = pytest.mark.built_web_app
ELEMENT = "prep-verifications"
MODULE = "prep-verifications.js"
ACTION = "iki/lots.record_plasmid_verification"
TAB = {"slot": "entity.tab", "label": "Verification"}
# Entirely synthetic verification and signatures for testing presentation.
PERSON, PLASMID, ALIGNMENT, REPORT = (f"00000000-0000-0000-0000-00000000000{i}" for i in range(4))
PLASMID_RECORD = "00000000-0000-0000-0000-000000000004"
ITEM: dict[str, Any] = {
    "hrid": "VER-000001",
    "values": {
        "review": "released",
        "review_flow": "regulated_qc",
        "outcome": "discrepancy",
        "reviewer": PERSON,
        "plasmid": PLASMID,
        "plasmid_record_id": PLASMID_RECORD,
        "plasmid_revision": 8,
        "alignment": ALIGNMENT,
        "alignment_revision": 2,
        "report": REPORT,
    },
    "refs": {PLASMID: {"hrid": "PLS-000001"}, ALIGNMENT: {"hrid": "ALN-000001"}},
    "people": {PERSON: {"name": "Example reviewer"}},
}


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    with serving(PLUGINS / "lots" / "components", {MODULE: ELEMENT}) as origin:
        yield origin


def give(page: Page, items: list[dict[str, Any]], **updates: Any) -> None:
    state = {"items": items, "more": False, "limited": False, "loading": False, "error": None}
    page.evaluate(
        "(s) => { document.querySelector('prep-verifications').entities = s; }", state | updates
    )


def listen(page: Page) -> None:
    page.wait_for_function("document.querySelector('prep-verifications') !== null")
    page.evaluate("""() => {
        const viewer = document.querySelector("prep-verifications");
        window.opened = [];
        window.pages = 0;
        viewer.addEventListener("iki-open", e => window.opened.push(e.detail.entity));
        viewer.addEventListener("iki-more", () => window.pages++);
    }""")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_prep_opens_a_verification_with_its_pinned_evidence(
    browser: Browser, frame_server: str, scheme: str
) -> None:
    page = open_frame(
        browser,
        frame_server,
        MODULE,
        {},
        viewport={"width": 390, "height": 844},
        color_scheme=scheme,
        bypass_csp=True,
    )
    listen(page)
    give(page, [ITEM], more=True)
    row = page.get_by_role("listitem")
    assert row.locator("dd").all_text_contents() == [
        "Discrepancy",
        "Released",
        "Regulated QC",
        "Example reviewer",
        "PLS-000001 · revision 8",
        "ALN-000001 · revision 2",
        "Attached",
    ]
    assert row.get_by_text("Reviewer", exact=True).is_visible()
    row.get_by_text("Pinned plasmid project record").click()
    assert row.get_by_text(PLASMID_RECORD, exact=True).is_visible()
    assert page.get_by_text(REPORT, exact=True).count() == 0
    open_result = row.get_by_role("button", name="Open verification and evidence for VER-000001")
    open_result.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.opened") == [ITEM["hrid"]]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.get_by_role("button", name="Show more verifications").click()
    assert page.evaluate("window.pages") == 1
    next_item = {**ITEM, "hrid": "VER-000002"}
    give(page, [ITEM, next_item])
    assert page.get_by_role("listitem").count() == 2
    assert page.get_by_role("button", name="Show more verifications").count() == 0
    assert ERRORS[page] == []
    page.context.close()


def test_empty_inaccessible_and_unsigned_records(browser: Browser, frame_server: str) -> None:
    page = open_frame(browser, frame_server, MODULE, {})
    listen(page)
    give(page, [], loading=True)
    assert page.get_by_role("status").inner_text() == "Loading verifications…"
    give(page, [])
    assert (
        page.get_by_role("status").inner_text()
        == "No verification records are visible for this prep."
    )
    action = page.get_by_role("button", name="Record verification")
    assert action.count() == 0
    place(page, ELEMENT, {**TAB, "runs": [ACTION]})
    action.click()
    assert page.evaluate("window.ran") == [ACTION]
    place(page, ELEMENT, {**TAB, "runs": []})
    assert action.count() == 0
    restricted = {
        **ITEM,
        "refs": {},
        "people": {},
        "values": {
            **ITEM["values"],
            "review": "recorded",
            "review_flow": "research",
            "report": None,
        },
    }
    give(page, [restricted], limited=True)
    assert page.get_by_text("Assigned reviewer").is_visible()
    assert page.get_by_text("Unavailable", exact=True).is_visible()
    assert page.get_by_text("Unavailable reference · revision 2").is_visible()
    for raw in (PERSON, ALIGNMENT, PLASMID, PLASMID_RECORD):
        assert page.get_by_text(raw, exact=True).count() == 0
    assert page.get_by_role("status").inner_text() == "Showing the first 1 verification."
    give(page, [], error="The verification search was refused.")
    assert page.get_by_role("status").inner_text() == "The verification search was refused."
    assert page.get_by_role("listitem").count() == 0
    assert page.evaluate("window.refused") == []
    page.context.close()

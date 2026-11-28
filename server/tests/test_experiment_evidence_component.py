# ruff: noqa: F811 - test_browser fixtures are imported by name.
"""The experiment witness inspects the captured evidence in the sandboxed component."""

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

MODULE = "evidence.js"
ELEMENT = "experiment-evidence"
CAPTURE = "iki/experiments.capture_results"
ENTRY = "00000000-0000-4000-8000-000000000001"
RECORD = "00000000-0000-4000-8000-000000000002"
LOT = "00000000-0000-4000-8000-000000000003"
OPERATOR = "00000000-0000-4000-8000-000000000004"
MOVEMENT = "00000000-0000-4000-8000-000000000005"
SAMPLE = "00000000-0000-4000-8000-000000000006"
FILE = "00000000-0000-4000-8000-000000000007"
HIDDEN = "00000000-0000-4000-8000-000000000008"
SOURCE = "00000000-0000-4000-8000-000000000009"
PURGED = "00000000-0000-4000-8000-00000000000a"


@pytest.fixture(scope="module")
def evidence_frame() -> Iterator[str]:
    with serving(PLUGINS / "experiments" / "components", {MODULE: ELEMENT}) as origin:
        yield origin


def synthetic_experiment() -> dict[str, Any]:
    return {
        "refs": {
            ENTRY: {"hrid": "RES-000001", "title": "Synthetic curve"},
            SAMPLE: {"hrid": "SMP-000001", "title": "Synthetic sample"},
            PURGED: {"hrid": "RES-000002", "title": "Purged record", "purged": True},
        },
        "files": {FILE: {"name": "notebook.pdf", "media_type": "application/pdf"}},
        "people": {OPERATOR: {"name": "Test operator"}},
        "values": {
            "title": "Synthetic witness example",
            "review": "reviewed",
            "eln_id": "notebook-9",
            "eln_url": "https://notebook.example/entries/9",
            "eln_revision": "check-in 4",
            "protocol_id": "SOP-7",
            "protocol_revision": "3.0",
            "samples": [SAMPLE],
            "attachments": [FILE, HIDDEN],
            "observations": [
                {"observed_at": "2026-01-01T10:30:00Z", "note": "Synthetic observation"}
            ],
            "result_pins": [
                {
                    "result": ENTRY,
                    "record_id": RECORD,
                    "record_revision": 7,
                    "entry_id": ENTRY,
                    "entry_revision": 2,
                },
                {"result": HIDDEN, "record_id": HIDDEN, "record_revision": 1},
                {"result": PURGED, "record_id": PURGED, "record_revision": 4},
            ],
            "material_draws": [
                {
                    "movement": MOVEMENT,
                    "record": LOT,
                    "record_revision": 8,
                    "source": SOURCE,
                    "source_revision": 6,
                    "amount": "0.250",
                    "unit": "mL",
                    "at": "2026-01-01T09:00:00Z",
                    "by": OPERATOR,
                }
            ],
        },
    }


def test_a_witness_opens_the_pinned_record_and_checks_its_support(
    browser: Browser, evidence_frame: str
) -> None:
    page = open_frame(browser, evidence_frame, MODULE, synthetic_experiment())
    page.get_by_role("heading", name="Experiment evidence").wait_for()
    assert page.get_by_text("check-in 4", exact=True).is_visible()
    assert page.get_by_text("3.0", exact=True).is_visible()
    assert page.get_by_text("https://notebook.example/entries/9", exact=True).is_visible()
    assert page.get_by_role("link").count() == 0
    table = page.get_by_role("table", name="Captured results")
    assert table.get_by_text(f"{ENTRY} · revision 2").is_visible()
    assert table.get_by_text("Restricted result · revision 1").is_visible()
    assert table.get_by_role("button").count() == 2
    page.evaluate(
        """(name) => {
          window.opened = [];
          const record = e => window.opened.push(e.detail);
          document.querySelector(name).addEventListener('iki-open', record);
        }""",
        ELEMENT,
    )
    table.get_by_role("button", name="RES-000001 · Synthetic curve · revision 7").click()
    table.get_by_role("button", name=f"{ENTRY} · revision 2").click()
    assert page.evaluate("window.opened") == [
        {"entity": RECORD, "revision": 7},
        {"entity": ENTRY, "revision": 2},
    ]
    for text in (
        "SMP-000001 · Synthetic sample",
        "0.250 mL",
        f"{LOT} · revision 8",
        f"{SOURCE} · revision 6",
        "Test operator",
        MOVEMENT,
        "2026-01-01T09:00:00Z",
        "Synthetic observation",
        "2026-01-01T10:30:00Z",
        "notebook.pdf · application/pdf",
        "Restricted attachment",
    ):
        assert page.get_by_text(text, exact=True).is_visible()
    place(page, ELEMENT, {"runs": [CAPTURE]})
    assert page.get_by_role("button", name="Capture results for review").count() == 0
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_a_recorded_experiment_offers_capture_when_allowed(
    browser: Browser, evidence_frame: str
) -> None:
    page = open_frame(browser, evidence_frame, MODULE, {"values": {"review": "recorded"}})
    page.get_by_text("No result revisions captured.").wait_for()
    assert page.get_by_role("button").count() == 0
    place(page, ELEMENT, {"runs": [CAPTURE]})
    page.get_by_role("button", name="Capture results for review").click()
    assert page.evaluate("window.ran") == [CAPTURE]
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_experiment_evidence_at_phone_width(
    browser: Browser, evidence_frame: str, scheme: str
) -> None:
    page = open_frame(
        browser,
        evidence_frame,
        MODULE,
        synthetic_experiment(),
        color_scheme=scheme,
        viewport={"width": 390, "height": 844},
        bypass_csp=True,
    )
    page.get_by_role("heading", name="Experiment evidence").wait_for()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

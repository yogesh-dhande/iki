# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""Equipment records in the plugin frame, with synthetic schedules and evidence."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
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
MODULE = "equipment.js"
SERVICE = "iki-equipment-service"
IMPACT = "iki-calibration-impacts"
USE = "iki-instrument-uses"
ENGINEER, INSTRUMENT, FAILURE, RESULT, EVIDENCE, REPORT, PASSING = (
    f"00000000-0000-0000-0000-00000000000{i}" for i in range(7)
)
SCHEDULED: dict[str, Any] = {
    "hrid": "EQS-000001",
    "values": {
        "kind": "maintenance",
        "due_on": "2026-04-29",
        "procedure": "Inspect and clean optics (example procedure)",
        "review": "scheduled",
    },
}
CALIBRATION: dict[str, Any] = {
    "hrid": "EQS-000002",
    "values": {
        "kind": "calibration",
        "due_on": "2026-04-30",
        "procedure": "Optical check (example procedure)",
        "performed_at": "2026-04-30T09:00:00Z",
        "performed_by": ENGINEER,
        "outcome": "out_of_tolerance",
        "suspect_since": "2026-04-02T08:00:00Z",
        "review": "released",
        "report": REPORT,
    },
    "people": {ENGINEER: {"name": "Example engineer"}},
}
MAINTENANCE: dict[str, Any] = {
    "hrid": "EQS-000003",
    "values": {
        "kind": "maintenance",
        "due_on": "2026-03-30",
        "procedure": "Inspect and clean optics (example procedure)",
        "performed_at": "2026-03-30T09:00:00Z",
        "outcome": "completed",
        "valid_until": "2026-04-30T09:00:00Z",
        "review": "signed_off",
    },
}
USE_ROW: dict[str, Any] = {
    "hrid": "EQU-000001",
    "values": {
        "instrument": INSTRUMENT,
        "result": RESULT,
        "result_revision": 7,
        "calibration": PASSING,
        "calibration_revision": 4,
        "measurement_at": "2026-04-02T10:30:00Z",
    },
    "refs": {INSTRUMENT: {"hrid": "INS-000001"}, RESULT: {"hrid": "QCD-000001"}},
}
IMPACT_ROW: dict[str, Any] = {
    "hrid": "EQI-000001",
    "values": {
        "failure": FAILURE,
        "failure_revision": 5,
        "use": EVIDENCE,
        "result": RESULT,
        "measurement_at": "2026-04-02T10:30:00Z",
        "disposition": "repeat_required",
        "review": "submitted",
    },
    "refs": {
        FAILURE: {"hrid": CALIBRATION["hrid"]},
        EVIDENCE: {"hrid": USE_ROW["hrid"]},
        RESULT: {"hrid": "QCD-000001"},
    },
}


@pytest.fixture(params=[SERVICE, IMPACT, USE])
def equipment_frame(request: pytest.FixtureRequest) -> Iterator[tuple[str, str]]:
    with serving(PLUGINS / "equipment" / "components", {MODULE: request.param}) as origin:
        yield origin, request.param


def give(page: Page, element: str, items: list[dict[str, Any]], **updates: Any) -> None:
    state = {"items": items, "more": False, "limited": False, "loading": False, "error": None}
    page.evaluate(
        "([name, state]) => { document.querySelector(name).entities = state; }",
        [element, state | updates],
    )


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_equipment_evidence_fits_a_phone_and_opens_the_record(
    browser: Browser, equipment_frame: tuple[str, str], scheme: str
) -> None:
    origin, element = equipment_frame
    page = open_frame(
        browser,
        origin,
        MODULE,
        {"values": {"service": "out_of_service", "calibration": "failed"}},
        viewport={"width": 390, "height": 844},
        color_scheme=scheme,
        bypass_csp=True,
    )
    page.clock.set_fixed_time(datetime(2026, 5, 1, 12, tzinfo=UTC))
    page.wait_for_function("(name) => document.querySelector(name) !== null", arg=element)
    page.evaluate(
        """(name) => {
          const viewer = document.querySelector(name);
          window.opened = [];
          window.pages = 0;
          viewer.addEventListener("iki-open", e => window.opened.push(e.detail.entity));
          viewer.addEventListener("iki-more", () => window.pages++);
        }""",
        element,
    )
    assert page.get_by_role("status").inner_text() == "Loading records…"

    if element == SERVICE:
        give(page, element, [SCHEDULED, CALIBRATION, MAINTENANCE])
        assert page.get_by_text("Out of service", exact=True).is_visible()
        assert page.get_by_text("Failed", exact=True).is_visible()
        assert page.get_by_text("Overdue", exact=True).is_visible()
        assert page.get_by_text("Maintenance due", exact=True).is_visible()
        assert page.get_by_text("Out of tolerance", exact=True).count() == 2
        assert page.get_by_text("Example engineer").is_visible()
        assert page.get_by_text(REPORT, exact=True).count() == 0
        assert page.get_by_role("button", name="Schedule service").count() == 0
        available = ["iki/equipment.schedule_service", "iki/equipment.trace_failed_calibrations"]
        place(page, element, {"slot": "entity.tab", "runs": available})
        page.get_by_role("button", name="Schedule service").click()
        page.get_by_role("button", name="Trace failed calibrations").click()
        assert page.evaluate("window.ran") == available
        opened = CALIBRATION
    elif element == IMPACT:
        give(page, element, [IMPACT_ROW])
        assert page.get_by_text("QCD-000001", exact=True).is_visible()
        assert page.get_by_text("EQS-000002 · revision 5").is_visible()
        assert page.get_by_text("Pending disposition").is_visible()
        released = {**IMPACT_ROW, "values": {**IMPACT_ROW["values"], "review": "released"}}
        give(page, element, [released])
        assert page.get_by_text("Repeat required").is_visible()
        assert page.get_by_text("Pending disposition").count() == 0
        opened = IMPACT_ROW
    else:
        give(page, element, [USE_ROW])
        assert page.get_by_text("QCD-000001 · revision 7").is_visible()
        assert page.get_by_text("Unavailable reference · revision 4").is_visible()
        assert page.get_by_text(PASSING, exact=True).count() == 0
        opened = USE_ROW

    page.get_by_role("button", name=f"Open {opened['hrid']}").focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.opened") == [opened["hrid"]]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []

    give(page, element, [opened], more=True)
    page.get_by_role("button", name="Show more records").click()
    assert page.evaluate("window.pages") == 1
    give(page, element, [opened], limited=True)
    assert page.get_by_role("status").inner_text() == "Showing the first 1 record."
    assert page.get_by_role("button", name="Show more records").count() == 0
    give(page, element, [], error="The search was refused.")
    assert page.get_by_role("status").inner_text() == "The search was refused."
    assert page.get_by_role("listitem").count() == 0
    assert ERRORS[page] == []
    page.context.close()


def test_an_instrument_outside_the_calibration_program_is_due_for_nothing(
    browser: Browser,
) -> None:
    """A centrifuge isn't calibrated, and its maintenance procedure sets no interval: its
    schedule says neither is due."""
    serviced = {
        "hrid": "EQS-000004",
        "values": {
            "kind": "maintenance",
            "due_on": "2026-04-20",
            "procedure": "Rotor inspection (example procedure)",
            "performed_at": "2026-04-20T09:00:00Z",
            "outcome": "completed",
            "review": "signed_off",
        },
    }
    with serving(PLUGINS / "equipment" / "components", {MODULE: SERVICE}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {"values": {"service": "in_service", "calibration": "not_required"}},
            bypass_csp=True,
        )
        page.clock.set_fixed_time(datetime(2026, 5, 1, 12, tzinfo=UTC))
        page.wait_for_function("(name) => document.querySelector(name) !== null", arg=SERVICE)
        give(page, SERVICE, [serviced])
        assert page.get_by_text("Not required", exact=True).is_visible()
        assert page.get_by_text("Calibration evidence", exact=True).count() == 0
        assert page.get_by_text("Calibration due", exact=True).count() == 0
        assert page.get_by_text("Maintenance due", exact=True).count() == 0
        # Its maintenance evidence: done when it was, and not due again.
        assert page.get_by_text(re.compile(r"^Completed .*2026")).is_visible()


def test_a_case_opened_manually_says_who_opened_it_and_why(browser: Browser) -> None:
    """A plate read on the failed reader with no use recorded: its case names no instrument
    evidence, and says Equipment QA opened it, and why, beside a traced case."""
    qa = "00000000-0000-0000-0000-000000000009"
    reason = "Bench logbook, page 41: plate 7 read on this reader."
    manual = {
        "hrid": "EQI-000002",
        "values": {
            **IMPACT_ROW["values"],
            "use": None,
            "opened": "manually",
            "opened_by": qa,
            "opened_reason": reason,
        },
        "refs": {FAILURE: {"hrid": CALIBRATION["hrid"]}, RESULT: {"hrid": "QCD-000002"}},
        "people": {qa: {"name": "Example QA"}},
    }
    traced = {**IMPACT_ROW, "values": {**IMPACT_ROW["values"], "opened": "traced"}}
    with serving(PLUGINS / "equipment" / "components", {MODULE: IMPACT}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            {"values": {}},
            viewport={"width": 390, "height": 844},
            bypass_csp=True,
        )
        page.wait_for_function("(name) => document.querySelector(name) !== null", arg=IMPACT)
        give(page, IMPACT, [traced, manual])
        rows = page.get_by_role("listitem")
        assert rows.nth(0).get_by_text(USE_ROW["hrid"], exact=True).is_visible()
        assert rows.nth(0).get_by_text("Opened manually by").count() == 0
        opened = rows.nth(1)
        assert opened.get_by_text("None recorded", exact=True).is_visible()
        assert opened.get_by_text("Example QA", exact=True).is_visible()
        assert opened.get_by_text(reason, exact=True).is_visible()
        assert page.get_by_text(qa, exact=True).count() == 0
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        assert ERRORS[page] == []
        page.context.close()

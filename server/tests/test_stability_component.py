# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The stability study tabs present pull windows and reviewed measurements."""

from collections.abc import Iterator
from typing import Any

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, place, serving
from playwright.sync_api import Browser, Page, expect
from test_browser import (  # noqa: F401 - fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    browser_name,
    playwright,
)

SCHEDULE = "stability-pull-schedule"
TREND = "stability-measurement-trend"
SCHEDULE_MODULE = "pull-schedule.js"
TREND_MODULE = "measurement-trend.js"
ACTION = "iki/stability.schedule_pulls"

# Synthetic pull dates and readings. Their condition labels follow ICH Q1A(R2), §2.2.7.1.
# https://database.ich.org/sites/default/files/Q1A%28R2%29%20Guideline.pdf
CONDITIONS = [
    {"code": "long_term", "label": "25 °C / 60% RH"},
    {"code": "accelerated", "label": "40 °C / 75% RH"},
]
STUDY = {"values": {"conditions": CONDITIONS}}


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    elements = {SCHEDULE_MODULE: SCHEDULE, TREND_MODULE: TREND}
    with serving(PLUGINS / "stability" / "components", elements) as origin:
        yield origin


def give(page: Page, element: str, items: list[dict[str, Any]], **updates: Any) -> None:
    state = {"items": items, "more": False, "limited": False, "loading": False, "error": None}
    page.locator(element).evaluate("(el, value) => { el.entities = value; }", state | updates)


def listen(page: Page, element: str) -> None:
    page.locator(element).evaluate("""el => {
        window.opened = [];
        window.refinements = [];
        window.pages = 0;
        el.addEventListener('iki-open', event => window.opened.push(event.detail.entity));
        el.addEventListener('iki-refine', event => window.refinements.push(event.detail));
        el.addEventListener('iki-more', () => window.pages++);
    }""")


def pull(number: int, *, condition: int = 0, **changes: Any) -> dict[str, Any]:
    settings = CONDITIONS[condition]
    return {
        "hrid": f"STP-{number:06}",
        "values": {
            "condition": settings["code"],
            "condition_label": settings["label"],
            "time_point": f"M{number}",
            "replicate": 1,
            "opens_on": "2020-03-01",
            "due_on": "2020-03-03",
            "closes_on": "2020-03-05",
            "tube": "a-tube-id",
            "amount": 10,
            "amount_unit": "uL",
            "tests": ["Assay"],
            "status": "scheduled",
            **changes,
        },
        "refs": {"a-tube-id": {"hrid": "TUB-000001"}},
    }


def reading(number: int, *, condition: int = 0, **changes: Any) -> dict[str, Any]:
    settings = CONDITIONS[condition]
    return {
        "hrid": f"STM-{number:06}",
        "values": {
            "condition": settings["code"],
            "condition_label": settings["label"],
            "time_point": f"M{number}",
            "elapsed_days": 30 * number,
            "replicate": 1,
            "pulled_on": "2020-03-08",
            "test": "Assay",
            "method": "Example method 1",
            "unit": "%",
            "value": 99.7,
            "review": "signed",
            **changes,
        },
    }


def accessible(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_pull_schedule_distinguishes_overdue_missed_and_late_pulls(
    browser: Browser, frame_server: str, scheme: str
) -> None:
    page = open_frame(
        browser,
        frame_server,
        SCHEDULE_MODULE,
        STUDY,
        viewport={"width": 390, "height": 844},
        color_scheme=scheme,
        locale="en-US",
        bypass_csp=True,
    )
    listen(page, SCHEDULE)
    expect(page.get_by_role("status")).to_have_text("Loading pulls…")
    place(page, SCHEDULE, {"slot": "entity.tab", "label": "Schedule", "runs": [ACTION]})
    page.get_by_role("button", name="Schedule pulls").click()
    assert page.evaluate("window.ran") == [ACTION]
    page.locator(SCHEDULE).evaluate(
        "(el, study) => { el.entity = study; }", {"values": STUDY["values"] | {"scheduled": True}}
    )
    give(
        page,
        SCHEDULE,
        [
            pull(1),
            pull(2, status="missed"),
            pull(3, condition=1, status="pulled", pulled_on="2020-03-08", window_note="Tube held"),
            pull(4, condition=1, status="pulled", pulled_on="2020-02-28", amount=13),
            # A pull by mass, drawn as a volume at its tube's concentration, in duplicate.
            pull(
                5, amount=50, amount_unit="ug", replicate=2, status="pulled", pulled_on="2020-03-03"
            ),
        ],
        more=True,
    )
    expect(page.get_by_role("button", name="Schedule pulls")).to_have_count(0)
    for status in ("Overdue", "Missed", "Pulled late", "Pulled early"):
        expect(page.get_by_role("list").get_by_text(status, exact=True)).to_be_visible()
    expect(page.get_by_text("Planned amount", exact=True)).to_have_count(2)
    by_mass = page.get_by_role("listitem").filter(has_text="M5 · Replicate 2")
    expect(by_mass.get_by_text("50 µg", exact=True)).to_be_visible()
    late = page.get_by_role("listitem").filter(has_text="Pulled late")
    expect(late.get_by_text("Mar 8, 2020", exact=True)).to_have_attribute("datetime", "2020-03-08")
    expect(late.get_by_text("10 µL", exact=True)).to_be_visible()
    expect(late.get_by_text("TUB-000001", exact=True)).to_be_visible()
    expect(late.get_by_text("Tube held", exact=True)).to_be_visible()
    page.get_by_role("combobox", name="Condition").select_option("accelerated")
    assert page.evaluate("window.refinements") == [
        {"where": {"field": "condition", "op": "eq", "value": "accelerated"}}
    ]
    page.get_by_role("combobox", name="Pull status").select_option("late")
    expect(page.get_by_role("listitem")).to_have_count(1)
    late.get_by_role("button", name="Open pull STP-000003").press("Enter")
    assert page.evaluate("window.opened") == ["STP-000003"]
    page.get_by_role("button", name="Show more pulls").click()
    assert page.evaluate("window.pages") == 1
    accessible(page)
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_stability_trend_keeps_only_signed_results_in_the_same_test_series(
    browser: Browser, frame_server: str, scheme: str
) -> None:
    page = open_frame(
        browser,
        frame_server,
        TREND_MODULE,
        STUDY,
        viewport={"width": 390, "height": 844},
        color_scheme=scheme,
        locale="en-US",
        bypass_csp=True,
    )
    listen(page, TREND)
    give(
        page,
        TREND,
        [
            reading(1, time_point="T0", elapsed_days=0),
            # Weeks and months share one axis by their days from the start.
            reading(2, time_point="W2", elapsed_days=14, review="released", value=99.1),
            reading(3, review="reviewed", value=123),
            reading(4, method="Example method 2", value=5),
            reading(5, unit="mg/mL", value=48),
            reading(6, condition=1, value=73),
        ],
        more=True,
    )
    expect(page.get_by_role("img", name="Assay in % by scheduled time point")).to_be_visible()
    expect(page.get_by_role("row")).to_have_count(3)
    expect(page.get_by_text("99.7", exact=True)).to_be_visible()
    expect(page.get_by_text("99.1", exact=True)).to_be_visible()
    expect(page.get_by_text("123", exact=True)).to_have_count(0)
    series = page.get_by_role("combobox", name="Test series")
    expect(series.locator("option")).to_have_count(4)
    series.select_option(label="Assay · Example method 2 · % · 25 °C / 60% RH")
    expect(page.get_by_role("row")).to_have_count(2)
    expect(page.get_by_text("5", exact=True)).to_be_visible()
    page.get_by_role("combobox", name="Condition").select_option("accelerated")
    expect(series.locator("option")).to_have_count(1)
    expect(page.get_by_text("73", exact=True)).to_be_visible()
    assert page.evaluate("window.refinements") == [
        {"where": {"field": "condition", "op": "eq", "value": "accelerated"}}
    ]
    page.get_by_role("button", name="Open reviewed result STM-000006").press("Enter")
    assert page.evaluate("window.opened") == ["STM-000006"]
    page.get_by_role("button", name="Show more reviewed results").click()
    assert page.evaluate("window.pages") == 1
    accessible(page)
    give(page, TREND, [reading(7, condition=1, review="submitted")])
    expect(page.get_by_role("img")).to_have_count(0)
    expect(page.get_by_role("status")).to_have_text(
        "No signed or QA-released results are visible for this condition."
    )
    page.context.close()

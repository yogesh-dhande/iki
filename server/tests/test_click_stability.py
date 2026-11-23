# ruff: noqa: F811 - pytest injects these fixtures by name.
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import pytest
from click_stability import install
from playwright.sync_api import Browser, Page
from test_browser import NEXT_FRAMES, playwright  # noqa: F401 - pytest fixture
from test_browser import browser as detector_browser  # noqa: F401 - pytest fixture

BUTTONS = """
<style>
button { position: absolute; top: 16px; left: 16px; width: 100px; height: 40px; }
</style>
<button id="release">Release</button>
<button id="press">Press</button>
"""
MOVE = "document.querySelector('#press').style.left = '200px'"


@pytest.fixture
def detector_page(detector_browser: Browser) -> Iterator[tuple[Page, list[dict[str, Any]]]]:
    records: list[dict[str, Any]] = []
    with detector_browser.new_context() as context:
        install(context, records)
        page = context.new_page()
        page.goto("data:text/html," + quote(BUTTONS))
        page.evaluate(NEXT_FRAMES)
        yield page, records


def test_stable_press(detector_page: tuple[Page, list[dict[str, Any]]]) -> None:
    page, records = detector_page
    page.mouse.click(40, 30)
    assert records == []


def test_press_moves_before_release(
    detector_page: tuple[Page, list[dict[str, Any]]],
) -> None:
    page, records = detector_page
    url = page.url
    page.evaluate("window.addEventListener('pointerup', () => location.replace('about:blank'))")
    page.mouse.move(40, 30)
    page.mouse.down()
    page.evaluate(MOVE)
    page.evaluate(NEXT_FRAMES)
    page.mouse.up()
    page.wait_for_url("about:blank")
    page.close()

    (hit,) = records
    assert hit["url"] == hit["page"] == url
    assert hit["mismatched"]
    assert hit["down"]["name"] == "Press"
    assert hit["up"]["name"] == "Release"
    assert hit["down"]["at"] <= hit["up"]["at"]
    assert hit["up"]["path"].endswith("button#release")


def test_attributed_shift_between_pointer_events(
    detector_page: tuple[Page, list[dict[str, Any]]], detector_browser: Browser
) -> None:
    page, records = detector_page
    # Chromium can buffer shifts during a native press and publish only an aggregate score
    # without sources on release. Use DOM pointer events to exercise an attributed shift.
    page.mouse.click(300, 30)
    press = page.locator("#press")
    press.dispatch_event("pointerdown", {"pointerId": 10, "bubbles": True})
    page.evaluate(MOVE)
    page.evaluate(NEXT_FRAMES)
    press.dispatch_event("pointerup", {"pointerId": 10, "bubbles": True})
    if detector_browser.browser_type.name == "chromium":
        (hit,) = records
        assert not hit["mismatched"]
        (shift,) = hit["shifts"]
        assert shift["element"]["name"] == "Press"
        assert hit["down"]["at"] <= shift["at"] <= hit["up"]["at"]
        assert shift["hadRecentInput"]
        assert shift["from"]["x"] == 16
        assert shift["to"]["x"] == 200
    else:
        assert records == []


def test_press_after_shift(
    detector_page: tuple[Page, list[dict[str, Any]]], detector_browser: Browser
) -> None:
    page, records = detector_page
    page.evaluate(MOVE)
    page.evaluate(NEXT_FRAMES)
    page.mouse.click(220, 30)

    if detector_browser.browser_type.name == "chromium":
        (hit,) = records
        assert not hit["mismatched"]
        assert hit["down"]["name"] == hit["up"]["name"] == "Press"
        (shift,) = hit["shifts"]
        assert 0 <= hit["down"]["at"] - shift["at"] <= 500
    else:
        assert records == []

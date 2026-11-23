# ruff: noqa: F811 - pytest injects these fixtures by name.
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from click_stability import finish, install
from playwright.sync_api import Browser, Page
from test_browser import NEXT_FRAMES, playwright  # noqa: F401 - pytest fixture
from test_browser import browser as detector_browser  # noqa: F401 - pytest fixture

pytestmark = pytest.mark.browser

BUTTONS = """
<style>
button { position: absolute; top: 16px; left: 16px; width: 100px; height: 40px; }
@media (width < 800px) { #press { left: 200px; } }
</style>
<button id="release">Release</button>
<button id="press">Press</button>
"""
MOVE = "document.querySelector('#press').style.left = '200px'"


@pytest.fixture
def detector_page(
    detector_browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Page, list[dict[str, Any]]]]:
    records: list[dict[str, Any]] = []
    with detector_browser.new_context() as context:
        install(context, records, monkeypatch)
        page = context.new_page()
        page.goto("data:text/html," + quote(BUTTONS))
        page.evaluate("""() => {
          const record = window.__ikiRecordClickStability;
          window.__ikiRecordClickStability = (hit) => (window.recorded = record(hit));
        }""")
        page.evaluate(NEXT_FRAMES)
        yield page, records


def collected(page: Page) -> None:
    # Input dispatch can finish before Playwright delivers the binding call to Python.
    page.evaluate("() => window.recorded")


def test_stable_press(detector_page: tuple[Page, list[dict[str, Any]]]) -> None:
    page, records = detector_page
    page.mouse.click(40, 30)
    collected(page)
    assert records == []


def test_press_pairing_and_cancel(detector_page: tuple[Page, list[dict[str, Any]]]) -> None:
    page, records = detector_page
    press = page.locator("#press")
    release = page.locator("#release")
    first = {"pointerId": 10, "bubbles": True}
    second = {"pointerId": 20, "bubbles": True}
    press.dispatch_event("pointerdown", first)
    release.dispatch_event("pointerup", second)
    press.dispatch_event("pointercancel", first)
    release.dispatch_event("pointerup", first)
    assert records == []

    press.dispatch_event("pointerdown", first)
    release.dispatch_event("pointerdown", second)
    release.dispatch_event("pointerup", second)
    release.dispatch_event("pointerup", first)
    collected(page)
    (hit,) = records
    assert hit["mismatched"]
    assert hit["down"]["name"] == "Press"
    assert hit["up"]["name"] == "Release"


def test_composed_ancestor_release(detector_page: tuple[Page, list[dict[str, Any]]]) -> None:
    page, records = detector_page
    page.evaluate("""() => {
      const host = document.body.appendChild(document.createElement('div'));
      const child = host.attachShadow({ mode: 'open' }).appendChild(document.createElement('span'));
      const options = { pointerId: 10, bubbles: true, composed: true };
      child.dispatchEvent(new PointerEvent('pointerdown', options));
      host.dispatchEvent(new PointerEvent('pointerup', options));
      host.dispatchEvent(new PointerEvent('pointerdown', options));
      child.dispatchEvent(new PointerEvent('pointerup', options));
    }""")
    collected(page)
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


@pytest.mark.parametrize("resize", [False, True], ids=["style-change", "viewport-change"])
def test_attributed_shift_between_pointer_events(
    detector_page: tuple[Page, list[dict[str, Any]]], detector_browser: Browser, resize: bool
) -> None:
    page, records = detector_page
    # Chromium can buffer shifts during a native press and publish only an aggregate score
    # without sources on release. Use DOM pointer events to exercise an attributed shift.
    page.mouse.click(300, 30)
    press = page.locator("#press")
    press.dispatch_event(
        "pointerdown", {"pointerId": 10, "bubbles": True, "clientX": 40, "clientY": 30}
    )
    if resize:
        page.set_viewport_size({"width": 700, "height": 700})
    else:
        page.evaluate(MOVE)
    page.evaluate(NEXT_FRAMES)
    press.dispatch_event("pointerup", {"pointerId": 10, "bubbles": True})
    collected(page)
    if detector_browser.browser_type.name == "chromium":
        (hit,) = records
        assert not hit["mismatched"]
        assert hit["movingTarget"]
        (shift,) = hit["shifts"]
        assert shift["element"]["name"] == "Press"
        assert hit["down"]["at"] <= shift["at"] <= hit["up"]["at"]
        assert shift["phase"] == "in-press"
        assert shift["suppressedBy"] is None
        assert (shift["testResize"] is not None) == resize
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
    collected(page)

    if detector_browser.browser_type.name == "chromium":
        (hit,) = records
        assert not hit["mismatched"]
        assert hit["movingTarget"]
        assert hit["down"]["name"] == hit["up"]["name"] == "Press"
        (shift,) = hit["shifts"]
        assert 0 < hit["down"]["at"] - shift["at"] <= 500
        assert shift["phase"] == "pre-press"
        assert shift["hitAreaChanged"] is True
        assert shift["suppressedBy"] is None
    else:
        assert records == []


def test_pre_press_shifts_that_do_not_change_the_hit_area(
    detector_page: tuple[Page, list[dict[str, Any]]], detector_browser: Browser
) -> None:
    page, records = detector_page
    press = page.locator("#press")
    press.evaluate("button => { button.style.left = '26px'; }")
    page.evaluate(NEXT_FRAMES)
    page.mouse.click(80, 30)
    collected(page)
    if detector_browser.browser_type.name == "chromium":
        (hit,) = records
        assert not hit["movingTarget"]
        (shift,) = hit["shifts"]
        assert shift["hitAreaChanged"] is False
        assert shift["suppressedBy"] == "hit-area-unchanged"
    else:
        assert records == []


def test_descendant_shift_cannot_establish_a_control_hit_area_change(
    detector_page: tuple[Page, list[dict[str, Any]]], detector_browser: Browser
) -> None:
    page, records = detector_page
    page.locator("#press").evaluate("""button => {
      button.innerHTML = '<span style="position:absolute;left:8px;top:8px">Press</span>';
    }""")
    page.evaluate(NEXT_FRAMES)
    page.locator("#press span").evaluate("label => { label.style.left = '50px'; }")
    page.evaluate(NEXT_FRAMES)
    page.mouse.click(35, 48)
    collected(page)
    if detector_browser.browser_type.name == "chromium":
        (hit,) = records
        assert not hit["movingTarget"]
        (shift,) = hit["shifts"]
        assert shift["element"]["tag"] == "span"
        assert shift["hitAreaChanged"] is None
        assert shift["suppressedBy"] == "hit-area-unattributed"
    else:
        assert records == []


@pytest.mark.parametrize("resize_event", [True, False], ids=["event", "viewport-confirmation"])
def test_viewport_resize_suppresses_a_pre_press_shift(
    detector_page: tuple[Page, list[dict[str, Any]]],
    detector_browser: Browser,
    resize_event: bool,
) -> None:
    page, records = detector_page
    if not resize_event:
        if detector_browser.browser_type.name != "chromium":
            pytest.skip("Only Chromium records layout shifts around a test resize")
        page.evaluate("window.addEventListener('resize', e => e.stopImmediatePropagation(), true)")
    page.set_viewport_size({"width": 700, "height": 700})
    page.evaluate(NEXT_FRAMES)
    page.mouse.click(220, 30)
    collected(page)
    if detector_browser.browser_type.name == "chromium":
        (hit,) = records
        assert not hit["movingTarget"]
        (shift,) = hit["shifts"]
        assert shift["phase"] == "pre-press"
        assert shift["hitAreaChanged"] is True
        assert shift["suppressedBy"] == "test-resize"
        assert shift["testResize"]["before"]["width"] == 1280
        assert shift["testResize"]["after"]["width"] == 700
        assert shift["from"]["x"] == 16
        assert shift["to"]["x"] == 200
    else:
        assert records == []


@pytest.mark.parametrize(("mode", "mismatched"), [(None, False), (None, True), ("report", True)])
def test_finish_writes_reports_and_only_fails_for_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None, mismatched: bool
) -> None:
    if mode is None:
        monkeypatch.delenv("IKI_CLICK_STABILITY", raising=False)
    else:
        monkeypatch.setenv("IKI_CLICK_STABILITY", mode)
    button = {"tag": "button", "role": "button", "name": "Pressed", "path": "button", "at": 1.0}
    records = [
        {
            "mismatched": mismatched,
            "movingTarget": True,
            "url": "about:blank",
            "down": button,
            "up": {**button, "name": "Released"},
        }
    ]
    if mismatched and mode is None:
        with pytest.raises(pytest.fail.Exception, match="different press and release targets"):
            finish(tmp_path, "example", "chromium", records)
    else:
        finish(tmp_path, "example", "chromium", records)
    (report,) = (tmp_path / ".pytest_cache" / "click-stability").glob("*.json")
    assert json.loads(report.read_text()) == {
        "test": "example",
        "engine": "chromium",
        "records": records,
    }

"""Trusted key events at scanner speed, with failure diagnostics for the fixture labels."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import pairwise
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error, Frame, Page, Request

_WATCH = """
({code, path, gaps}) => {
  if (window !== window.top || location.pathname !== path) return;
  const expected = [...code, "Enter"];
  const keys = [];
  let deviceMs = 0;
  // Keep a trace across a full navigation, including an accidental activation of a link.
  const storageKey = "iki-test-scan-" + code;
  window.__readStorageScan = () => keys;
  window.addEventListener("pagehide", () => {
    if (keys.length) sessionStorage.setItem(storageKey, JSON.stringify(keys));
  }, {once: true});
  function keydown(event) {
    if (event.key === "Shift") return;
    const row = {
      expected: event.key === expected[keys.length], trusted: event.isTrusted,
      nativeMs: event.timeStamp, target: event.target?.localName ?? null
    };
    if (row.expected && row.trusted) {
      // Preserve native key dispatch, focus and Enter activation. Only the device clock for
      // this scripted label is independent of how long the browser driver takes between keys.
      deviceMs = keys.length ? deviceMs + gaps[keys.length - 1] : Math.max(1, row.nativeMs);
      Object.defineProperty(event, "timeStamp", {value: deviceMs});
      row.deviceMs = event.timeStamp;
    }
    keys.push(row);
    if (event.key === "Enter" || keys.length === expected.length) {
      window.removeEventListener("keydown", keydown, true);
      setTimeout(() => { row.defaultPrevented = event.defaultPrevented; }, 0);
    }
  }
  window.addEventListener("keydown", keydown, {capture: true, passive: true});
}
"""


_READ_KEYS = """
code => {
  const here = window.__readStorageScan?.();
  if (here?.length) return here;
  return JSON.parse(sessionStorage.getItem("iki-test-scan-" + code) || "[]");
}
"""


def _lookup(request: Request, code: str, sent_ms: int) -> dict[str, object]:
    query = parse_qs(urlsplit(request.url).query).get("code", [""])[0]
    response = request.existing_response
    timing = request.timing
    result: dict[str, object] = {
        "code": query if query and code.endswith(query) else "other",
        "sent_ms": sent_ms,
        "status": response.status if response else None,
        "response_elapsed_ms": timing["responseEnd"],
        "failed": bool(request.failure),
    }
    if response and response.ok and timing["responseEnd"] >= 0:
        # Only the count and slots from this fixture, never the response body.
        items = response.json()["items"]
        result["matches"] = len(items)
        result["positions"] = [item.get("position") for item in items]
    return result


@contextmanager
def observe_scan(
    page: Page,
    code: str,
    path: str,
    expected: str,
    *,
    new_document: bool = False,
    gaps_ms: tuple[int, ...] | None = None,
) -> Iterator[None]:
    """Watch the fixture scan without inserting a Playwright call between focus and typing."""
    gaps = gaps_ms if gaps_ms is not None else (5,) * len(code)
    assert len(gaps) == len(code), "Supply one interval per character through Enter"
    script = f"({_WATCH})({json.dumps({'code': code, 'path': path, 'gaps': gaps})})"
    if new_document:
        page.add_init_script(script=script)
    else:
        page.evaluate(script)
    started = monotonic()
    lookups: list[tuple[Request, int]] = []
    navigations: list[tuple[int, str]] = []

    def since_start() -> int:
        return round((monotonic() - started) * 1000)

    def requested(request: Request) -> None:
        if urlsplit(request.url).path == "/api/v1/storage/scan":
            lookups.append((request, since_start()))

    def navigated(frame: Frame) -> None:
        if frame == page.main_frame:
            navigations.append((since_start(), frame.url))

    page.on("request", requested)
    page.on("framenavigated", navigated)
    try:
        yield
        keys = page.evaluate(_READ_KEYS, code)
        assert len(keys) == len(code) + 1 and all(
            row["expected"] and row["trusted"] for row in keys
        )
        delivered = [round(b["deviceMs"] - a["deviceMs"]) for a, b in pairwise(keys)]
        assert delivered == list(gaps), "The detector must receive the scripted device intervals"
    except Exception as error:
        error.add_note(
            f"Scan {code!r} expected {expected}. At {since_start()} ms: {page.url}. "
            f"Navigations (ms, URL): {navigations}"
        )
        try:
            keys = page.evaluate(_READ_KEYS, code)
            observation = page.evaluate(
                """() => {
                  return {retry: !!document.querySelector(".scan-bar [role=alert]"),
                    noMatch: [...document.querySelectorAll("[role=alert]")].some(
                      el => el.textContent.includes("Nothing you can read has the code")
                    )};
                }"""
            )
            error.add_note(
                f"Fixture keydowns ({len(keys)}/{len(code) + 1}): {keys}. "
                f"Retry shown: {observation['retry']}. No match shown: {observation['noMatch']}."
            )
            found = [_lookup(request, code, ms) for request, ms in lookups]
            error.add_note(f"GET /api/v1/storage/scan: {found}")
        except (Error, KeyError, ValueError) as unavailable:
            error.add_note(f"Scanner diagnostics unavailable: {type(unavailable).__name__}")
        raise
    finally:
        page.remove_listener("request", requested)
        page.remove_listener("framenavigated", navigated)

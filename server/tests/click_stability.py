import json
import os
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Error, Page, ViewportSize

SCRIPT = Path(__file__).with_suffix(".js")


def install(
    context: BrowserContext, records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def record(source: dict[str, Any], hit: dict[str, Any]) -> None:
        # Keep the URL from pointerdown in case the document is gone by teardown.
        hit["page"] = hit["url"] if source["frame"].parent_frame is None else source["page"].url
        records.append(hit)

    context.expose_binding("__ikiRecordClickStability", record)
    context.add_init_script(path=SCRIPT)

    def watch_viewport(page: Page) -> None:
        set_viewport_size = page.set_viewport_size

        def resize(viewport_size: ViewportSize) -> None:
            if viewport_size != page.viewport_size:
                for frame in page.frames:
                    # A document can navigate or detach before the viewport changes.
                    with suppress(Error):
                        frame.evaluate("size => window.__ikiMarkTestResize?.(size)", viewport_size)
            set_viewport_size(viewport_size)

        monkeypatch.setattr(page, "set_viewport_size", resize)

    if context.browser and context.browser.browser_type.name == "chromium":
        context.on("page", watch_viewport)


def _element(element: dict[str, Any]) -> str:
    role = f" role={element['role']}" if element["role"] else ""
    return f"<{element['tag']}{role}> {element['name']!r} ({element['path']})"


def finish(root: Path, nodeid: str, engine: str, records: list[dict[str, Any]]) -> None:
    mode = os.environ.get("IKI_CLICK_STABILITY", "fail")
    if mode not in {"fail", "report"}:
        pytest.fail(f"Set IKI_CLICK_STABILITY to report or fail (got {mode!r})", pytrace=False)

    directory = root / ".pytest_cache" / "click-stability"
    directory.mkdir(parents=True, exist_ok=True)
    name = sha256(nodeid.encode()).hexdigest()
    report = {"test": nodeid, "engine": engine, "records": records}
    (directory / f"{name}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    mismatches = [hit for hit in records if hit["mismatched"]]
    if mode == "report" or not mismatches:
        return

    lines = [f"Click stability ({engine})"]
    for hit in mismatches:
        down, up = hit["down"], hit["up"]
        lines.append(f"  {hit['url']} — different press and release targets")
        lines.append(f"    down {down['at']:.1f} ms {_element(down)}")
        lines.append(f"    up   {up['at']:.1f} ms {_element(up)}")
    pytest.fail("\n".join(lines), pytrace=False)

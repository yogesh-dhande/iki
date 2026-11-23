import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import BrowserContext

SCRIPT = Path(__file__).with_suffix(".js")


def install(context: BrowserContext, records: list[dict[str, Any]]) -> None:
    def record(source: dict[str, Any], hit: dict[str, Any]) -> None:
        # The document may be gone by teardown; keep the URL from pointerdown.
        hit["page"] = hit["url"] if source["frame"].parent_frame is None else source["page"].url
        records.append(hit)

    context.expose_binding("__ikiRecordClickStability", record)
    context.add_init_script(path=SCRIPT)


def _element(element: dict[str, Any]) -> str:
    role = f" role={element['role']}" if element["role"] else ""
    return f"<{element['tag']}{role}> {element['name']!r} ({element['path']})"


def finish(request: pytest.FixtureRequest, engine: str, records: list[dict[str, Any]]) -> None:
    # Report by default while measuring the detector across the suite.
    mode = os.environ.get("IKI_CLICK_STABILITY", "report")
    if mode == "report":
        directory = request.config.rootpath / ".pytest_cache" / "click-stability"
        directory.mkdir(parents=True, exist_ok=True)
        nodeid = request.node.nodeid
        name = sha256(nodeid.encode()).hexdigest()
        report = {"test": nodeid, "engine": engine, "records": records}
        (directory / f"{name}.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        return
    if mode != "fail":
        pytest.fail(f"Unknown IKI_CLICK_STABILITY mode {mode!r}; use report or fail", pytrace=False)
    if not records:
        return

    lines = [f"Click stability ({engine}):"]
    for hit in records:
        down, up = hit["down"], hit["up"]
        reason = "different press and release targets" if hit["mismatched"] else "moving target"
        lines.append(f"  {hit['url']} — {reason}")
        lines.append(f"    down {down['at']:.1f} ms {_element(down)}")
        lines.append(f"    up   {up['at']:.1f} ms {_element(up)}")
        for shift in hit["shifts"]:
            before, after = shift["from"], shift["to"]
            lines.append(
                f"    shift {shift['at']:.1f} ms {_element(shift['element'])}, "
                f"({before['x']:.1f}, {before['y']:.1f}) → ({after['x']:.1f}, {after['y']:.1f}), "
                f"score {shift['value']:.5g}, recent input={shift['hadRecentInput']}"
            )
    pytest.fail("\n".join(lines), pytrace=False)

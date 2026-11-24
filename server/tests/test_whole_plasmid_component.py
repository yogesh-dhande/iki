# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The whole-plasmid alignment's Map tab: public pUC19 reads' depth and differences drawn on
the plasmid's ring and line, said in words and tabulated, at a phone's width and a desktop's,
in either color scheme, passing axe's WCAG 2.2 AA checks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, serving
from playwright.sync_api import Browser, expect
from test_browser import AXE, RUN_AXE, WCAG_22_AA, browser, playwright  # noqa: F401 - fixtures

PLUGIN = PLUGINS / "plasmid_sequencing"
MODULE = "whole-plasmid-map.js"


def loaded(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    found = importlib.util.module_from_spec(spec)
    sys.modules[name] = found
    spec.loader.exec_module(found)
    return found


def result(scenario: str, tmp_path: Path) -> dict[str, Any]:
    """What the action records for a scenario of its own tests, on minimap2's recorded
    answers: the public reads, or a synthetic consensus."""
    replay = loaded("plasmid_sequencing_replay", PLUGIN / "tests/replay.py")
    script = replay.module()
    answers = replay.replaying()
    script.minimap2 = answers
    script.minimap2_version = lambda: answers.version
    given, out_dir = replay.scenarios(tmp_path)[scenario]
    (created,) = replay.run(script, given, out_dir)["creates"]
    return {"hrid": "WPA-000001", "values": created["values"]}


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1440])
def test_public_puc19_reads_are_drawn_on_the_plasmid(
    browser: Browser, scheme: str, width: int, tmp_path: Path
) -> None:
    entity = result("reads", tmp_path)
    with serving(PLUGIN / "components", {MODULE: "whole-plasmid-map"}) as origin:
        page = open_frame(
            browser,
            origin,
            MODULE,
            entity,
            viewport={"width": width, "height": 900},
            color_scheme=scheme,
            bypass_csp=True,
        )
        expect(page.get_by_role("heading", name="Map")).to_be_visible()
        said = page.get_by_text("A circle of 2,686 base pairs")
        expect(said).to_contain_text("with 3 features of its own: lac promoter, 508 to 591")
        expect(said).to_contain_text("55 reads deep on average, 46 at the least")
        expect(said).to_contain_text("6 differences from the record")
        expect(said).to_contain_text("o.887del at 887 in ori (rep_origin)")
        differences = page.get_by_role("region", name="Differences")
        expect(differences.get_by_role("row")).to_have_count(7)
        expect(
            differences.get_by_role("cell", name="54 of 54, 100% (32 forward, 22 reverse)")
        ).to_have_count(2)
        expect(differences.get_by_role("cell", name="7 bases")).to_be_visible()
        expect(page.get_by_role("region", name="Read depth")).to_contain_text("10 reads or more")
        assert (
            page.locator("whole-plasmid-map").evaluate(
                "e => e.shadowRoot.querySelectorAll('path.depth').length"
            )
            == 1
        )
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []

        page.get_by_role("button", name="Linear").click()
        expect(page.get_by_role("button", name="Linear")).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_text("drawn as a line cut at base 1")).to_be_visible()
        page.get_by_role("button", name="Read depth").click()
        expect(page.get_by_role("region", name="Read depth")).to_have_count(0)
        assert (
            page.locator("whole-plasmid-map").evaluate(
                "e => e.shadowRoot.querySelectorAll('path.depth').length"
            )
            == 0
        )
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        assert page.evaluate("window.refused") == []
        assert ERRORS[page] == []
        page.context.close()


def test_a_linear_consensus_that_stops_short_shows_where(browser: Browser, tmp_path: Path) -> None:
    entity = result("linear-short", tmp_path)
    with serving(PLUGIN / "components", {MODULE: "whole-plasmid-map"}) as origin:
        page = open_frame(
            browser, origin, MODULE, entity, viewport={"width": 390, "height": 844}, bypass_csp=True
        )
        said = page.get_by_text("A linear molecule of 2,686 base pairs")
        expect(said).to_contain_text("under the minimum depth of 1 at 2,637–2,686")
        expect(said).to_contain_text("with no differences from the record")
        # A linear molecule has no ring to draw.
        expect(page.get_by_role("button", name="Circular")).to_have_count(0)
        page.evaluate(AXE.read_text())
        assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
        assert ERRORS[page] == []
        page.context.close()

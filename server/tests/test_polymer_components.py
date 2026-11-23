# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The polymer registry's Structure tab in the sandboxed component frame."""

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
ELEMENT = "polymer-structure"
MODULE = "structure.js"
INSPECT = "iki/polymer.inspect_bigsmiles"
TAB = {"slot": "entity.tab", "label": "Structure"}
STYRENE = "00000000-0000-0000-0000-000000000001"
# NIST SRM 1478, as represented in the seed: styrene is a registered source compound,
# and the repeat fragment is polymerized styrene. The lot holds the molecular masses.
# https://tsapps.nist.gov/srmext/certificates/1478.pdf
POLYSTYRENE: dict[str, Any] = {
    "hrid": "POL-000001",
    "type": "iki/polymer.Polymer",
    "refs": {STYRENE: {"hrid": "CPD-000001", "title": "Styrene"}},
    "values": {
        "architecture": "linear",
        "sequence_distribution": "homopolymer",
        "monomers": [STYRENE],
        "bigsmiles": "{[][<]CC(c1ccccc1)[>][]}",
        "notation_status": "checked",
        "repeat_units": [{"object": 1, "fragment": "[<]CC(c1ccccc1)[>]"}],
        "implicit_end_groups": [],
    },
}


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    with serving(PLUGINS / "polymer" / "components", {MODULE: ELEMENT}) as origin:
        yield origin


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_structure_preserves_the_notation_and_distinguishes_monomers(
    browser: Browser, frame_server: str, scheme: str
) -> None:
    page = open_frame(
        browser,
        frame_server,
        MODULE,
        POLYSTYRENE,
        viewport={"width": 390, "height": 844},
        color_scheme=scheme,
        bypass_csp=True,  # allow axe to check the frame
    )
    source = page.get_by_role("region", name="Source monomers")
    source.get_by_text("CPD-000001 · Styrene", exact=True).wait_for()
    notation = page.get_by_role("region", name="BigSMILES")
    assert notation.get_by_text("{[][<]CC(c1ccccc1)[>][]}", exact=True).is_visible()
    assert notation.get_by_text("[<]CC(c1ccccc1)[>]", exact=True).is_visible()
    assert notation.get_by_role("heading", name="Stochastic object 1").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()


def test_repeat_units_and_implicit_ends_stay_with_their_stochastic_object(
    browser: Browser, frame_server: str
) -> None:
    # Nylon-6,6 with the two implicit end groups in the authors' v1.1 grammar.
    # https://olsenlabmit.github.io/BigSMILES/docs/line_notation.html#end-groups
    acid, amine = "[<]C(=O)CCCCC(=O)[<]", "[>]NCCCCCCN[>]"
    hydroxyl, hydrogen = "[>]O[H]", "[<][H]"
    values = {
        "architecture": "linear",
        "bigsmiles": "{[]" + acid + "," + amine + ";" + hydroxyl + "," + hydrogen + "[]}",
        "notation_status": "checked",
        "end_groups": "Carboxylic acid and amine",
        "repeat_units": [{"object": 1, "fragment": frag} for frag in (acid, amine)],
        "implicit_end_groups": [{"object": 1, "fragment": frag} for frag in (hydroxyl, hydrogen)],
    }
    page = open_frame(browser, frame_server, MODULE, {"values": values})
    page.get_by_role("heading", name="Stochastic object 1").wait_for()
    notation = page.get_by_role("region", name="BigSMILES")
    assert (
        notation.locator("dt").all_text_contents()
        == ["Repeat unit"] * 2 + ["Implicit end group"] * 2
    )
    assert notation.locator("dd").all_text_contents() == [acid, amine, hydroxyl, hydrogen]
    assert page.get_by_text("Carboxylic acid and amine", exact=True).is_visible()
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_external_notation_remains_readable_when_inspection_is_unsupported(
    browser: Browser, frame_server: str
) -> None:
    reason = "Fragment names are outside this inspector's supported syntax."
    # Fragment named by the submitter. Registration preserves the exact original text.
    raw = "{[][<][#A][>][]}.{#A=CC}"
    values = {
        **POLYSTYRENE["values"],
        "bigsmiles": raw,
        "notation_status": "unsupported",
        "notation_note": reason,
        "repeat_units": None,
    }
    page = open_frame(browser, frame_server, MODULE, {"values": values, "refs": {}})
    page.get_by_text(raw, exact=True).wait_for()
    assert page.get_by_text(reason, exact=True).is_visible()
    assert page.get_by_text("Unavailable reference").is_visible()
    assert page.get_by_text(STYRENE, exact=True).count() == 0
    assert page.get_by_role("heading", name="Stochastic object 1").count() == 0
    button = page.get_by_role("button", name="Inspect BigSMILES")
    assert button.count() == 0
    place(page, ELEMENT, {**TAB, "runs": [INSPECT]})
    button.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.ran") == [INSPECT]
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()

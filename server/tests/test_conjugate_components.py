# ruff: noqa: F811 - browser fixtures imported by name are parameters here.
"""The carrier, attachment sites and composition as served in Iki's component frame."""

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

ELEMENT = "conjugate-composition"
MODULE = "composition.js"
COMPUTE = "iki/conjugate.compute_composition"
TAB = {"slot": "entity.tab", "label": "Composition"}
CARRIER = "00000000-0000-0000-0000-000000000001"
PART = "00000000-0000-0000-0000-000000000002"
BASIS = "Neutral free-acid carrier and declared attachments, after each recorded atom loss."
# The deposited adenylated DNA in PDB 8V1W, chain C. The action calculates the neutral
# free-acid masses. The tab formats those results. https://www.rcsb.org/structure/8V1W
VALUES = {
    "carrier": [{"kind": "oligo_conjugate", "molecule": CARRIER}],
    "attachments": [{"part": PART, "copies": 1, "site": "5′ phosphate", "loss_formula": "H2O"}],
    "molecular_formula": "C78H99N33O49P8",
    "molecular_weight": 2530.62,
    "monoisotopic_mass": 2529.417,
    "composition_note": BASIS,
}
ENTITY: dict[str, Any] = {
    "hrid": "CJG-000001",
    "type": "iki/conjugate.Conjugate",
    "values": VALUES,
    "refs": {
        CARRIER: {"hrid": "OLI-000001", "title": "5′-phosphorylated GTCGGAC"},
        PART: {"hrid": "CPD-000001", "title": "Adenosine monophosphate"},
    },
}


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    with serving(PLUGINS / "conjugate" / "components", {MODULE: ELEMENT}) as origin:
        yield origin


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_composition_and_sites_on_a_narrow_screen(
    browser: Browser, frame_server: str, scheme: str
) -> None:
    page = open_frame(
        browser,
        frame_server,
        MODULE,
        ENTITY,
        viewport={"width": 390, "height": 844},
        color_scheme=scheme,
        bypass_csp=True,  # inject axe in the sandbox
    )
    page.get_by_role("heading", name="Oligonucleotide conjugate").wait_for()
    assert page.get_by_text("OLI-000001 · 5′-phosphorylated GTCGGAC").is_visible()
    attachment = page.get_by_role("list", name="Attachments").get_by_role("listitem")
    assert attachment.locator("p").inner_text() == "CPD-000001 · Adenosine monophosphate"
    assert attachment.locator("dd").all_text_contents() == ["5′ phosphate", "1", "H2O"]
    assert page.get_by_text("C78H99N33O49P8").is_visible()
    assert page.get_by_text("2,530.62 u", exact=True).is_visible()
    assert page.get_by_text("2,529.417 u", exact=True).is_visible()
    assert page.get_by_text(BASIS).is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()


def test_an_unresolved_attachment_and_the_permitted_action(
    browser: Browser, frame_server: str
) -> None:
    # An omitted count is an unknown load. An unreadable payload's UUID stays concealed.
    reason = "The number of copies is unspecified."
    values = {
        "carrier": [{"kind": "pegylated_protein", "molecule": CARRIER}],
        "attachments": [{"part": PART, "site": "unlocalized", "peg_grade": "Synthetic PEG grade"}],
        "composition_note": reason,
    }
    entity = {**ENTITY, "values": values, "refs": {CARRIER: {"hrid": "PRT-000001"}}}
    page = open_frame(browser, frame_server, MODULE, entity)
    page.get_by_role("heading", name="PEGylated protein").wait_for()
    assert page.get_by_text("PRT-000001", exact=True).is_visible()
    assert page.get_by_text("Unavailable reference").is_visible()
    assert page.get_by_text(PART, exact=True).count() == 0
    assert page.get_by_text("Unspecified", exact=True).is_visible()
    assert page.get_by_text("Unknown", exact=True).is_visible()
    assert page.get_by_text(reason).is_visible()
    assert page.get_by_text("Synthetic PEG grade").is_visible()
    assert page.get_by_text("Average mass").count() == 0
    button = page.get_by_role("button", name="Compute composition")
    assert button.count() == 0
    place(page, ELEMENT, {**TAB, "runs": [COMPUTE]})
    button.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.ran") == [COMPUTE]
    place(page, ELEMENT, {**TAB, "runs": []})
    assert button.count() == 0
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()

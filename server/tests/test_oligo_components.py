# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The oligo plugin's components, in each browser, under the policy Iki's component frames
have (ADR 0084): the Chemistry tab draws an oligonucleotide's nucleotides from the
positions its actions computed, and the Pairing tab a duplex's strands antiparallel. Each
is named for screen readers as it's drawn for the eye, and passes axe's WCAG 2.2 AA
checks in light and dark. The frame is served as Iki's is (component_frames)."""

from collections.abc import Iterator
from typing import Any

import pytest
from component_frames import ERRORS, PLUGINS, open_frame, serving
from playwright.sync_api import Browser
from test_browser import (  # noqa: F401 - fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    browser_name,
    playwright,
)

# Browser tests: CI runs them once, where it installs the browsers (see ci.yml's matrix).
pytestmark = pytest.mark.built_web_app


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    elements = {"chemistry.js": "oligo-chemistry", "duplex.js": "oligo-duplex"}
    with serving(PLUGINS / "oligo" / "components", elements) as origin:
        yield origin


def _brief(symbol: str, name: str, *, modified: bool) -> dict[str, Any]:
    return {"symbol": symbol, "name": name, "modified": modified}


MOE = _brief("MOE", "2′-O-Methoxyethylribose", modified=True)
DEOXY = _brief("dR", "2′-Deoxyribose", modified=False)
PS = _brief("sP", "Phosphorothioate", modified=True)
#: A 5′-6-FAM, MOE-wing gapmer's first three nucleotides, as the plugin's actions compute
#: its positions.
GAPMER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "hrid": "OLI-000001",
    "type": "iki/oligo.Oligonucleotide",
    "values": {
        "name": "a gapmer",
        "positions": {
            "nucleotides": [
                {
                    "position": 1,
                    "base": "C",
                    "sugar": MOE,
                    "nucleobase": _brief("5meC", "5-Methylcytosine", modified=True),
                    "linker": PS,
                    "others": [],
                },
                {
                    "position": 2,
                    "base": "A",
                    "sugar": MOE,
                    "nucleobase": _brief("A", "Adenine", modified=False),
                    "linker": PS,
                    "others": [],
                },
                {
                    "position": 3,
                    "base": "G",
                    "sugar": DEOXY,
                    "nucleobase": _brief("G", "Guanine", modified=False),
                    "linker": None,
                    "others": [],
                },
            ],
            "ends": {"5′": [_brief("5FAM", "6-FAM", modified=True)]},
        },
    },
}
#: Patisiran's strands, as register_duplex finds them pairing.
PATISIRAN = {
    "id": "00000000-0000-0000-0000-000000000002",
    "hrid": "DUP-000001",
    "type": "iki/oligo.Duplex",
    "values": {
        "duplex_length": 19,
        "mismatches": 0,
        "pairing": {
            "length": 19,
            "mismatches": 0,
            "sense": {"bases": "GUAACCAAGAGUAUUCCAUTT", "from": 1, "to": 19},
            "antisense": {"bases": "AUGGAAUACUCUUGGUUACTT", "from": 1, "to": 19},
            "sense_overhang_5": 0,
            "sense_overhang_3": 2,
            "antisense_overhang_5": 0,
            "antisense_overhang_3": 2,
            "pairs": [{"sense": i, "antisense": 20 - i, "paired": True} for i in range(1, 20)],
        },
    },
}


def test_the_chemistry_tab_names_each_nucleotide(browser: Browser, frame_server: str) -> None:
    page = open_frame(browser, frame_server, "chemistry.js", GAPMER)
    strand = page.get_by_role("list", name="Nucleotides, 5′ to 3′")
    strand.wait_for(timeout=30_000)
    items = strand.get_by_role("listitem")
    assert items.count() == 4
    assert items.nth(0).get_attribute("aria-label") == "5′ end: 6-FAM"
    first = page.get_by_role(
        "listitem",
        name="Position 1; 5-Methylcytosine, modified on 2′-O-Methoxyethylribose, modified; "
        "then Phosphorothioate, modified",
    )
    assert first.count() == 1
    assert page.get_by_role("listitem", name="Position 3; Guanine on 2′-Deoxyribose").count() == 1
    page.get_by_role("group").get_by_text("Each position").click()
    rows = page.get_by_role("row")
    assert rows.count() == 4
    assert page.get_by_role("rowheader", name="2").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_the_pairing_tab_says_what_it_draws(browser: Browser, frame_server: str) -> None:
    page = open_frame(browser, frame_server, "duplex.js", PATISIRAN)
    heading = page.get_by_role("heading", name="Pairing")
    heading.wait_for(timeout=30_000)
    assert page.get_by_text(
        "19 base pairs, no mismatches. Sense strand: 0-nucleotide 5′ and 2-nucleotide 3′ "
        "overhang. Antisense strand: 0-nucleotide 5′ and 2-nucleotide 3′ overhang."
    ).is_visible()
    assert page.get_by_role("definition").filter(has_text="sense 1–19, antisense 1–19").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_a_duplex_whose_strands_dont_pair_says_so(browser: Browser, frame_server: str) -> None:
    entity = {"hrid": "DUP-000002", "values": {"pairing": {"length": 0, "pairs": []}}}
    page = open_frame(browser, frame_server, "duplex.js", entity)
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert "don't pair" in (status.text_content() or "")
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("module", "entity"), [("chemistry.js", GAPMER), ("duplex.js", PATISIRAN)])
def test_they_pass_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: str, module: str, entity: object
) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = open_frame(browser, frame_server, module, entity, color_scheme=scheme, bypass_csp=True)
    page.get_by_role("heading").first.wait_for(timeout=30_000)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

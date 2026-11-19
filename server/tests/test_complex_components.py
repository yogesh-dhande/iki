# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The complex plugin's Composition tab, in each browser, under the policy Iki's component
frames have (ADR 0084): a complex's parts with their roles and copies, its formula, masses
and absorbance at 280 nm as compute_composition computed them, and with its disulfides,
worked out from its bonds as they are (ADR 0161). It's named for screen readers as it's
drawn for the eye, and passes axe's WCAG 2.2 AA checks in light and dark. The frame is
served as Iki's is (component_frames).

Emicizumab's values are its chains' (KEGG DRUG D10821), as the antibody plugin's
assemble_multispecific and the complex plugin's compute_composition give them: two heavy
chains, one copy each, and two of the light chain both arms share; its parts as they are,
C6434H9972N1724O2047S45, 145,669.27 u, and 209,370 L/(mol.cm) at 280 nm (Pace's, as
Biopython's ProtParam gives its chains: 83,770, 73,800 and 25,900 twice); with KEGG's 16
disulfides, four between its chains and each domain's own, 32 hydrogens fewer,
C6434H9940N1724O2047S45, 145,637.02 u, and 211,370 L/(mol.cm)."""

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


def cells(row: Any) -> list[str]:
    """A table row's cells' text, as each browser has it."""
    return [c.text_content() or "" for c in row.locator("th, td").all()]


H1, H2, L = (f"00000000-0000-7000-8000-00000000000{n}" for n in (1, 2, 3))


def _ref(hrid: str, title: str) -> dict[str, Any]:
    return {"hrid": hrid, "title": title, "type": "iki/protein.Protein", "archived": False}


def _disulfide(part: str, copy: int, at: int, partner: str, other: int, to: int) -> dict[str, Any]:
    return {
        "kind": "disulfide",
        "part": part,
        "copy": copy,
        "residue": at,
        "partner": partner,
        "partner_copy": other,
        "partner_residue": to,
    }


EMICIZUMAB_VALUES: dict[str, Any] = {
    "name": "Emicizumab",
    "components": [
        {"part": H1, "copies": 1, "role": "heavy"},
        {"part": H2, "copies": 1, "role": "heavy"},
        {"part": L, "copies": 2, "role": "light"},
    ],
    "bonds": [
        _disulfide(H1, 1, 137, L, 1, 214),
        _disulfide(H2, 1, 133, L, 2, 214),
        _disulfide(H1, 1, 229, H2, 1, 225),
        _disulfide(H1, 1, 232, H2, 1, 228),
        *(
            _disulfide(H1, 1, a, H1, 1, b)
            for a, b in ((22, 96), (150, 206), (264, 324), (370, 428))
        ),
        *(
            _disulfide(H2, 1, a, H2, 1, b)
            for a, b in ((22, 96), (146, 202), (260, 320), (366, 424))
        ),
        *(_disulfide(L, n, a, L, n, b) for n in (1, 2) for a, b in ((23, 88), (134, 194))),
    ],
    "molecular_formula": "C6434H9972N1724O2047S45",
    "molecular_weight": 145669.27,
    "monoisotopic_mass": 145579.6642,
    "chain_count": 4,
    "extinction_coefficient_reduced": 209370,
}
EMICIZUMAB: dict[str, Any] = {
    "id": "00000000-0000-7000-8000-000000000010",
    "hrid": "MSA-000001",
    "type": "iki/antibody.MultispecificAntibody",
    "values": EMICIZUMAB_VALUES,
    "refs": {
        H1: _ref("PRT-000001", "Emicizumab anti-FIXa heavy chain"),
        H2: _ref("PRT-000002", "Emicizumab anti-FX heavy chain"),
        L: _ref("PRT-000003", "Emicizumab light chain"),
    },
}


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    with serving(
        PLUGINS / "complex" / "components", {"composition.js": "complex-composition"}
    ) as origin:
        yield origin


def test_the_composition_tab_shows_its_parts_and_masses(
    browser: Browser, frame_server: str
) -> None:
    page = open_frame(browser, frame_server, "composition.js", EMICIZUMAB)
    page.get_by_role("heading", name="Composition").wait_for(timeout=30_000)
    assert page.get_by_text("3 parts, 4 copies in all, 4 chains.").is_visible()
    parts = page.get_by_role("table", name="Parts")
    assert [cells(r) for r in parts.get_by_role("row").all()[1:]] == [
        ["PRT-000001 · Emicizumab anti-FIXa heavy chain", "heavy", "1"],
        ["PRT-000002 · Emicizumab anti-FX heavy chain", "heavy", "1"],
        ["PRT-000003 · Emicizumab light chain", "light", "2"],
    ]
    masses = page.get_by_role("table", name="Formula, masses and absorbance")
    rows = [cells(r) for r in masses.get_by_role("row").all()]
    assert rows == [
        ["", "Its parts as they are", "With its 16 disulfides"],
        ["Formula", "C6434H9972N1724O2047S45", "C6434H9940N1724O2047S45"],
        ["Average mass", "145,669.27 u (145.67 ku)", "145,637.02 u (145.64 ku)"],
        ["Monoisotopic mass", "145,579.6642 u (145.58 ku)", "145,547.4138 u (145.55 ku)"],
        ["Absorbance at 280 nm", "209,370 L/(mol·cm)", "211,370 L/(mol·cm)"],
    ]
    page.get_by_text("Its 16 bonds").click()
    # The first of its rows whose name starts so: bonds 10 to 16's do too.
    first = page.get_by_role("row", name="1 disulfide", exact=False).first
    assert cells(first) == [
        "1",
        "disulfide",
        "PRT-000001 · Emicizumab anti-FIXa heavy chain, copy 1, residue 137",
        "PRT-000003 · Emicizumab light chain, copy 1, residue 214",
    ]
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_a_complex_not_yet_computed_says_so(browser: Browser, frame_server: str) -> None:
    values = {"components": EMICIZUMAB_VALUES["components"]}
    page = open_frame(browser, frame_server, "composition.js", {**EMICIZUMAB, "values": values})
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert status.text_content() == (
        "MSA-000001 has no formula computed yet: it's computed from its parts when it's registered."
    )
    page.context.close()


def test_a_complex_without_an_absorbance_shows_none(browser: Browser, frame_server: str) -> None:
    # Hemoglobin's hemes absorb at 280 nm in ways Pace's values don't weigh, so
    # compute_composition gives it none, with its disulfides or without.
    values = {k: v for k, v in EMICIZUMAB_VALUES.items() if k != "extinction_coefficient_reduced"}
    page = open_frame(browser, frame_server, "composition.js", {**EMICIZUMAB, "values": values})
    masses = page.get_by_role("table", name="Formula, masses and absorbance")
    masses.wait_for(timeout=30_000)
    assert cells(masses.get_by_role("row").last) == ["Absorbance at 280 nm", "—", "—"]
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_passes_wcag_22_aa(browser: Browser, frame_server: str, scheme: str) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = open_frame(
        browser, frame_server, "composition.js", EMICIZUMAB, color_scheme=scheme, bypass_csp=True
    )
    page.get_by_role("heading").first.wait_for(timeout=30_000)
    page.get_by_text("Its 16 bonds").click()
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

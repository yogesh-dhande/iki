# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The DNA plugin's Changes tab, in each browser, under the policy Iki's component frames
have (ADR 0084): a site-directed mutagenesis drawn from what the run recorded -- the
changes placed along the template, what they do to the protein, and the oligos to order --
said in words for screen readers and tabulated beneath, so nothing is told by colour
alone. It passes axe's WCAG 2.2 AA checks in light and dark.

The designs it draws are real: pUC19 (GenBank L09137.2) with three changes at once, and
human beta-globin with the sickle cell change, as the plugin's own action writes them,
recorded into `data/mutagenesis.json` by `data/record_mutagenesis.py`.

The frame is served here, not by Iki, so it needs no plugin installed: its policy is the
component frame's (iki_server.components.views.frame), and it runs Iki's hardening script
first and the module by its integrity, as Iki's frame does.
"""

import base64
import hashlib
import json
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.sync_api import Browser, Page
from test_browser import (  # noqa: F401 - fixtures
    AXE,
    RUN_AXE,
    WCAG_22_AA,
    browser,
    browser_name,
    playwright,
    watch,
)

from iki_server.web import frames

# Browser tests: CI runs them once, where it installs the browsers (see ci.yml's matrix).
pytestmark = pytest.mark.built_web_app

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "plugins" / "dna" / "components"
MODULE = "mutagenesis-view.js"
ELEMENT = "mutagenesis-view"
APPLY = "iki/dna.apply_mutagenesis"
#: What the frame's policy allows (iki_server.components.views.frame), with the test's
#: own driver in place of Iki's bridge.
POLICY = (
    "default-src 'none'; script-src " + frames.HARDEN_SOURCE + " {origin}/{module} "
    "{origin}/driver.js; style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; media-src data: blob:; worker-src blob:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'; object-src 'none'; sandbox allow-scripts"
)
PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
    '<link rel="icon" href="data:,">\n'
    # As Iki's frame has it (iki_server.components.views), so dark mode is dark.
    '<meta name="color-scheme" content="light dark"><title>Changes</title>\n'
    + frames.HARDEN_TAG
    + '<script type="module" src="/{module}" integrity="{integrity}" '
    'crossorigin="anonymous"></script>\n'
    '<script type="module" src="/driver.js" crossorigin="anonymous"></script>\n'
    "</head><body></body></html>\n"
)
#: Sets the element's entity as Iki's bridge does; notes any request the policy refused,
#: and any action it asks to run (ADR 0170).
DRIVER = b"""
window.refused = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.refused.push(`${e.violatedDirective} ${e.blockedURI}`);
});
await customElements.whenDefined("mutagenesis-view");
const element = document.createElement("mutagenesis-view");
window.ran = [];
element.addEventListener("iki-run", (e) => window.ran.push(e.detail?.action));
element.entity = JSON.parse(decodeURIComponent(location.hash.slice(1)));
document.body.append(element);
"""

#: Two designs as the action writes them, recorded by data/record_mutagenesis.py.
RECORDED = json.loads((ROOT / "server" / "tests" / "data" / "mutagenesis.json").read_text())
#: pUC19 with three changes at once: the EcoRI site out, six bases out of the resistance
#: gene, a BamHI site into the origin. Six QuikChange oligos.
PLASMID = {
    "id": "00000000-0000-0000-0000-000000000001",
    "hrid": "MUT-000001",
    "type": "iki/dna.Mutagenesis",
    "values": RECORDED["plasmid"]["values"],
}
#: Human beta-globin with the sickle cell change, and the coding region said, so the
#: protein consequence is reported.
GENE = {
    "id": "00000000-0000-0000-0000-000000000002",
    "hrid": "MUT-000002",
    "type": "iki/dna.Mutagenesis",
    "values": RECORDED["gene"]["values"],
}
#: A design nobody has run yet: what the record is the moment it's created.
WAITING = {
    "id": "00000000-0000-0000-0000-000000000003",
    "hrid": "MUT-000003",
    "type": "iki/dna.Mutagenesis",
    "values": {"name": "pET28a His tag out", "changes": ["o.5000_5017del"]},
}


@pytest.fixture(scope="module")
def frame_server() -> Iterator[str]:
    module = (COMPONENTS / MODULE).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            origin = f"http://{self.headers['Host']}"
            path = self.path.split("#")[0].split("?")[0]
            if path == f"/frame/{MODULE}":
                digest = hashlib.sha256(module).digest()
                integrity = "sha256-" + base64.b64encode(digest).decode()
                body = PAGE.replace("{module}", MODULE).replace("{integrity}", integrity).encode()
                kind = "text/html; charset=utf-8"
            elif path == f"/{MODULE}":
                body, kind = module, "text/javascript"
            elif path == "/driver.js":
                body, kind = DRIVER, "text/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # A sandboxed frame is no one's origin: its modules come to it cross-origin.
            self.send_header("Access-Control-Allow-Origin", "*")
            if kind.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    POLICY.replace("{origin}", origin).replace("{module}", MODULE),
                )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


#: Each page's uncaught errors. Not its console's: Firefox logs its own favicon request,
#: which the frame's policy blocks, as an error.
ERRORS: dict[Page, list[str]] = {}


def _open(browser: Browser, frame_server: str, entity: object, **options: Any) -> Page:
    context = browser.new_context(**options)
    page = watch(context.new_page())
    ERRORS[page] = []
    page.on("pageerror", lambda error: ERRORS[page].append(str(error)))
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    page.get_by_role("table").first.wait_for(timeout=30_000)
    return page


def _place(page: Page, runs: list[str]) -> None:
    """Tells the element where it is, as Iki's bridge does: which actions the person
    viewing it may run here (ADR 0170)."""
    page.evaluate(
        f"(runs) => {{ document.querySelector('{ELEMENT}').place = "
        "{ slot: 'entity.tab', label: 'Changes', runs }; }",
        runs,
    )


def test_the_strip_says_in_words_what_it_draws(browser: Browser, frame_server: str) -> None:
    """The picture places each change on the template, so a reader who isn't looking at it
    is told how many changes there are, what each is, where it lies, and what the changes
    did to the molecule's length."""
    page = _open(browser, frame_server, PLASMID)
    assert page.get_by_text(
        "3 changes along the template's 2,686 bases: a substitution at 401, a deletion "
        "at 1,000, an insertion at 1,501, and the mutant is the template's own length."
    ).is_visible()
    # The drawing itself is hidden from screen readers, since the words and the table say it.
    assert page.locator("svg[aria-hidden=true]").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_what_the_design_made_is_above_the_strip(browser: Browser, frame_server: str) -> None:
    """The numbers a reader wants first, with the protocol and its rules named: an
    acceptance temperature by the kit manual's formula is not a nearest-neighbour melting
    temperature, so the record says which it is (ADR 0204)."""
    page = _open(browser, frame_server, PLASMID)
    terms = page.get_by_role("term")
    definitions = page.get_by_role("definition")
    said = {
        terms.nth(i).text_content(): definitions.nth(i).text_content() for i in range(terms.count())
    }
    assert said["Changes"] == "3"
    assert said["Mutant"] == "2,686 bp"
    assert said["Primers designed"] == "6"
    how = said["Designed with"] or ""
    assert "the QuikChange design" in how
    assert "Braman, Papworth and Greener, Methods Mol Biol 57:31, 1996" in how
    assert "at least 78 °C by the kit manual's formula" in how
    assert ERRORS[page] == []
    page.context.close()


def test_each_change_is_in_the_table_as_stated_and_as_hgvs_names_it(
    browser: Browser, frame_server: str
) -> None:
    """A change is applied where the lab stated it and named where HGVS puts it, as far 3'
    as the bases allow; the two differ for the BamHI site put into a GG, and a reader
    comparing a design with a sequencing report needs both columns (ADR 0204)."""
    page = _open(browser, frame_server, PLASMID)
    table = page.get_by_role("table", name="Changes (3)")
    assert table.count() == 1
    assert table.get_by_role("rowheader", name="o.401C>T", exact=True).count() == 1
    assert table.get_by_role("rowheader", name="o.1500_1501insGGATCC", exact=True).count() == 1
    row = table.get_by_role("row").filter(has_text="o.1500_1501insGGATCC")
    cells = row.get_by_role("cell")
    assert [cells.nth(i).text_content() for i in range(3)] == [
        "o.1501_1502insGATCCG",
        "an insertion",
        "+6",
    ]
    taken = table.get_by_role("row").filter(has_text="o.1000_1005del")
    assert taken.get_by_role("cell").nth(2).text_content() == "\N{MINUS SIGN}6"
    assert ERRORS[page] == []
    page.context.close()


def test_the_oligos_to_order_are_in_a_table_with_the_protocols_own_measure(
    browser: Browser, frame_server: str
) -> None:
    """The table a person copies out of: the bases in one piece, how much of each matches
    the template, and the acceptance temperature under the name of the formula that gives
    it -- never as "the Tm" (ADR 0204)."""
    page = _open(browser, frame_server, PLASMID)
    assert page.get_by_role("table", name="Primers to order (6)").count() == 1
    forward = page.get_by_role("row").filter(has_text="GACGGCCAGTGAATTTGAGCTCGGTACCCGG")
    cells = forward.get_by_role("cell")
    assert [cells.nth(i).text_content() for i in range(5)] == [
        "forward",
        "GACGGCCAGTGAATTTGAGCTCGGTACCCGG",
        "31",
        "30",
        "QuikChange Tm 81.6 °C",
    ]
    # Every oligo is there, both of each pair, and none is called a melting temperature.
    assert page.get_by_role("cell", name="QuikChange Tm", exact=False).count() == 6
    page.context.close()


def test_what_a_change_does_to_the_protein_is_in_its_own_table(
    browser: Browser, frame_server: str
) -> None:
    """Sickle cell haemoglobin: the design says where the coding region is, so the run
    reports the consequence, in HGVS's three-letter protein code."""
    page = _open(browser, frame_server, GENE)
    assert page.get_by_role("table", name="What it does to the protein").count() == 1
    assert page.get_by_role("rowheader", name="p.Glu7Val").count() == 1
    assert page.get_by_role("cell", name="missense").count() == 1
    assert ERRORS[page] == []
    page.context.close()


def test_a_design_with_no_coding_region_said_shows_no_protein_table(
    browser: Browser, frame_server: str
) -> None:
    """A plasmid whose coding region nobody named gets the changes to its DNA and nothing
    asserted about a protein."""
    page = _open(browser, frame_server, PLASMID)
    assert page.get_by_role("table", name="What it does to the protein").count() == 0
    page.context.close()


def test_a_design_not_made_yet_says_how_and_offers_the_action(
    browser: Browser, frame_server: str
) -> None:
    """A design is a record of what was asked for; where the changes landed, the mutant
    and the primers are a run's. Until the run has been the tab names the action rather
    than drawing an empty strip, and offers it where the person viewing it may run it
    (ADR 0170)."""
    context = browser.new_context()
    page = watch(context.new_page())
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(WAITING))}")
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert status.text_content() == (
        "Its changes aren't made yet: they are checked against the template, made, and "
        "the mutant and primers registered, by Make the changes and design the primers."
    )
    run = page.get_by_role("button", name="Make the changes")
    assert run.count() == 0
    _place(page, [])
    assert run.count() == 0
    _place(page, [APPLY])
    run.click()
    assert page.evaluate("window.ran") == [APPLY]
    # And from the keyboard.
    run.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.ran") == [APPLY, APPLY]
    context.close()


def test_a_design_already_made_offers_no_run(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, PLASMID)
    _place(page, [APPLY])
    page.get_by_role("table", name="Changes (3)").wait_for()
    assert page.get_by_role("button", name="Make the changes").count() == 0
    page.context.close()


def test_the_strip_places_each_change_and_an_insertion_is_a_join(
    browser: Browser, frame_server: str
) -> None:
    """The picture is the design: a mark where each change lies, as wide as the change is.
    An insertion covers no template base at all -- HGVS gives it the pair it goes between
    -- so it is drawn as the narrowest mark, not as the whole molecule."""
    page = _open(browser, frame_server, PLASMID)
    marks = page.locator("svg rect.change")
    assert marks.count() == 3
    widths = page.evaluate(
        f"[...document.querySelector('{ELEMENT}').shadowRoot"
        ".querySelectorAll('svg rect.change')].map((r) => Number(r.getAttribute('width')))"
    )
    # A single base changed and a six-base join are both drawn at the least width; six
    # bases deleted is six bases of a 2,686-base molecule, which is narrower still, so
    # every mark is the least width and none is anywhere near the strip's own.
    assert all(width == pytest.approx(3.0) for width in widths)
    lefts = page.evaluate(
        f"[...document.querySelector('{ELEMENT}').shadowRoot"
        ".querySelectorAll('svg rect.change')].map((r) => Number(r.getAttribute('x')))"
    )
    assert lefts == sorted(lefts)
    # Base 401 of 2,686 is about an eighth along the 992-wide lane, and 1,501 over half.
    assert lefts[0] == pytest.approx(151.8, abs=1)
    assert lefts[2] == pytest.approx(558.4, abs=1)
    assert ERRORS[page] == []
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_passes_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: Literal["light", "dark"]
) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = _open(browser, frame_server, PLASMID, color_scheme=scheme, bypass_csp=True)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()
    gene = _open(browser, frame_server, GENE, color_scheme=scheme, bypass_csp=True)
    gene.evaluate(AXE.read_text())
    assert gene.evaluate(RUN_AXE, WCAG_22_AA) == []
    gene.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_its_empty_state_passes_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: Literal["light", "dark"]
) -> None:
    context = browser.new_context(color_scheme=scheme, bypass_csp=True)
    page = watch(context.new_page())
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(WAITING))}")
    page.get_by_role("status").wait_for(timeout=30_000)
    _place(page, [APPLY])
    page.get_by_role("button", name="Make the changes").focus()
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    context.close()


def test_it_reads_at_a_phones_width(browser: Browser, frame_server: str) -> None:
    """At 390 pixels across, the strip scales to the width it's given and the tables stay
    inside it: an oligo is 40 bases of unbroken letters, which is the one thing here that
    could push the page sideways."""
    page = _open(browser, frame_server, PLASMID, viewport={"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    strip = page.locator("svg")
    box = strip.bounding_box()
    assert box is not None and box["width"] <= 390
    assert page.get_by_role("table", name="Primers to order (6)").is_visible()
    assert ERRORS[page] == []
    page.context.close()


def test_the_recorded_designs_are_the_ones_the_action_writes() -> None:
    """The fixtures are recordings of real runs, not hand-written designs: they say which
    template and what recorded them, and their primers are consistent with their counts.
    """
    assert RECORDED["recorded_by"] == "server/tests/data/record_mutagenesis.py"
    plasmid = RECORDED["plasmid"]["values"]
    assert RECORDED["plasmid"]["template"] == "pUC19 (L09137.2), 2,686 bp, circular"
    assert len(plasmid["changes_made"]) == 3
    assert plasmid["primers_designed"] == len(plasmid["primers"]) == 6
    assert plasmid["product_length"] == 2686
    assert plasmid["protein_changes"] == []
    # A QuikChange pair is complementary, so the two are the same length and the same
    # acceptance temperature, and each is a pair of one forward and one reverse.
    for at in (0, 2, 4):
        first, second = plasmid["primers"][at], plasmid["primers"][at + 1]
        assert first["change"] == second["change"]
        assert (first["direction"], second["direction"]) == ("forward", "reverse")
        assert first["length"] == second["length"]
        assert first["measures"] == second["measures"]
        assert first["measures"][0]["what"] == "QuikChange Tm"
        assert first["measures"][0]["value"] >= 78.0
    gene = RECORDED["gene"]["values"]
    assert RECORDED["gene"]["template"] == "HBB (NM_000518.5), 444 bp, linear"
    assert gene["protein_changes"] == [{"effect": "missense", "name": "p.Glu7Val"}]
    assert gene["product_length"] == 444

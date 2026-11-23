# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The DNA plugin's Guides tab, in each browser, under the policy Iki's component frames
have (ADR 0084): a CRISPR guide design drawn from what the run recorded -- the guides
placed along the target, what is known about each, and everywhere else each could cut --
said in words for screen readers and tabulated beneath, so nothing is told by colour
alone. It passes axe's WCAG 2.2 AA checks in light and dark.

The designs it draws are real: guides in the ampicillin resistance gene pUC19 shares with
pBR322, searched against both plasmids, and Cas12a guides in pUC19's polylinker with
nothing searched, as the plugin's own action writes them, recorded into
`data/guide_design.json` by `data/record_guide_design.py`.

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
MODULE = "guide-view.js"
ELEMENT = "guide-view"
DESIGN = "iki/dna.design_guides"
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
    '<meta name="color-scheme" content="light dark"><title>Guides</title>\n'
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
await customElements.whenDefined("guide-view");
const element = document.createElement("guide-view");
window.ran = [];
element.addEventListener("iki-run", (e) => window.ran.push(e.detail?.action));
element.entity = JSON.parse(decodeURIComponent(location.hash.slice(1)));
document.body.append(element);
"""

#: Two designs as the action writes them, recorded by data/record_guide_design.py.
RECORDED = json.loads((ROOT / "server" / "tests" / "data" / "guide_design.json").read_text())
#: Eight SpCas9 guides in the gene pUC19 and pBR322 share, searched against both.
SHARED = {
    "id": "00000000-0000-0000-0000-000000000001",
    "hrid": "GDE-000001",
    "type": "iki/dna.GuideDesign",
    "values": RECORDED["shared"]["values"],
}
#: Two Cas12a guides, whose motif is at the other end and whose cut is staggered, with
#: nothing searched.
POLYLINKER = {
    "id": "00000000-0000-0000-0000-000000000002",
    "hrid": "GDE-000002",
    "type": "iki/dna.GuideDesign",
    "values": RECORDED["polylinker"]["values"],
}
#: A design nobody has run yet: what the record is the moment it's created.
WAITING = {
    "id": "00000000-0000-0000-0000-000000000003",
    "hrid": "GDE-000003",
    "type": "iki/dna.GuideDesign",
    "values": {"name": "EMX1 exon 3", "nuclease": "spcas9", "most_mismatches": 3},
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
        "{ slot: 'entity.tab', label: 'Guides', runs }; }",
        runs,
    )


def test_the_strip_says_in_words_what_it_draws(browser: Browser, frame_server: str) -> None:
    """The picture places each guide on its own strand's side of the target's line, so a
    reader who isn't looking at it is told how many there are, how they split between the
    strands, how many are flagged and how many could cut somewhere else."""
    page = _open(browser, frame_server, SHARED)
    assert page.get_by_text(
        "8 guides, between bases 1,909 and 2,013 of the target: 5 on the forward strand, "
        "drawn above the line, and 3 on the reverse strand, drawn below it. 5 of them "
        "have something known to make a guide work less well, and 8 could cut somewhere "
        "else in what was searched."
    ).is_visible()
    # The drawing itself is hidden from screen readers, since the words and the tables say it.
    assert page.locator("svg[aria-hidden=true]").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_what_was_searched_and_what_it_was_found_with_are_above_the_strip(
    browser: Browser, frame_server: str
) -> None:
    """A specificity is a number about a search, so the summary names what was searched;
    and the formula behind it is the website's, not the paper's, so the summary says
    that too (ADR 0205)."""
    page = _open(browser, frame_server, SHARED)
    terms = page.get_by_role("term")
    definitions = page.get_by_role("definition")
    said = {
        terms.nth(i).text_content(): definitions.nth(i).text_content() for i in range(terms.count())
    }
    assert said["Guides found"] == "8"
    assert said["Searched in"] == "pUC19 (2,686 bp), pBR322 (4,361 bp)"
    how = said["Found with"] or ""
    assert "reading NGG after a 20-base protospacer, cutting bluntly 3 bases from the PAM" in how
    assert "Jinek et al., Science 337:816, 2012" in how
    assert "which are the site's documentation and not Hsu et al. 2013" in how
    assert ERRORS[page] == []
    page.context.close()


def test_every_guide_is_in_the_table_with_its_pam_set_apart(
    browser: Browser, frame_server: str
) -> None:
    """The PAM is the part the DNA must already have and the guide can't change, so it is
    written beside the protospacer rather than run together with it."""
    page = _open(browser, frame_server, SHARED)
    table = page.get_by_role("table", name="Guides (8)")
    assert table.count() == 1
    assert table.get_by_role("rowheader").count() == 8
    row = table.get_by_role("row").filter(has_text="GTGGTGTCACGCTCGTCGTT")
    cells = row.get_by_role("cell")
    assert [cells.nth(i).text_content() for i in range(5)] == [
        "forward",
        "1,967",
        "60%",
        "50%",
        "1",
    ]
    assert row.get_by_role("rowheader").text_content() == "GTGGTGTCACGCTCGTCGTT TGG"
    assert ERRORS[page] == []
    page.context.close()


def test_a_perfect_match_elsewhere_halves_a_guides_specificity(
    browser: Browser, frame_server: str
) -> None:
    """pUC19 got its resistance gene from pBR322, so three of these guides are in the
    other plasmid base for base. One perfect match elsewhere adds the whole 100 to the sum
    the aggregate formula divides by, which is half the specificity (ADR 0205)."""
    page = _open(browser, frame_server, SHARED)
    hits = page.get_by_role("table", name="Where else they could cut (8)")
    assert hits.count() == 1
    exact = hits.get_by_role("row").filter(has_text="nowhere: it matches exactly")
    assert exact.count() == 5
    cells = exact.first.get_by_role("cell")
    assert cells.nth(0).text_content() == "pBR322"
    assert cells.nth(4).text_content() == "nowhere: it matches exactly"
    assert cells.nth(5).text_content() == "100.0"
    # And each of those five guides is at 50% in the guides table's specificity column.
    rows = page.get_by_role("table", name="Guides (8)").get_by_role("row")
    specificities = page.evaluate(
        f"[...document.querySelector('{ELEMENT}').shadowRoot"
        ".querySelectorAll('table tbody tr')].map((r) => r.children[4]?.textContent)"
    )
    assert specificities[: rows.count() - 1].count("50%") == 5
    page.context.close()


def test_a_single_mismatch_is_placed_and_scored_by_where_it_lies(
    browser: Browser, frame_server: str
) -> None:
    """The positions are counted from the end furthest from the PAM, which is how the
    weights behind the score are numbered: a mismatch at 11 leaves more of the score than
    one at 15, which leaves more than one at 19."""
    page = _open(browser, frame_server, SHARED)
    hits = page.get_by_role("table", name="Where else they could cut (8)")
    row = hits.get_by_role("row").filter(has_text="TGCCATTGCTGCAGGCATCG")
    cells = row.get_by_role("cell")
    assert cells.nth(4).text_content() == "11"
    assert cells.nth(5).text_content() == "55.5"
    assert ERRORS[page] == []
    page.context.close()


def test_what_may_make_a_guide_work_less_well_is_in_its_own_table(
    browser: Browser, frame_server: str
) -> None:
    """Not a score: the properties a design can see, each under the work that says so, so
    a lab weighs them itself (ADR 0205)."""
    page = _open(browser, frame_server, SHARED)
    flags = page.get_by_role("table", name="What may make a guide work less well (5)")
    assert flags.count() == 1
    assert flags.get_by_role("cell", name="doesn't begin with a G", exact=False).count() == 5
    assert flags.get_by_role("cell", name="Mali et al. 2013", exact=False).count() == 5
    page.context.close()


def test_a_cas12a_design_writes_its_motif_first_and_both_its_cuts(
    browser: Browser, frame_server: str
) -> None:
    """Cas12a reads its motif 5' of the protospacer and cuts the two strands in different
    places, so the tab writes the PAM before the bases and gives both cut sites."""
    page = _open(browser, frame_server, POLYLINKER)
    table = page.get_by_role("table", name="Guides (2)")
    assert table.count() == 1
    row = table.get_by_role("row").filter(has_text="CTGTGTGAAATTGTTATCCGCTC")
    assert row.get_by_role("rowheader").text_content() == "TTTC CTGTGTGAAATTGTTATCCGCTC"
    assert row.get_by_role("cell").nth(1).text_content() == "496 and 501"
    page.context.close()


def test_a_design_with_nothing_searched_says_so_rather_than_showing_a_blank(
    browser: Browser, frame_server: str
) -> None:
    """An absent specificity must not read as a particular guide: the tab says nothing was
    searched, and the column is an em dash rather than a number."""
    page = _open(browser, frame_server, POLYLINKER)
    assert page.get_by_text(
        "Nothing was searched, so no off-target was looked for: these guides have no "
        "specificity, which is not the same as being particular."
    ).is_visible()
    assert (
        page.get_by_role("table", name="Guides (2)").get_by_role("cell", name="\N{EM DASH}").count()
        == 2
    )
    assert page.get_by_role("table", name="Where else they could cut").count() == 0
    assert ERRORS[page] == []
    page.context.close()


def test_a_design_not_run_yet_says_how_and_offers_the_action(
    browser: Browser, frame_server: str
) -> None:
    """A design is a record of what was asked for; the guides, their flags and their
    off-targets are a run's. Until the run has been the tab names the action rather than
    drawing an empty strip, and offers it where the person viewing it may run it
    (ADR 0170)."""
    context = browser.new_context()
    page = watch(context.new_page())
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(WAITING))}")
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert status.text_content() == (
        "Its guides aren't found yet: both strands are read for the nuclease's own motif, "
        "and every guide searched for where else it could cut, by Find the guides and "
        "where else they could cut."
    )
    run = page.get_by_role("button", name="Find the guides")
    assert run.count() == 0
    _place(page, [])
    assert run.count() == 0
    _place(page, [DESIGN])
    run.click()
    assert page.evaluate("window.ran") == [DESIGN]
    # And from the keyboard.
    run.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.ran") == [DESIGN, DESIGN]
    context.close()


def test_a_design_already_run_offers_no_run(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, SHARED)
    _place(page, [DESIGN])
    page.get_by_role("table", name="Guides (8)").wait_for()
    assert page.get_by_role("button", name="Find the guides").count() == 0
    page.context.close()


def test_the_strip_draws_the_two_strands_on_their_own_sides_of_the_line(
    browser: Browser, frame_server: str
) -> None:
    """The strands are told apart by where a mark is drawn, never by colour: a reader who
    can't tell two hues apart can still see which strand a guide is on."""
    page = _open(browser, frame_server, SHARED)
    marks = page.locator("svg rect.guide")
    assert marks.count() == 8
    tops = page.evaluate(
        f"[...document.querySelector('{ELEMENT}').shadowRoot"
        ".querySelectorAll('svg rect.guide')].map((r) => Number(r.getAttribute('y')))"
    )
    # The line is at 40. Every forward guide is drawn above it and every reverse one
    # below, whatever row each is stacked into, so the two sets never cross it.
    assert len(tops) == 8
    assert sum(1 for top in tops if top < 40) == 5
    assert sum(1 for top in tops if top > 40) == 3
    assert ERRORS[page] == []
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_passes_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: Literal["light", "dark"]
) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = _open(browser, frame_server, SHARED, color_scheme=scheme, bypass_csp=True)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()
    other = _open(browser, frame_server, POLYLINKER, color_scheme=scheme, bypass_csp=True)
    other.evaluate(AXE.read_text())
    assert other.evaluate(RUN_AXE, WCAG_22_AA) == []
    other.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_its_empty_state_passes_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: Literal["light", "dark"]
) -> None:
    context = browser.new_context(color_scheme=scheme, bypass_csp=True)
    page = watch(context.new_page())
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(WAITING))}")
    page.get_by_role("status").wait_for(timeout=30_000)
    _place(page, [DESIGN])
    page.get_by_role("button", name="Find the guides").focus()
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    context.close()


def test_it_reads_at_a_phones_width(browser: Browser, frame_server: str) -> None:
    """At 390 pixels across, the strip scales to the width it's given and the tables stay
    inside it: a protospacer is 20 letters that can't be hyphenated and the off-target
    table has seven columns, which is what could push the page sideways."""
    page = _open(browser, frame_server, SHARED, viewport={"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    strip = page.locator("svg")
    box = strip.bounding_box()
    assert box is not None and box["width"] <= 390
    assert page.get_by_role("table", name="Guides (8)").is_visible()
    assert ERRORS[page] == []
    page.context.close()


def test_the_recorded_designs_are_the_ones_the_action_writes() -> None:
    """The fixtures are recordings of real runs, not hand-written designs: they say which
    target and what recorded them, and their guides are consistent with their counts and
    with the formula's own arithmetic.
    """
    assert RECORDED["recorded_by"] == "server/tests/data/record_guide_design.py"
    shared = RECORDED["shared"]["values"]
    assert RECORDED["shared"]["searched"] == "pUC19 (L09137.2) and pBR322 (J01749.1)"
    assert shared["guides_found"] == len(shared["guides"]) == 8
    for guide in shared["guides"]:
        assert len(guide["protospacer"]) == 20
        assert guide["pam"][1:] == "GG"
        assert guide["cuts"] == [
            guide["end"] - 3 if guide["strand"] == "forward" else guide["start"] + 2
        ]
        # The aggregate formula over this guide's own off-targets, to a tenth.
        total = sum(hit["score"] for hit in guide["hits"])
        assert guide["specificity"] == pytest.approx(100 / (100 + total) * 100, abs=0.05)
    polylinker = RECORDED["polylinker"]["values"]
    assert polylinker["guides_found"] == len(polylinker["guides"]) == 2
    for guide in polylinker["guides"]:
        assert len(guide["protospacer"]) == 23
        assert guide["pam"].startswith("TTT")
        assert guide["specificity"] is None
        assert len(guide["cuts"]) == 2

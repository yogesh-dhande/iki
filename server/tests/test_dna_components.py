# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The DNA plugin's Codons tab, in each browser, under the policy Iki's component frames
have (ADR 0084): a codon optimization drawn codon by codon from what the run recorded,
said in words for screen readers and tabulated beneath, so nothing is told by colour
alone. It passes axe's WCAG 2.2 AA checks in light and dark.

The design it draws is real: human beta-globin (HBB, NM_000518.5) rewritten in the codons
of _E. coli_ K-12 MG1655 without the EcoRI or BamHI site, as the plugin's own action
writes it, recorded into `data/codon_optimization.json` by `data/record_codon_optimization.py`.

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
MODULE = "codon-view.js"
ELEMENT = "codon-view"
OPTIMIZE = "iki/dna.optimize_codons"
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
    '<meta name="color-scheme" content="light dark"><title>Codons</title>\n'
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
await customElements.whenDefined("codon-view");
const element = document.createElement("codon-view");
window.ran = [];
element.addEventListener("iki-run", (e) => window.ran.push(e.detail?.action));
element.entity = JSON.parse(decodeURIComponent(location.hash.slice(1)));
document.body.append(element);
"""

#: HBB rewritten for E. coli, as the action writes it: 148 codons, 76 of them changed, and
#: every constraint met. Recorded by data/record_codon_optimization.py.
RECORDED = json.loads((ROOT / "server" / "tests" / "data" / "codon_optimization.json").read_text())
HBB = {
    "id": "00000000-0000-0000-0000-000000000001",
    "hrid": "OPT-000001",
    "type": "iki/dna.CodonOptimization",
    "values": RECORDED["values"],
}
#: The one thing five methionines in a row can't be rid of.
MOTIF = "the motif ATGATG, on the given strand"
#: A design with nothing it can do: five methionines with ATGATG forbidden. Methionine has
#: one codon, so the motif can't be designed away, and the run says so rather than
#: changing the protein. This is the action's own test
#: (plugins/dna/tests/optimize_codons.yaml), as a record.
STUCK = {
    "id": "00000000-0000-0000-0000-000000000002",
    "hrid": "OPT-000002",
    "type": "iki/dna.CodonOptimization",
    "values": {
        "name": "Five methionines",
        "host": "e_coli_k12",
        "codons": 6,
        "codons_changed": 0,
        "protein_length": 5,
        "source_cai": None,
        "optimized_cai": None,
        "cai_reference": (
            "Escherichia coli K-12 substr. MG1655 (GCF_000005845.2 ASM584v2, 4,286 CDS)"
        ),
        "source_gc_content": 27.78,
        "optimized_gc_content": 27.78,
        "constraints_met": False,
        "unmet_constraints": [
            {"kind": "motif", "start": at, "end": at + 5, "detail": MOTIF} for at in (1, 4, 7, 10)
        ],
        "codon_changes": [],
        "optimized_with": (
            "highest usage codons for Escherichia coli K-12 substr. MG1655 "
            "(GCF_000005845.2 ASM584v2, 4,286 CDS); translation table 1 (Standard); "
            "without the motifs ATGATG"
        ),
    },
}
#: A design nobody has run yet: what the record is the moment it's created.
WAITING = {
    "id": "00000000-0000-0000-0000-000000000003",
    "hrid": "OPT-000003",
    "type": "iki/dna.CodonOptimization",
    "values": {"name": "GFP for CHO", "host": "c_griseus"},
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
    page.get_by_role("heading", name="Codons", exact=True).wait_for(timeout=30_000)
    return page


def _place(page: Page, runs: list[str]) -> None:
    """Tells the element where it is, as Iki's bridge does: which actions the person
    viewing it may run here (ADR 0170)."""
    page.evaluate(
        f"(runs) => {{ document.querySelector('{ELEMENT}').place = "
        "{ slot: 'entity.tab', label: 'Codons', runs }; }",
        runs,
    )


def test_the_strip_says_in_words_what_it_draws(browser: Browser, frame_server: str) -> None:
    """The picture is one mark per codon, so a reader who isn't looking at it is told how
    many codons moved and where along the gene, and what the marks' heights mean."""
    page = _open(browser, frame_server, HBB)
    assert page.get_by_text(
        "76 of 148 codons were rewritten, 25 of 50 in its first third, 28 of 50 in its "
        "middle third, 23 of 48 in its last third, each still coding for the amino acid "
        "the template's codon coded for."
    ).is_visible()
    assert page.get_by_text(
        "Its codon adaptation index against the host's table went from 0.66 to 1.00."
    ).is_visible()
    # The drawing itself is hidden from screen readers, since the words and the table say it.
    assert page.locator("svg[aria-hidden=true]").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_how_far_the_design_moved_the_gene_is_above_the_strip(
    browser: Browser, frame_server: str
) -> None:
    """The numbers a reader wants first, with the index's reference set named: an index
    against a genome-wide table is not an index against highly expressed genes, so the
    record says which table it's against (ADR 0203)."""
    page = _open(browser, frame_server, HBB)
    terms = page.get_by_role("term")
    definitions = page.get_by_role("definition")
    said = {
        terms.nth(i).text_content(): definitions.nth(i).text_content() for i in range(terms.count())
    }
    assert said["Codons"] == "148"
    assert said["Rewritten"] == "76 of 148"
    assert said["Protein"] == "147 residues, unchanged"
    assert said["Codon adaptation index"] == "0.66 → 1.00"
    assert said["GC content"] == "56.1% → 57.4%"
    assert said["Index against"] == (
        "Escherichia coli K-12 substr. MG1655 (GCF_000005845.2 ASM584v2, 4,286 CDS)"
    )
    # And what the design was worked out with, in words, as the record keeps it.
    assert "no codon below w = 0.1" in (said["Optimized with"] or "")
    assert "without the EcoRI, BamHI sites" in (said["Optimized with"] or "")
    assert ERRORS[page] == []
    page.context.close()


def test_every_codon_rewritten_is_in_the_table_with_both_weights(
    browser: Browser, frame_server: str
) -> None:
    """A change is checkable only with the host's relative adaptiveness for the codon that
    went and the one that came, so every row carries both."""
    page = _open(browser, frame_server, HBB)
    assert page.get_by_role("table", name="Codons rewritten (76)").count() == 1
    rows = page.get_by_role("row")
    # Two tables' headers, plus the 76 rewritten codons; nothing is left out.
    assert page.get_by_role("rowheader").count() == 76
    first = rows.filter(has_text="ACT")
    cells = first.get_by_role("cell")
    assert [cells.nth(i).text_content() for i in range(5)] == ["T", "ACT", "0.38", "ACC", "1.00"]
    assert page.get_by_role("rowheader", name="5", exact=True).count() == 1
    # The last codon the design rewrote is there too, not just the first few.
    last = RECORDED["values"]["codon_changes"][-1]
    assert page.get_by_role("rowheader", name=str(last["codon"]), exact=True).count() == 1
    assert ERRORS[page] == []
    page.context.close()


def test_a_design_that_met_everything_asked_for_says_so(
    browser: Browser, frame_server: str
) -> None:
    page = _open(browser, frame_server, HBB)
    assert (
        page.get_by_role("status").text_content()
        == "Every constraint the design asked for was met."
    )
    assert page.get_by_role("table", name="Constraints no synonymous codon").count() == 0
    page.context.close()


def test_what_no_synonymous_codon_could_meet_is_placed_in_its_own_table(
    browser: Browser, frame_server: str
) -> None:
    """Five methionines with ATGATG forbidden: methionine has one codon, so the design is
    registered with the motif placed rather than with the protein changed (ADR 0203), and
    the tab shows every place it couldn't help."""
    page = _open(browser, frame_server, STUCK)
    stuck = page.get_by_role("table", name="Constraints no synonymous codon could meet (4)")
    assert stuck.count() == 1
    assert page.get_by_role("rowheader", name="1–6").count() == 1
    assert page.get_by_role("rowheader", name="10–15").count() == 1
    assert page.get_by_role("cell", name="the motif ATGATG, on the given strand").count() == 4
    # A gene with no codon the index counts has no index, which is said rather than nought.
    definitions = page.get_by_role("definition")
    said = [definitions.nth(i).text_content() for i in range(definitions.count())]
    assert "— → —" in said
    # And no codon moved, so there is no table of codons rewritten.
    assert page.get_by_role("table", name="Codons rewritten").count() == 0
    assert page.get_by_text("0 of 6 codons were rewritten").is_visible()
    assert ERRORS[page] == []
    page.context.close()


def test_a_design_not_worked_out_yet_says_how_and_offers_the_action(
    browser: Browser, frame_server: str
) -> None:
    """A design is a record of what was asked for; the codons are a run's. Until the run
    has been the tab names the action rather than drawing an empty strip, and offers it
    where the person viewing it may run it (ADR 0170)."""
    context = browser.new_context()
    page = watch(context.new_page())
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(WAITING))}")
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert status.text_content() == (
        "Its codons aren't worked out yet: the design is read, and the sequence it makes "
        "registered, by Rewrite the coding region in the host's codons."
    )
    run = page.get_by_role("button", name="Run the optimization")
    assert run.count() == 0
    _place(page, [])
    assert run.count() == 0
    _place(page, [OPTIMIZE])
    run.click()
    assert page.evaluate("window.ran") == [OPTIMIZE]
    # And from the keyboard.
    run.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.ran") == [OPTIMIZE, OPTIMIZE]
    context.close()


def test_a_design_already_worked_out_offers_no_run(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, HBB)
    _place(page, [OPTIMIZE])
    page.get_by_role("heading", name="Codons", exact=True).wait_for()
    assert page.get_by_role("button", name="Run the optimization").count() == 0
    page.context.close()


def test_the_strip_draws_one_mark_per_codon_and_marks_the_rewritten_ones(
    browser: Browser, frame_server: str
) -> None:
    """The picture is the design: a mark for every codon of the gene, the rewritten ones
    told apart from the ones kept, and each as tall as the host's relative adaptiveness
    for the codon now written there -- so a stretch the design left alone is visible as
    the dip it is."""
    page = _open(browser, frame_server, HBB)
    marks = page.locator("svg rect")
    assert marks.count() == 148
    assert page.locator("svg rect.changed").count() == 76
    assert page.locator("svg rect.kept").count() == 72
    # Codon 5 was rewritten to ACC, whose relative adaptiveness is 1, so its mark is the
    # full height of the lane; nothing is drawn taller than that.
    heights = page.evaluate(
        "[...document.querySelector('codon-view').shadowRoot.querySelectorAll('svg rect')]"
        ".map((r) => Number(r.getAttribute('height')))"
    )
    assert max(heights) == pytest.approx(80.0)
    assert min(heights) > 0
    assert ERRORS[page] == []
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_passes_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: Literal["light", "dark"]
) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = _open(browser, frame_server, HBB, color_scheme=scheme, bypass_csp=True)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()
    stuck = _open(browser, frame_server, STUCK, color_scheme=scheme, bypass_csp=True)
    stuck.evaluate(AXE.read_text())
    assert stuck.evaluate(RUN_AXE, WCAG_22_AA) == []
    stuck.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_its_empty_state_passes_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: Literal["light", "dark"]
) -> None:
    context = browser.new_context(color_scheme=scheme, bypass_csp=True)
    page = watch(context.new_page())
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(WAITING))}")
    page.get_by_role("status").wait_for(timeout=30_000)
    _place(page, [OPTIMIZE])
    page.get_by_role("button", name="Run the optimization").focus()
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    context.close()


def test_it_reads_at_a_phones_width(browser: Browser, frame_server: str) -> None:
    """At 390 pixels across, the strip scales to the width it's given and the tables
    stay inside it: nothing is cut off and nothing scrolls sideways."""
    page = _open(browser, frame_server, HBB, viewport={"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    strip = page.locator("svg")
    box = strip.bounding_box()
    assert box is not None and box["width"] <= 390
    assert page.get_by_role("table", name="Codons rewritten (76)").is_visible()
    assert ERRORS[page] == []
    page.context.close()


def test_the_recorded_design_is_the_one_the_action_writes() -> None:
    """The fixture is a recording of a real run, not a hand-written design: it says which
    gene, which host and what recorded it, and its changes are consistent with its counts.
    """
    values = RECORDED["values"]
    assert RECORDED["gene"] == "HBB (NM_000518.5)"
    assert RECORDED["recorded_by"] == "server/tests/data/record_codon_optimization.py"
    assert len(values["codon_changes"]) == values["codons_changed"] == 76
    assert values["codons"] == 148
    assert values["protein_length"] == 147
    assert values["constraints_met"] is True
    assert values["unmet_constraints"] == []
    assert all(change["from"] != change["to"] for change in values["codon_changes"])
    assert all(1 <= change["codon"] <= 148 for change in values["codon_changes"])

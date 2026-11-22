# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""The plasmid plugin's Map tab, in each browser, under the policy Iki's component frames
have (ADR 0084): a plasmid drawn round a circle from the parts its annotation found, said
in words for screen readers and tabulated beneath, so nothing is told by colour alone. It
passes axe's WCAG 2.2 AA checks in light and dark.

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

COMPONENTS = Path(__file__).resolve().parents[2] / "plugins" / "plasmid" / "components"
MODULE = "plasmid-map.js"
ELEMENT = "plasmid-map"
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
    '<meta name="color-scheme" content="light dark"><title>Plasmid</title>\n'
    + frames.HARDEN_TAG
    + '<script type="module" src="/{module}" integrity="{integrity}" '
    'crossorigin="anonymous"></script>\n'
    '<script type="module" src="/driver.js" crossorigin="anonymous"></script>\n'
    "</head><body></body></html>\n"
)
#: Sets the element's entity, and the entities its place found, as Iki's bridge does; notes
#: any request the policy refused, and any event the element sent the page (ADR 0117).
DRIVER = b"""
window.refused = [];
window.sent = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.refused.push(`${e.violatedDirective} ${e.blockedURI}`);
});
await customElements.whenDefined("plasmid-map");
const element = document.createElement("plasmid-map");
// The actions it asks to run, as Iki's bridge would pass them to the page (ADR 0170).
window.ran = [];
element.addEventListener("iki-run", (e) => window.ran.push(e.detail?.action));
const given = JSON.parse(decodeURIComponent(location.hash.slice(1)));
for (const name of ["iki-open", "iki-select", "iki-more", "iki-refine"]) {
  element.addEventListener(name, (e) => window.sent.push({ name, detail: e.detail }));
}
if (given.entities) element.entities = given.entities;
element.entity = given.entity ?? given;
document.body.append(element);
"""


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


def _feature(
    kind: str, name: str, start: int, end: int, strand: str, role: str, identity: str, label: str
) -> dict[str, Any]:
    return {
        "type": kind,
        "name": name,
        "start": start,
        "end": end,
        "strand": strand,
        "qualifiers": {"label": label, "role": role, "identity": identity},
    }


#: pcDNA3.1(+) (INSDC MN996867.1, 5,428 bp) as the plugin annotates it: ten parts of the
#: library, two of them overlapping (the SV40 origin lies inside the SV40 early promoter),
#: and two on the reverse strand. ``server/tests/test_plasmid_annotation.py`` checks these
#: coordinates against the record.
PCDNA = {
    "id": "00000000-0000-0000-0000-000000000001",
    "hrid": "PLS-000001",
    "type": "iki/plasmid.Plasmid",
    "values": {
        "name": "pcDNA3.1(+)",
        "length": 5428,
        "topology": "circular",
        "annotated_with": "iki/plasmid features 1.0 (34 parts), at least 90% identity",
        "annotated_features": [
            _feature(
                "promoter",
                "CMV promoter",
                302,
                818,
                "forward",
                "promoter",
                "100.0%",
                "CMV promoter and enhancer (human cytomegalovirus immediate early)",
            ),
            _feature(
                "promoter", "T7 promoter", 863, 881, "forward", "promoter", "100.0%", "T7 promoter"
            ),
            _feature("rep_origin", "f1 ori", 1298, 1726, "forward", "origin", "94.1%", "f1 ori"),
            _feature(
                "promoter",
                "SV40 early promoter",
                1731,
                2012,
                "forward",
                "promoter",
                "99.7%",
                "SV40 early promoter",
            ),
            _feature(
                "rep_origin", "SV40 ori", 1969, 2044, "forward", "origin", "98.7%", "SV40 ori"
            ),
            _feature(
                "CDS",
                "NeoR/KanR",
                2136,
                2930,
                "forward",
                "selection",
                "100.0%",
                "NeoR/KanR (nptII, neomycin phosphotransferase II)",
            ),
            _feature(
                "polyA_signal",
                "SV40 poly(A) signal",
                3104,
                3178,
                "forward",
                "terminator",
                "100.0%",
                "SV40 poly(A) signal",
            ),
            _feature(
                "promoter",
                "lac promoter",
                3317,
                3400,
                "reverse",
                "promoter",
                "100.0%",
                "lac promoter",
            ),
            _feature(
                "rep_origin",
                "ori",
                3676,
                4261,
                "reverse",
                "origin",
                "99.3%",
                "ori (ColE1/pMB1/pBR322/pUC origin of replication)",
            ),
            _feature(
                "CDS",
                "AmpR",
                4432,
                5292,
                "reverse",
                "resistance",
                "100.0%",
                "AmpR (bla, beta-lactamase)",
            ),
        ],
    },
}
#: A part that wraps the origin, as one of a circular plasmid's may (ADR 0138).
AROUND = {
    "hrid": "PLS-000002",
    "values": {
        "name": "a construct",
        "length": 4000,
        "topology": "circular",
        "annotated_features": [
            _feature("CDS", "AmpR", 3900, 120, "forward", "resistance", "99.5%", "AmpR")
        ],
    },
}

#: pGKL1 (INSDC X01095.1), the 8,874 bp linear killer plasmid of Kluyveromyces lactis,
#: with two parts of a library along it, one on each strand: a linear molecule's map is
#: drawn as a line, since it has two ends and no origin to go round.
LINEAR = {
    "id": "00000000-0000-0000-0000-000000000003",
    "hrid": "PLS-000005",
    "type": "iki/plasmid.Plasmid",
    "values": {
        "name": "pGKL1",
        "length": 8874,
        "topology": "linear",
        "annotated_with": "iki/plasmid features 1.0 (34 parts), at least 90% identity",
        "annotated_features": [
            _feature("CDS", "ORF2", 3230, 6670, "forward", "regulatory", "100.0%", "ORF2"),
            _feature("CDS", "ORF3", 6641, 7927, "reverse", "regulatory", "100.0%", "ORF3"),
        ],
    },
}

#: pUC19 (INSDC L09137.2) with the layers its runs found along it: the ORFs its own bases
#: code for, as ``server/tests/test_plasmid_orfs.py`` checks them against the record -- its
#: beta-lactamase across 1,626 to 2,486 on the reverse strand and its lacZ-alpha fragment
#: -- and where NEB's M13/pUC sequencing primers bind it, as
#: ``server/tests/test_plasmid_pcr.py`` checks them against its bases.
LAYERED = {
    "id": "00000000-0000-0000-0000-000000000004",
    "hrid": "PLS-000010",
    "type": "iki/plasmid.Plasmid",
    "values": {
        "name": "pUC19",
        "length": 2686,
        "topology": "circular",
        "annotated_with": "iki/plasmid features 1.0 (34 parts), at least 90% identity",
        "annotated_features": [
            _feature("CDS", "AmpR", 1626, 2486, "reverse", "resistance", "100.0%", "AmpR (bla)"),
        ],
        "orfs_found_with": (
            "ORFs of at least 75 codons, translation table 11 (Bacterial, Archaeal, Plant "
            "Plastid), starting at ATG only; read across its origin"
        ),
        "orfs": [
            {
                "start": 146,
                "end": 469,
                "strand": "reverse",
                "frame": -1,
                "length": 324,
                "amino_acids": 107,
                "start_codon": "ATG",
                "translation": "MTMITPSLHACRST",
            },
            {
                "start": 1626,
                "end": 2486,
                "strand": "reverse",
                "frame": -3,
                "length": 861,
                "amino_acids": 286,
                "start_codon": "ATG",
                "translation": "MSIQHFRVALIPFFAAFCLPVFA",
            },
        ],
        "primer_binding_sites_found_with": (
            "placed by their 15 3' bases, mismatches weighing at most 2 (3 within the last 5 "
            "bases, 1 beyond); Tm by SantaLucia 1998 nearest-neighbour (Biopython Tm_NN, "
            "DNA_NN3), 50 mM Na+, no Mg2+ or dNTPs, 25 nM of each strand"
        ),
        "primer_binding_sites": [
            {
                "primer": "PRM-000001",
                "name": "M13/pUC forward (-20)",
                "strand": "forward",
                "start": 379,
                "end": 395,
                "three_prime": 395,
                "annealed": 17,
                "tail": 0,
                "mismatches": [],
                "melting_temperature": 49.2,
            },
            {
                "primer": "PRM-000002",
                "name": "M13/pUC reverse",
                "strand": "reverse",
                "start": 465,
                "end": 481,
                "three_prime": 465,
                "annealed": 17,
                "tail": 0,
                "mismatches": [{"from_three_prime": 13, "primer": "C", "template": "A"}],
                "melting_temperature": 41.3,
            },
        ],
    },
}
#: The project's primers, as Iki gives a component the entities its place finds (ADR 0117).
PRIMERS = {
    "items": [
        {
            "id": "00000000-0000-0000-0000-000000000031",
            "hrid": "PRM-000001",
            "title": "M13/pUC forward (-20)",
            "values": {"name": "M13/pUC forward (-20)", "bases": "GTAAAACGACGGCCAGT"},
        }
    ],
    "more": False,
    "limited": False,
    "loading": False,
    "error": None,
}

#: Each page's uncaught errors. Not its console's: Firefox logs its own favicon request,
#: which the frame's policy blocks, as an error.
ERRORS: dict[Page, list[str]] = {}


def _open(browser: Browser, frame_server: str, entity: object, **options: Any) -> Page:
    context = browser.new_context(**options)
    page = watch(context.new_page())
    ERRORS[page] = []
    page.on("pageerror", lambda error: ERRORS[page].append(str(error)))
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    page.get_by_role("heading", name="Map").wait_for(timeout=30_000)
    return page


def test_the_map_says_in_words_what_it_draws(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, PCDNA)
    assert page.get_by_text(
        "A circle of 5,428 base pairs, drawn as a ring with base 1 at the top, going "
        "clockwise, with 10 parts of the library found along it: CMV promoter and enhancer "
        "(human cytomegalovirus immediate early), 302 to 818 on the forward strand"
    ).is_visible()
    # The drawing itself is hidden from screen readers, since the words and the table say it.
    assert page.locator("svg[aria-hidden=true]").count() == 1
    assert page.get_by_text(
        "iki/plasmid features 1.0 (34 parts), at least 90% identity"
    ).is_visible()
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_every_part_is_in_the_table_with_its_coordinates(
    browser: Browser, frame_server: str
) -> None:
    page = _open(browser, frame_server, PCDNA)
    rows = page.get_by_role("row")
    assert rows.count() == 11  # its header and the ten parts
    assert page.get_by_role("rowheader", name="AmpR (bla, beta-lactamase)").count() == 1
    beta = rows.filter(has_text="beta-lactamase")
    cells = beta.get_by_role("cell")
    assert [cells.nth(i).text_content() for i in range(5)] == [
        "4,432–5,292",
        "861 bp",
        "reverse",
        "Bacterial resistance",
        "100.0%",
    ]
    # Overlapping parts are both there: the SV40 origin lies inside the SV40 promoter.
    assert page.get_by_role("rowheader", name="SV40 ori").count() == 1
    assert page.get_by_role("rowheader", name="SV40 early promoter").count() == 1
    page.context.close()


def test_a_part_that_wraps_the_origin_gives_both_stretches(
    browser: Browser, frame_server: str
) -> None:
    page = _open(browser, frame_server, AROUND)
    assert page.get_by_role("cell", name="3,900–4,000, 1–120").count() == 1
    assert page.get_by_role("cell", name="221 bp").count() == 1
    page.context.close()


@pytest.mark.parametrize(
    ("what", "values"),
    [
        ("nothing at all", {}),
        # A plasmid's length is its registry entry's, the same for every project (ADR
        # 0115), so a second project has it as soon as its record exists -- before that
        # project's own annotation has run. `annotated_with` is what that run writes, so
        # until it's there the map says it isn't drawn yet, rather than reporting that
        # none of the library's parts is along a 4,000 bp plasmid.
        ("a length from its registry entry and no annotation of its own", {"length": 4000}),
    ],
)
def test_a_plasmid_not_annotated_yet_says_so_rather_than_drawing_nothing(
    browser: Browser, frame_server: str, what: str, values: dict[str, object]
) -> None:
    context = browser.new_context()
    page = watch(context.new_page())
    entity = {"hrid": "PLS-000003", "values": values}
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    text = status.text_content() or ""
    assert "drawn once the plasmid is annotated" in text, what
    assert "none of the library's parts" not in text, what
    context.close()


ANNOTATE = "iki/plasmid.annotate_plasmid"


def _place(page: Page, runs: list[str]) -> None:
    """Sets the element's place, as Iki's bridge does once the page says where it is."""
    page.evaluate(
        "(runs) => { document.querySelector('plasmid-map').place = "
        "{ slot: 'entity.tab', label: 'Map', runs }; }",
        runs,
    )


def test_a_plasmid_not_annotated_yet_says_how_and_offers_its_annotation(
    browser: Browser, frame_server: str
) -> None:
    # The trigger that annotates each new plasmid is a project's, and off until someone
    # turns it on (ADR 0080): the map names the action rather than promising it runs,
    # and offers it where the person viewing it may run it (ADR 0170).
    context = browser.new_context()
    page = watch(context.new_page())
    entity = {"hrid": "PLS-000003", "values": {"length": 4000}}
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    status = page.get_by_role("status")
    status.wait_for(timeout=30_000)
    assert status.text_content() == (
        "Its map isn't drawn yet: it's drawn once the plasmid is annotated from the library, "
        "by Annotate a plasmid from the library. A project can run it on each plasmid it "
        "gets, with its trigger Annotate each new plasmid from the library."
    )
    run = page.get_by_role("button", name="Run annotation")
    assert run.count() == 0
    _place(page, [])
    assert run.count() == 0
    _place(page, [ANNOTATE])
    run.click()
    assert page.evaluate("window.ran") == [ANNOTATE]
    # And from the keyboard.
    run.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("window.ran") == [ANNOTATE, ANNOTATE]
    context.close()


def test_an_annotated_plasmid_offers_no_annotation(browser: Browser, frame_server: str) -> None:
    page = _open(browser, frame_server, PCDNA)
    _place(page, [ANNOTATE])
    page.get_by_role("heading", name="Map").wait_for()
    assert page.get_by_role("button", name="Run annotation").count() == 0
    page.context.close()


def test_an_annotated_plasmid_with_no_parts_found_says_none_were(
    browser: Browser, frame_server: str
) -> None:
    """A run that annotated the record and found nothing is not the same as a record
    waiting for its run: once `annotated_with` is there, the map says so."""
    context = browser.new_context()
    page = watch(context.new_page())
    entity = {
        "hrid": "PLS-000004",
        "values": {
            "length": 4000,
            "annotated_with": "iki/plasmid features 1.0 (34 parts), at least 90% identity",
            "annotated_features": [],
        },
    }
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    page.get_by_role("heading", name="Map").wait_for(timeout=30_000)
    said = page.get_by_text(
        "A circle of 4,000 base pairs, drawn as a ring with base 1 at the top, going "
        "clockwise, with none of the library's parts found along it."
    )
    assert said.count() == 1
    context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_passes_wcag_22_aa(browser: Browser, frame_server: str, scheme: str) -> None:
    # axe is run as a script, which the frame's policy refuses to a page's own: here the
    # context sets the policy aside, which changes nothing axe checks.
    page = _open(browser, frame_server, PCDNA, color_scheme=scheme, bypass_csp=True)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_its_empty_state_passes_wcag_22_aa(
    browser: Browser, frame_server: str, scheme: Literal["light", "dark"]
) -> None:
    context = browser.new_context(color_scheme=scheme, bypass_csp=True)
    page = watch(context.new_page())
    entity = {"hrid": "PLS-000003", "values": {"length": 4000}}
    page.goto(f"{frame_server}/frame/{MODULE}#{urllib.parse.quote(json.dumps(entity))}")
    page.get_by_role("status").wait_for(timeout=30_000)
    _place(page, [ANNOTATE])
    page.get_by_role("button", name="Run annotation").focus()
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    context.close()


# --- the ring and the line (ADR 0168) ------------------------------------------------


def test_a_linear_plasmid_opens_on_the_line_and_is_offered_no_ring(
    browser: Browser, frame_server: str
) -> None:
    """A linear molecule has two ends and no origin, so its map is a line, and there is
    no ring view to switch to. The words say it is linear and where its ends are, and
    every part is in the table beneath as it is on a circle's map."""
    page = _open(browser, frame_server, LINEAR)
    assert page.get_by_text(
        "A linear molecule of 8,874 base pairs, drawn as a line from base 1 at the left to "
        "base 8,874 at the right, with an end at each, with 2 parts of the library found "
        "along it: ORF2, 3,230 to 6,670 on the forward strand; ORF3, 6,641 to 7,927 on the "
        "reverse strand."
    ).is_visible()
    assert page.get_by_role("group", name="Map view").count() == 0
    assert page.get_by_role("rowheader", name="ORF2").count() == 1
    assert page.locator("svg[aria-hidden=true]").count() == 1
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_a_circular_plasmid_opens_on_the_ring_and_switches_to_the_line(
    browser: Browser, frame_server: str
) -> None:
    """A circle opens on its ring, as a plasmid is read, and can be seen as a line too --
    cut at base 1, which the words say, since that is where a part across the origin is
    split in two. Each view's button says whether it is the one being shown, and the
    table is the same under both."""
    page = _open(browser, frame_server, PCDNA)
    ring = page.get_by_role("button", name="Circular")
    line = page.get_by_role("button", name="Linear")
    assert ring.get_attribute("aria-pressed") == "true"
    assert line.get_attribute("aria-pressed") == "false"
    assert page.get_by_text(
        "A circle of 5,428 base pairs, drawn as a ring with base 1 at the top, going clockwise"
    ).is_visible()
    line.click()
    assert page.get_by_text(
        "A circle of 5,428 base pairs, drawn as a line cut at base 1, so a part across its "
        "origin is drawn in two pieces"
    ).is_visible()
    assert page.get_by_role("button", name="Linear").get_attribute("aria-pressed") == "true"
    assert page.get_by_role("button", name="Circular").get_attribute("aria-pressed") == "false"
    assert page.get_by_role("row").count() == 11
    # And back to the ring.
    page.get_by_role("button", name="Circular").click()
    assert page.get_by_role("button", name="Circular").get_attribute("aria-pressed") == "true"
    assert ERRORS[page] == []
    page.context.close()


def test_the_views_are_reached_and_chosen_from_the_keyboard(
    browser: Browser, browser_name: str, frame_server: str
) -> None:
    """The two views are buttons, so the keyboard reaches them and presses them as it does
    any button -- with Enter and with Space -- and after a view is chosen the keyboard is
    left on the button that was pressed rather than at the top of the page.

    Safari moves the tab key between links and form fields only unless its full keyboard
    access is on, so where the tab key lands is checked in the browsers whose default it
    is; pressing the buttons from the keyboard is checked in all three.
    """
    page = _open(browser, frame_server, PCDNA)
    focused = "document.querySelector('plasmid-map').shadowRoot.activeElement"
    if browser_name != "webkit":
        page.keyboard.press("Tab")
        assert page.evaluate(f"{focused}?.textContent") == "Circular"
        page.keyboard.press("Tab")
        assert page.evaluate(f"{focused}?.textContent") == "Linear"
    else:
        page.get_by_role("button", name="Linear").focus()
    page.keyboard.press("Enter")
    assert page.get_by_role("button", name="Linear").get_attribute("aria-pressed") == "true"
    # The keyboard is on the view that was chosen, so the next key reaches it.
    assert page.evaluate(f"{focused}?.getAttribute('aria-pressed')") == "true"
    # Space works too, as it does on any button: back to the ring, and the keyboard with it.
    page.get_by_role("button", name="Circular").focus()
    page.keyboard.press(" ")
    assert page.get_by_role("button", name="Circular").get_attribute("aria-pressed") == "true"
    assert page.evaluate(f"{focused}?.textContent") == "Circular"
    assert ERRORS[page] == []
    page.context.close()


def test_the_line_draws_a_part_across_the_origin_in_two_pieces(
    browser: Browser, frame_server: str
) -> None:
    """A circle's part that crosses its origin is one arc on the ring and two pieces on
    the line, since the line is cut there. Its row in the table gives both stretches
    either way, so the table says what the drawing can't."""
    page = _open(browser, frame_server, AROUND)
    bars = (
        "document.querySelector('plasmid-map').shadowRoot.querySelectorAll('svg line.arc').length"
    )
    page.get_by_role("button", name="Linear").click()
    assert page.evaluate(bars) == 2
    assert page.get_by_role("cell", name="3,900–4,000, 1–120").count() == 1
    page.get_by_role("button", name="Circular").click()
    assert page.evaluate(bars) == 0  # the ring draws arcs as paths, not lines
    assert page.get_by_role("cell", name="3,900–4,000, 1–120").count() == 1
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_line_passes_wcag_22_aa(browser: Browser, frame_server: str, scheme: str) -> None:
    """The linear map and its view buttons, in light and dark, on a linear plasmid and on
    a circle shown as a line."""
    page = _open(browser, frame_server, LINEAR, color_scheme=scheme, bypass_csp=True)
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()
    page = _open(browser, frame_server, PCDNA, color_scheme=scheme, bypass_csp=True)
    page.get_by_role("button", name="Linear").click()
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()


def test_its_introduction_is_true_of_both_views() -> None:
    # The introduction is above the map whichever view is shown, a linear molecule's
    # line or a circle's ring or line, so it doesn't say it's drawn round a circle.
    import yaml

    manifest = yaml.safe_load((COMPONENTS / "plasmid_map.yaml").read_text(encoding="utf-8"))
    said = " ".join(manifest["description"].split())
    assert "round a ring for a circle and along a line for a linear one" in said
    assert not said.startswith("The plasmid round a circle")


# --- the layers: parts, ORFs and primer binding sites (ADR 0172) ---------------------


def test_each_layer_is_drawn_tabulated_and_said_in_words(
    browser: Browser, frame_server: str
) -> None:
    """A plasmid whose three runs have been has three layers along it, each in a table of
    its own with what only that layer has: an ORF's frame and the protein it codes for,
    and a binding site's 3' end, its tail, its pairing and its melting temperature. The
    words above the drawing name every one of them, so nothing is told by the picture
    alone, and each run's own settings are named beneath.
    """
    page = _open(browser, frame_server, LAYERED)
    said = page.get_by_text(
        "A circle of 2,686 base pairs, drawn as a ring with base 1 at the top, going "
        "clockwise, with 1 part of the library found along it"
    )
    assert said.is_visible()
    assert page.get_by_text("2 open reading frames: 107 amino acids in frame -1").is_visible()
    assert page.get_by_text(
        "2 primer binding sites: M13/pUC forward (-20), 379 to 395 on the forward strand, "
        "its 3′ end at base 395"
    ).is_visible()
    # An ORF's row: where it lies, its frame, its length in bases and in amino acids, the
    # codon it starts at and the protein.
    orfs = page.get_by_role("table").filter(has_text="Open reading frames")
    bla = orfs.get_by_role("row").filter(has_text="1,626")
    cells = bla.get_by_role("cell")
    assert [cells.nth(i).text_content() for i in range(6)] == [
        "-3",
        "reverse",
        "861 bp",
        "286 aa",
        "ATG",
        "MSIQHFRVALIPFFAAFCLPVFA",
    ]
    # A binding site's row: its primer, where it lies, its 3' end, how much annealed, its
    # 5' tail, how it paired and the site's Tm.
    sites = page.get_by_role("table").filter(has_text="Primer binding sites")
    reverse = sites.get_by_role("row").filter(has_text="M13/pUC reverse")
    said_cells = reverse.get_by_role("cell")
    assert [said_cells.nth(i).text_content() for i in range(7)] == [
        "465–481",
        "reverse",
        "465",
        "17 bp",
        "0 bp",
        "C→A, 13 from its 3′ end",
        "41.3 °C",
    ]
    assert page.get_by_text("ORFs of at least 75 codons, translation table 11").is_visible()
    assert page.get_by_text("placed by their 15 3' bases").is_visible()
    assert ERRORS[page] == []
    assert page.evaluate("window.refused") == []
    page.context.close()


def test_a_layer_is_turned_off_and_on_from_the_keyboard(
    browser: Browser, browser_name: str, frame_server: str
) -> None:
    """Each layer is a button that says whether it is showing and how many it holds.
    Turning one off takes its table and its words away and leaves the others; turning it
    on brings them back, and the keyboard stays on the button that was pressed.
    """
    page = _open(browser, frame_server, LAYERED)
    orfs = page.get_by_role("button", name="Open reading frames (2)")
    assert orfs.get_attribute("aria-pressed") == "true"
    assert page.get_by_role("table").filter(has_text="Open reading frames").count() == 1
    orfs.click()
    assert (
        page.get_by_role("button", name="Open reading frames (2)").get_attribute("aria-pressed")
        == "false"
    )
    assert page.get_by_role("table").filter(has_text="Open reading frames").count() == 0
    assert "open reading frames" not in (page.get_by_role("paragraph").first.text_content() or "")
    # The other layers are untouched.
    assert page.get_by_role("table").filter(has_text="Primer binding sites").count() == 1
    assert page.get_by_role("button", name="Library parts (1)").get_attribute("aria-pressed") == (
        "true"
    )
    focused = "document.querySelector('plasmid-map').shadowRoot.activeElement"
    if browser_name != "webkit":
        assert page.evaluate(f"{focused}?.textContent") == "Open reading frames (2)"
    page.get_by_role("button", name="Open reading frames (2)").focus()
    page.keyboard.press("Enter")
    assert (
        page.get_by_role("button", name="Open reading frames (2)").get_attribute("aria-pressed")
        == "true"
    )
    assert page.get_by_role("table").filter(has_text="Open reading frames").count() == 1
    assert ERRORS[page] == []
    page.context.close()


def test_the_layers_are_drawn_on_the_line_as_well_as_the_ring(
    browser: Browser, frame_server: str
) -> None:
    """The line view draws every layer too, each on lanes of its own: four bars for the
    part, the two ORFs and the two sites that lie on it, above the line where they run
    forward and below where they run reverse. The tables are the same under both views.
    """
    page = _open(browser, frame_server, LAYERED)
    bars = (
        "document.querySelector('plasmid-map').shadowRoot.querySelectorAll('svg line.arc').length"
    )
    page.get_by_role("button", name="Linear").click()
    assert page.evaluate(bars) == 5  # one part, two ORFs, two sites
    assert page.get_by_role("table").filter(has_text="Open reading frames").count() == 1
    page.get_by_role("button", name="Primer binding sites (2)").click()
    assert page.evaluate(bars) == 3
    assert ERRORS[page] == []
    page.context.close()


def test_a_sites_primer_opens_its_record_through_iki(browser: Browser, frame_server: str) -> None:
    """A binding site's row names its primer, and where Iki gave the map that primer among
    the project's own, the name is a button that asks Iki to open its page: the map never
    navigates itself (ADR 0117). A primer Iki didn't give it is named in plain words
    instead, with nothing to press.
    """
    page = _open(browser, frame_server, {"entity": LAYERED, "entities": PRIMERS})
    sites = page.get_by_role("table").filter(has_text="Primer binding sites")
    forward = sites.get_by_role("button", name="M13/pUC forward (-20) (PRM-000001)")
    assert forward.count() == 1
    forward.click()
    assert page.evaluate("window.sent") == [
        {"name": "iki-open", "detail": {"entity": "PRM-000001"}}
    ]
    # The reverse primer isn't among the ones Iki gave, so its row is words, not a button.
    assert sites.get_by_role("button", name="M13/pUC reverse (PRM-000002)").count() == 0
    assert sites.get_by_role("rowheader", name="M13/pUC reverse (PRM-000002)").count() == 1
    assert ERRORS[page] == []
    page.context.close()


def test_a_run_that_found_no_orfs_says_so_rather_than_nothing(
    browser: Browser, frame_server: str
) -> None:
    """A layer whose run has been and found nothing is not the same as a layer whose run
    hasn't been: once the record says what the ORFs were found with, the map says that none
    was found in any of its six frames."""
    entity = {
        "hrid": "PLS-000011",
        "values": {
            "length": 2686,
            "topology": "circular",
            "annotated_with": "iki/plasmid features 1.0 (34 parts), at least 90% identity",
            "annotated_features": [],
            "orfs_found_with": (
                "ORFs of at least 300 codons, translation table 1 (Standard), starting at "
                "ATG only; read across its origin"
            ),
            "orfs": [],
        },
    }
    page = _open(browser, frame_server, entity)
    assert page.get_by_text(
        "with none of the library's parts found along it, and with no open reading frame "
        "found in any of its six frames."
    ).is_visible()
    assert page.get_by_role("button", name="Open reading frames (0)").count() == 1
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_layers_pass_wcag_22_aa(browser: Browser, frame_server: str, scheme: str) -> None:
    """Every layer, its buttons and its table, on the ring and on the line, in light and
    dark."""
    page = _open(
        browser,
        frame_server,
        {"entity": LAYERED, "entities": PRIMERS},
        color_scheme=scheme,
        bypass_csp=True,
    )
    page.evaluate(AXE.read_text())
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.get_by_role("button", name="Linear").click()
    assert page.evaluate(RUN_AXE, WCAG_22_AA) == []
    page.context.close()

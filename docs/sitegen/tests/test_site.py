"""The built docs site, ikisci.org, in Chromium, Firefox and WebKit.

It's served as a static host serves it, with only the policy each page carries. Each kind
of page is opened in light and dark, on a phone (390 px) and a wide screen (1440 px): it
follows the reader's colour scheme, breaks no rule of its Content Security Policy (nor do
the viewers' frames inside it), logs no errors, asks no other origin for anything, never
scrolls sideways, and axe finds nothing against WCAG 2.2 AA. Then its controls are used as
a reader uses them: the worked plate's toggle, the theme switch and search; and it reads
without scripts. Nothing on it cites Iki's design records, which are for its developers.

Needs the built site (``make docs``) and the browsers (``make test-browsers``).
"""

from __future__ import annotations

import functools
import http.server
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.sync_api import Browser, Page, Playwright, expect, sync_playwright

pytestmark = pytest.mark.built_docs

ROOT = Path(__file__).resolve().parents[3]
DIST = ROOT / "docs" / "site" / "dist"
AXE = ROOT / "node_modules" / "axe-core" / "axe.min.js"
BROWSERS = ["chromium", "firefox", "webkit"]
WCAG_22_AA = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
WIDTHS = [390, 1440]

PREP = "/use-cases/verify-a-plasmid-prep/"
ALIGNMENTS = "/concepts/whole-plasmid-alignments/"
ASSAY = "/use-cases/dose-response-plate-assay/"
SIGNATURES = "/reference/api/get-entities-ref-signatures/"
STATES = "/concepts/states-locks-and-signatures/"
FIELD = "/reference/plugin-spec/field/"
STATE_FIELDS = "/guides/write-a-plugin/states/"

#: Every kind of page: the home page, a use case with a viewer's frame, a use case with
#: the live plate, a concept with a viewer and one in words, the API's overview, a call and
#: a schema, the plugin spec's overview and a kind's keys, a page of a guide in several
#: pages, the sections' indexes, and the page a host serves for a missing path.
PAGES = [
    "/",
    PREP,
    ASSAY,
    ALIGNMENTS,
    STATES,
    "/reference/",
    "/reference/api/",
    SIGNATURES,
    "/reference/api/schemas/SignaturesOut/",
    "/reference/plugin-spec/",
    FIELD,
    STATE_FIELDS,
    "/guides/",
    "/use-cases/",
    "/concepts/",
    "/404.html",
]

# Records CSP violations, in the page and in each frame: init scripts run in every frame,
# before its own scripts.
WATCH_CSP = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(`${event.effectiveDirective} ${event.blockedURI}`);
});
"""

RUN_AXE = """
async (tags) => {
  const result = await axe.run(document, { runOnly: { type: "tag", values: tags } });
  return result.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    nodes: v.nodes.map((n) => [n.target.join(" "), n.failureSummary].join(": ")),
  }));
}
"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    """The built site, served from its directory as a static host would."""
    if not (DIST / "pagefind" / "pagefind.js").is_file():
        pytest.fail(f"{DIST} isn't built: run make docs")
    if not AXE.is_file():
        pytest.fail(f"{AXE} is missing: run pnpm install --frozen-lockfile")
    handler = functools.partial(_Quiet, directory=str(DIST))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()


@pytest.fixture(scope="module")
def playwright() -> Iterator[Playwright]:
    with sync_playwright() as running:
        yield running


@pytest.fixture(scope="module", params=BROWSERS)
def browser(request: pytest.FixtureRequest, playwright: Playwright) -> Iterator[Browser]:
    try:
        launched = getattr(playwright, request.param).launch()
    except Exception as error:
        pytest.fail(f"{request.param} failed to launch: {error}\nRun make test-browsers.")
    yield launched
    launched.close()


class Opened:
    """A page, and what it did that it shouldn't: errors, and requests to other origins."""

    def __init__(self, page: Page, site: str) -> None:
        self.page = page
        self.errors: list[str] = []
        self.elsewhere: list[str] = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on(
            "request",
            lambda r: None if r.url.startswith((site, "data:")) else self.elsewhere.append(r.url),
        )

    def violations(self) -> list[str]:
        """CSP violations in the page and in every frame it holds."""
        found: list[str] = []
        for frame in self.page.frames:
            found += frame.evaluate("window.__cspViolations ?? []")
        return found


def open_page(
    browser: Browser,
    site: str,
    path: str,
    *,
    scheme: Literal["light", "dark"] = "light",
    width: int = 1440,
    script: bool = True,
) -> Opened:
    context = browser.new_context(
        base_url=site,
        color_scheme=scheme,
        viewport={"width": width, "height": 900},
        java_script_enabled=script,
    )
    context.add_init_script(WATCH_CSP)
    opened = Opened(context.new_page(), site)
    opened.page.goto(path)
    opened.page.get_by_role("heading", level=1).first.wait_for()
    return opened


def background_is_dark(page: Page) -> bool:
    colour = page.evaluate("getComputedStyle(document.body).backgroundColor")
    channels = [int(c) for c in re.findall(r"\d+", colour)[:3]]
    return sum(channels) / 3 < 128


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_kind_of_page_is_accessible_and_keeps_its_policy(
    browser: Browser, site: str, scheme: Literal["light", "dark"], width: int
) -> None:
    for path in PAGES:
        opened = open_page(browser, site, path, scheme=scheme, width=width)
        page = opened.page
        page.wait_for_load_state("networkidle")
        assert background_is_dark(page) == (scheme == "dark"), path
        # Nothing wider than the window: tables and code scroll in their own frames.
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, (path, overflow)
        assert opened.violations() == [], path
        assert opened.errors == [], path
        assert opened.elsewhere == [], path
        page.evaluate(AXE.read_text())
        violations = page.evaluate(RUN_AXE, WCAG_22_AA)
        assert violations == [], (path, violations)
        page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_prep_use_case_draws_the_plugins_own_map_in_its_theme(
    browser: Browser, site: str, scheme: Literal["light", "dark"]
) -> None:
    opened = open_page(browser, site, PREP, scheme=scheme)
    page = opened.page
    frame = page.frame_locator("iframe[data-viewer]")
    viewer = page.locator("iframe[data-viewer]")
    viewer.scroll_into_view_if_needed()
    # The plasmid sequencing plugin's Map tab, on the alignment the build ran.
    expect(frame.get_by_role("button", name="Differences (6)")).to_be_visible()
    expect(frame.get_by_text("o.887del").first).to_be_attached()
    # The page sizes the frame to what it drew, so it never scrolls on its own.
    inner = frame.locator("html")
    page.wait_for_function(
        """() => {
          const f = document.querySelector("iframe[data-viewer]");
          const drawn = f.contentDocument.documentElement.scrollHeight;
          return drawn > 400 && Math.abs(f.clientHeight - drawn) <= 1;
        }"""
    )
    # Choosing the other theme here changes the frame's too.
    other = "light" if scheme == "dark" else "dark"
    page.get_by_role("button", name=other.capitalize(), exact=True).click()
    expect(inner).to_have_attribute("data-theme", other)
    assert opened.violations() == []
    assert opened.errors == []
    assert opened.elsewhere == []
    page.context.close()


def test_the_worked_plate_switches_between_its_two_recorded_fits(
    browser: Browser, site: str
) -> None:
    opened = open_page(browser, site, ASSAY)
    page = opened.page
    figure = page.locator("[data-plate-assay]")
    toggle = figure.get_by_role("button", name="Exclude A4: Simulated dispensing error")
    expect(figure.get_by_text("12.07 nM")).to_be_visible()
    expect(figure.get_by_text("8 of 8, within range")).to_be_visible()
    toggle.click()
    expect(figure.get_by_role("button", name="Include A4 again")).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(figure.get_by_text("12.34 nM")).to_be_visible()
    expect(figure.get_by_text("10.65–14.30 nM")).to_be_visible()
    expect(figure.get_by_text("7 of 8, within range")).to_be_visible()
    expect(figure.get_by_role("img", name=re.compile("with A4 excluded"))).to_be_visible()
    expect(figure.get_by_text("Excluded from the fit")).to_be_visible()
    # A well shows its reading when chosen.
    figure.get_by_role("button", name="A8, Sample, 0.175143 AU").click()
    expect(figure.get_by_text("128 nM, replicate 1, file row 10")).to_be_visible()
    expect(figure.get_by_text("Excluded from the fit")).to_be_hidden()
    figure.get_by_role("button", name="Include A4 again").click()
    expect(figure.get_by_text("12.07 nM")).to_be_visible()
    assert opened.violations() == []
    assert opened.errors == []
    page.context.close()


def test_a_spec_key_is_a_place_to_link_to(browser: Browser, site: str) -> None:
    opened = open_page(browser, site, f"{FIELD}#states.on_edit")
    page = opened.page
    row = page.locator("tr", has=page.get_by_role("link", name="on_edit", exact=True))
    expect(row).to_be_in_viewport()
    expect(row).to_have_attribute("id", "states.on_edit")
    # A field type's keys link to each key's row.
    by_type = page.get_by_role("region", name="The keys each field type takes")
    by_type.get_by_role("link", name="link", exact=True).first.click()
    expect(page.locator("tr#link")).to_be_in_viewport()
    assert opened.errors == []
    page.context.close()


def test_a_guide_in_several_pages_links_its_pages(browser: Browser, site: str) -> None:
    opened = open_page(browser, site, STATE_FIELDS)
    page = opened.page
    pages = page.get_by_role("navigation", name="Pages for this audience")
    expect(pages.get_by_role("link", name="Write a plugin", exact=True)).to_be_visible()
    expect(pages.locator("a[aria-current=page]")).to_have_attribute("href", STATE_FIELDS)
    pager = page.get_by_role("navigation", name="This guide's pages")
    expect(pager.locator("a[rel=prev]")).to_be_visible()
    pager.locator("a[rel=next]").click()
    expect(page.get_by_role("navigation", name="Breadcrumbs")).to_contain_text("Write a plugin")
    assert opened.errors == []
    page.context.close()


def test_a_chosen_theme_holds_across_pages(browser: Browser, site: str) -> None:
    opened = open_page(browser, site, "/", scheme="light")
    page = opened.page
    page.get_by_role("button", name="Dark", exact=True).click()
    expect(page.get_by_role("button", name="Dark", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert background_is_dark(page)
    page.goto(ASSAY)
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert background_is_dark(page)
    page.get_by_role("button", name="System", exact=True).click()
    assert not background_is_dark(page)
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") is None
    assert opened.violations() == []
    page.context.close()


def test_search_finds_pages_offline(browser: Browser, site: str) -> None:
    opened = open_page(browser, site, "/")
    page = opened.page
    page.get_by_role("searchbox", name="Search the docs").fill("dispensing error")
    expect(page.get_by_role("status").filter(has_text="dispensing error")).to_be_visible()
    hit = page.locator("#search-results a").filter(has_text="Run a dose-response").first
    expect(hit).to_be_visible()
    page.get_by_role("searchbox", name="Search the docs").press("Enter")
    expect(hit.or_(page.locator("#search-results a").first)).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#search-results")).to_be_hidden()
    assert opened.violations() == []
    assert opened.errors == []
    assert opened.elsewhere == []
    page.context.close()


def test_no_sentence_is_split_into_paragraphs() -> None:
    """MDX turns text reflowed onto lines of its own inside an element into paragraphs: a
    caption or a link broken into pieces, as Prettier's MDX formatting does."""
    split = re.compile(r"<(a|figcaption|h3|p|strong|span)\b[^>]*>\s*<p>")
    found = [
        str(page.relative_to(DIST))
        for page in DIST.rglob("*.html")
        if split.search(page.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_no_page_cites_a_design_record() -> None:
    """The design records (ADRs) are on the developers' architecture site, not this one."""
    cited = re.compile(r"\bADRs? \d{4}\b|/decisions/|docs/adr/")
    found = [
        str(page.relative_to(DIST))
        for page in DIST.rglob("*.html")
        if cited.search(page.read_text(encoding="utf-8"))
    ]
    assert found == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_pages_read_without_scripts(
    browser: Browser, site: str, scheme: Literal["light", "dark"]
) -> None:
    opened = open_page(browser, site, ASSAY, scheme=scheme, script=False)
    page = opened.page
    assert background_is_dark(page) == (scheme == "dark")
    # The recorded fit, its curve and the steps are all there.
    expect(page.get_by_role("heading", name="Read the plate and fit the curve")).to_be_visible()
    expect(page.get_by_text("12.07 nM").first).to_be_visible()
    # What needs a script isn't offered: search, the theme switch, the plate's toggle.
    expect(page.get_by_role("searchbox")).to_be_hidden()
    expect(page.get_by_role("button", name="Dark", exact=True)).to_be_hidden()
    expect(page.get_by_role("button", name=re.compile("^Exclude A4"))).to_be_hidden()
    assert opened.elsewhere == []
    page.context.close()


def test_the_api_reference_links_a_call_to_the_guides_and_code_that_use_it(
    browser: Browser, site: str
) -> None:
    opened = open_page(browser, site, SIGNATURES)
    page = opened.page
    expect(page.get_by_role("heading", level=1)).to_have_text("A record's electronic signatures")
    related = page.get_by_role("complementary", name="Related")
    expect(related.get_by_role("link", name="Run a dose-response plate assay")).to_be_visible()
    expect(related.get_by_role("link", name="Verify a plasmid prep")).to_be_visible()
    expect(related.get_by_role("link", name=re.compile("client.signatures.list"))).to_be_visible()
    expect(related.get_by_role("link", name=re.compile("get_signatures"))).to_be_visible()
    page.get_by_role("link", name="SignaturesOut").first.click()
    expect(page.get_by_role("heading", level=1)).to_have_text("SignaturesOut")
    page.context.close()

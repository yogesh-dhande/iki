"""The web app in real browsers: Chromium, Firefox and WebKit against the real
server (production settings, collected static files, uvicorn), with the
production Content Security Policy and Trusted Types enforced.

Every page is checked with axe against WCAG 2.2 AA, in light and dark mode,
and must log no errors and break no CSP rule. Needs the built app (``make web``)
and browsers (``make test-browsers``).
"""

import contextlib
import itertools
import os
import re
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from browser_evidence import DISPOSABLE_BROWSER
from conftest import S3_ACCESS_KEY, S3_SECRET_KEY
from django.conf import settings
from django.test import Client
from iki_test_plugins import ensure_built
from org_world import World, build_world
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    Route,
    expect,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from real_server import Server, app_role, manage, quiet, run_tasks, serve, server_env

from iki_server.accounts.models import User
from iki_server.entities.models import Entity
from iki_server.notifications import kinds
from iki_server.notifications.services import notify
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.web import frontend
from iki_server.webhooks import services as webhooks

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

BROWSERS = ["chromium", "firefox", "webkit"]
WCAG_22_AA = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
AXE = Path(__file__).parents[2] / "node_modules/axe-core/axe.min.js"

# Records CSP violations; init scripts run before the page's own, CSP or not.
WATCH_CSP = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(`${event.effectiveDirective} ${event.blockedURI}`);
});
"""

# Records where each click landed and whether anything cancelled it: when a click does
# nothing, what it hit says why.
WATCH_CLICKS = """
window.__clicks = [];
window.addEventListener("click", (event) => {
  const t = event.target instanceof Element ? event.target : null;
  const link = t?.closest("a, button");
  window.__clicks.push({
    at: [Math.round(event.clientX), Math.round(event.clientY)],
    target: t ? `${t.tagName.toLowerCase()}.${t.className}` : String(event.target),
    in: link ? `${link.tagName.toLowerCase()} "${link.textContent.trim().slice(0, 40)}"` : null,
    cancelled: event.defaultPrevented,
    when: Math.round(performance.now()),
  });
});
"""

# What's on screen has settled: finite animations (fades) have finished, and spinners run
# for ever. React Aria marks a dialog or popover with data-entering or data-exiting until
# its fade ends. That's the signal to trust, not an animation's playState alone: WebKit
# works playState out from the clock when asked, but applies an animation to styles only
# at the next frame, so between frames a fade can say it's finished while its element is
# still drawn part-way, and when frames are far apart (a busy CI machine) axe saw a dialog
# just opened at a fraction of its opacity and its text failing contrast.
FADES_DONE = """
() => !document.querySelector("[data-entering], [data-exiting]")
  && document.getAnimations().every(
    (a) => !a.pending
      && (a.playState !== "running" || a.effect.getComputedTiming().endTime === Infinity),
  )
"""

# Two frames: the browser has worked out styles at least once since the fades finished.
NEXT_FRAMES = """
() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(true))))
"""

RUN_AXE = """
async (tags) => {
  // Iki's pages, not plugins' components in their frames: those are their plugins' to
  // check, and their origin is no one's, so axe couldn't reach them anyway (ADR 0084).
  const result = await axe.run(document, {
    runOnly: { type: "tag", values: tags },
    iframes: false,
  });
  return result.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    // With what axe measured (a contrast ratio and its colours, say), for when it's flaky.
    nodes: v.nodes.map((n) => [n.target.join(" "), n.failureSummary].join(": ")),
  }));
}
"""


@pytest.fixture(scope="module", autouse=True)
def _orm_beside_playwright() -> Iterator[None]:
    # Sync Playwright keeps an event loop in this thread, so Django assumes the
    # ORM is called from async code. Nothing runs concurrently: allow it here.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
        yield


@pytest.fixture(scope="module")
def server(
    browser_app_assets: None,
    django_db_setup: None,
    django_db_blocker: Any,
    tmp_path_factory: pytest.TempPathFactory,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> Iterator[Server]:
    with django_db_blocker.unblock(), app_role() as role:
        # The test process signs people in, so the server shares its secret key. It
        # serves the fixture plugins the test database has tables for.
        plugins = os.pathsep.join(
            p for p in (str(ensure_built()), os.environ.get("PYTHONPATH")) if p
        )
        env = server_env(
            role,
            tmp_path_factory.mktemp("static"),
            IKI_SECRET_KEY=settings.SECRET_KEY,
            PYTHONPATH=plugins,
            # Browsers upload straight to this store, as they would to S3.
            IKI_FILES_BUCKET=_s3_bucket,
            IKI_FILES_PREFIX=f"browser-{uuid.uuid4().hex}",
            IKI_FILES_ENDPOINT_URL=_s3_endpoint,
            # The tests' deployment, so backend actions can run.
            IKI_COMPUTE_FILE=os.environ["IKI_COMPUTE_FILE"],
            AWS_ACCESS_KEY_ID=S3_ACCESS_KEY,
            AWS_SECRET_ACCESS_KEY=S3_SECRET_KEY,
            # It signs the bundles it exports with the tests' key.
            IKI_BUNDLE_SIGNING_KEY=settings.IKI_BUNDLE_SIGNING_KEY,
            # ...and imports those the test process signs, as "test".
            IKI_BUNDLE_TRUSTED_KEYS="test=ed25519:riAtz4Nv0SFcXARze-_ieUqB027N1ie892Fk7U0Ao-4",
            # Plugins' writers run as a development server runs them: each export in a
            # child process of the server's own (ADR 0230).
            IKI_EXPORT_SANDBOX="local",
            IKI_ALLOW_LOCAL_PLUGINS="true",
        )
        env["IKI_PUBLIC_URL"] = f"http://127.0.0.1:{env['IKI_PORT']}"
        for command in (["collectstatic", "--noinput"], ["setup_storage"]):
            done = manage(*command, env=env)
            assert done.returncode == 0, done.stdout + done.stderr
        with serve(env) as running:
            yield running


@pytest.fixture(scope="module")
def playwright() -> Iterator[Playwright]:
    with sync_playwright() as running:
        yield running


FIREFOX_PREFS = {
    # Headless Firefox on Linux turns WebGL off without a GPU; its software renderer
    # draws Mol* well enough to test (Chromium and WebKit use theirs anyway).
    "webgl.force-enabled": True,
    # When the machine's network changes (Docker adding or removing a container's
    # interface, as testcontainers and other checks on the same machine do), Firefox
    # closes each connection that carries nothing for the next 5 seconds
    # (nsHttpConnectionMgr::VerifyTraffic): an open but quiet /api/v1/events stream, which
    # the page then logs as "interrupted while the page was loading". The tests reach the
    # server at 127.0.0.1, which no network change moves.
    "network.notify.changed": False,
}


@pytest.fixture(scope="module", params=BROWSERS)
def browser(request: pytest.FixtureRequest, playwright: Playwright) -> Iterator[Browser]:
    options: dict[str, Any] = (
        {"firefox_user_prefs": FIREFOX_PREFS} if request.param == "firefox" else {}
    )
    try:
        launched = getattr(playwright, request.param).launch(**options)
    except PlaywrightError as error:
        pytest.fail(f"{request.param} failed to launch: {error}\nRun make test-browsers.")
    request.node.stash[DISPOSABLE_BROWSER] = (request.fixturename, launched)
    try:
        yield launched
    finally:
        del request.node.stash[DISPOSABLE_BROWSER]
        launched.close()


@pytest.fixture
def world(server: Server) -> World:
    return build_world()


#: Errors each page logged (uncaught exceptions and console errors).
PAGE_ERRORS: dict[Page, list[str]] = {}
#: Requests each page made that failed, and why ("GET /x: net::ERR_ABORTED"). Not errors in
#: themselves (a page cancels what it no longer needs), but what a failure is said with.
FAILED_REQUESTS: dict[Page, list[str]] = {}
#: Requests each page has sent and had no answer to yet, and when each was sent.
IN_FLIGHT: dict[Page, dict[Any, tuple[float, str]]] = {}
#: Where each page has been: each address it loaded or the app moved it to.
VISITED: dict[Page, list[str]] = {}

#: What WebKit says of a fetch it refuses. It refuses one that fails a CORS check, and any a
#: document starts once it's being left: that one it never sends, so no request is made or
#: fails. It reports either as an uncaught error, which Playwright splits at the first colon,
#: the scheme's, leaving "/127.0.0.1:…/api/v1/… due to access control checks." as its message.
_REFUSED = re.compile(r"Fetch API cannot load (\S+) due to access control checks\.")


def refused(error: Any) -> str | None:
    """The address of the fetch a WebKit page error says it refused, if that's what it says."""
    said = (error.stack or "").split("\n", 1)[0] or f"{error.name}:/{error.message}"
    found = _REFUSED.fullmatch(said)
    return found[1] if found else None


def watch(page: Page) -> Page:
    """Records what goes wrong on the page, from now on: its errors in `PAGE_ERRORS`,
    which `assert_clean` checks, its failed requests in `FAILED_REQUESTS`, and what it's
    still waiting for in `IN_FLIGHT`. `what_happened` says it all when a test fails."""
    errors: list[str] = []
    failed: list[str] = []
    waiting: dict[Any, tuple[float, str]] = {}
    #: Whether the page has begun to leave its document: from its next document's request
    #: until it's on it.
    leaving: list[str] = []

    def bare(url: str) -> str:
        return urllib.parse.urldefrag(url).url

    def leaves_for(request: Any) -> bool:
        return bool(request.is_navigation_request() and request.frame == page.main_frame)

    def sent(request: Any) -> None:
        waiting[request] = (time.monotonic(), f"{request.method} {request.url}")
        if leaves_for(request):
            leaving[:] = [bare(request.url)]

    def done(request: Any) -> None:
        waiting.pop(request, None)

    def fail(request: Any) -> None:
        done(request)
        if leaves_for(request):
            leaving.clear()
        failed.append(f"{request.method} {request.url}: {request.failure}")

    def thrown(error: Any) -> None:
        # Not an error (the ResizeObserver spec): what a size change made while observers were
        # called is delivered in the next frame. WebKit reports it when a popover grows as
        # what it read arrives and react-aria places it again, above its button.
        if str(error) == "ResizeObserver loop completed with undelivered notifications.":
            return
        # Still an error when the page was leaving: its reads stop as a navigation away
        # begins (page-reads.ts), so one started then is one that should have waited.
        url = refused(error)
        if url:
            when = " as the page was being left" if leaving else ""
            errors.append(f"WebKit refused a fetch of {url}{when}: {error.stack or error}")
            return
        errors.append(str(error))

    page.on("pageerror", thrown)

    def logged(message: Any) -> None:
        if message.type == "error":
            errors.append(message.text)

    page.on("console", logged)
    page.on("request", sent)
    page.on("requestfinished", done)
    page.on("requestfailed", fail)

    # A console error says a request failed ("the server responded with a status of 404"),
    # but not which: this does.
    def answered(response: Any) -> None:
        if response.status >= 400:
            failed.append(f"{response.request.method} {response.url}: {response.status}")
        # A navigation answered with no content leaves the page where it was.
        if response.status in (204, 205) and leaves_for(response.request):
            leaving.clear()

    page.on("response", answered)
    visited: list[str] = []

    def navigated(frame: Any) -> None:
        if frame != page.main_frame:
            return
        visited.append(frame.url)
        if leaving and bare(frame.url) == leaving[0]:
            leaving.clear()

    page.on("framenavigated", navigated)
    VISITED[page] = visited
    PAGE_ERRORS[page] = errors
    FAILED_REQUESTS[page] = failed
    IN_FLIGHT[page] = waiting
    return page


#: What in a server's log says something went wrong in it.
_WRONG = re.compile(r"Traceback|\"level\": \"(?:error|critical)\"|\"status\": 5\d\d\b")


def went_wrong(server: Server, since: int) -> str:
    """What went wrong in the server since it had printed `since` lines, for a report."""
    lines = server.output[since:]
    found = [i for i, line in enumerate(lines) if _WRONG.search(line)]
    # A traceback's lines follow its first.
    shown = sorted({j for i in found for j in range(i, min(i + 30, len(lines)))})
    return "".join(lines[j] for j in shown[-200:]) or "nothing"


def what_happened(page: Page) -> str:
    """The page's errors, its failed requests, and those it's still waiting for: what a
    page that didn't get where a test expected is to be judged by."""
    now = time.monotonic()
    waiting = [f"{what} ({now - sent:.1f}s)" for sent, what in IN_FLIGHT.get(page, {}).values()]
    try:
        clicks = page.evaluate("window.__clicks")
    except Exception as error:  # it's only for the report
        clicks = repr(error)
    return (
        f"{page.url}\n  went through: {VISITED.get(page)}\n  errors: {PAGE_ERRORS.get(page)}"
        f"\n  failed requests: {FAILED_REQUESTS.get(page)}\n  requests not yet answered: "
        f"{waiting}\n  clicks: {clicks}"
    )


def shown(page: Page) -> None:
    """Waits for the page's heading: the app has started and drawn the page."""
    try:
        page.locator("h1").first.wait_for()
    except PlaywrightTimeoutError as error:
        raise AssertionError(f"No heading was drawn. {what_happened(page)}") from error


def session_cookie(user: User) -> str:
    client = Client()
    client.force_login(user)
    return client.cookies[settings.SESSION_COOKIE_NAME].value


@pytest.fixture
def open_as(
    browser: Browser,
    server: Server,
    world: World,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    contexts: list[BrowserContext] = []
    since = len(server.output)

    def open_as(
        principal: str, path: str, color_scheme: str = "light", timezone_id: str | None = None
    ) -> Page:
        context = browser.new_context(
            base_url=server.url,
            color_scheme=color_scheme,  # type: ignore[arg-type]
            timezone_id=timezone_id,
        )
        contexts.append(context)
        close = context.close

        def close_context(*, reason: str | None = None) -> None:
            if context.is_closed():
                return
            # Tests also close their contexts directly. A parked request may never finish;
            # retire its handler before closing disposes the request client used by fetch().
            try:
                for tab in context.pages:
                    tab.unroute_all(behavior="ignoreErrors")
                context.unroute_all(behavior="ignoreErrors")
            finally:
                close(reason=reason)

        monkeypatch.setattr(context, "close", close_context)
        user = world.users[principal]
        # The server runs without HTTPS, so its cookie has no __Host- prefix.
        context.add_cookies(
            [{"name": "iki_session", "value": session_cookie(user), "url": server.url}]
        )
        context.add_init_script(WATCH_CSP)
        context.add_init_script(WATCH_CLICKS)
        page = watch(context.new_page())
        page.goto(path)
        shown(page)
        return page

    yield open_as
    call = getattr(request.node, "rep_call", None)
    if call is not None and call.failed:
        seen = "\n".join(what_happened(page) for context in contexts for page in context.pages)
        request.node.add_report_section("teardown", "the pages", seen)
        request.node.add_report_section("teardown", "the server", went_wrong(server, since))
    for context in contexts:
        context.close()
    quiet()


@pytest.mark.parametrize("scope", ["page", "context"])
def test_a_retired_route_cannot_fail_the_next_browser_context(open_as: Any, scope: str) -> None:
    """Leave a real response body unread until the context that requested it has closed."""
    first: Page = open_as("org_admin", "/admin/sign-in/")
    following: Page = open_as("org_admin", "/admin/sign-in/")

    def roles(route: Route) -> None:
        response = route.fetch()
        following.evaluate("document.documentElement.dataset.rolesFetched = 'yes'")
        try:
            following.locator("html[data-read-roles]").wait_for(timeout=10000)
            route.fulfill(json=response.json())
        finally:
            following.evaluate("document.documentElement.dataset.handlerReturned = 'yes'")

    url = "**/api/v1/lab-roles"
    owner = first if scope == "page" else first.context
    owner.route(url, roles)
    first.evaluate("() => { void fetch('/api/v1/lab-roles') }")
    following.locator("html[data-roles-fetched]").wait_for()
    first.context.close()
    following.evaluate("document.documentElement.dataset.readRoles = 'yes'")
    following.locator("html[data-handler-returned]").wait_for()
    assert following.title()


def test_a_live_route_still_reports_its_handler_error(open_as: Any) -> None:
    page: Page = open_as("org_admin", "/admin/sign-in/")

    def broken(route: Route) -> None:
        route.fulfill(json=[])
        raise ValueError("broken lab roles fixture")

    page.route("**/api/v1/lab-roles", broken)
    with pytest.raises(ValueError, match="broken lab roles fixture"):
        page.evaluate("fetch('/api/v1/lab-roles').then(r => r.json())")
        page.title()


def assert_clean(page: Page) -> None:
    """No CSP violations, no errors, and nothing axe finds against WCAG 2.2 AA, in every
    engine (Firefox's reads of ARIA's element properties: see conftest)."""
    assert page.evaluate("window.__cspViolations") == [], page.url
    assert PAGE_ERRORS[page] == [], (page.url, FAILED_REQUESTS.get(page))
    # Mid-fade text has less contrast than it ends up with: check the end state, once it's
    # drawn, and still settled then.
    page.wait_for_function(FADES_DONE)
    page.evaluate(NEXT_FRAMES)
    page.wait_for_function(FADES_DONE)
    page.evaluate(AXE.read_text())
    violations = page.evaluate(RUN_AXE, WCAG_22_AA)
    assert violations == [], (page.url, violations)


def add_filter(page: Page, field: str) -> Locator:
    """Adds a grid filter by a field, from its "+ Filter" menu, and returns the filter's
    editor, which opens."""
    page.get_by_role("button", name="Filter", exact=True).click()
    menu = page.get_by_role("menu", name="Filter")
    menu.get_by_role("menuitem", name=re.compile(f"^{re.escape(field)}")).click()
    editor = page.get_by_role("dialog", name=re.compile(r"^Filter \d+: " + re.escape(field)))
    editor.wait_for()
    return editor


#: (who, page): the web app's pages, and server-rendered pages that share its design.
PAGES = [
    ("admin", "/"),
    ("admin", "/projects/"),
    ("admin", "/projects/new/"),
    ("admin", "/projects/kinase-atlas/"),
    ("admin", "/projects/kinase-atlas/members/"),
    ("admin", "/projects/kinase-atlas/settings/"),
    ("org_admin", "/teams/"),
    ("org_admin", "/teams/structural-biology/"),
    ("admin", "/notifications/"),
    ("admin", "/work/"),
    ("admin", "/work/?open=true"),
    ("admin", "/account/apps/"),
    ("admin", "/account/notifications/"),
    ("org_admin", "/admin/email/"),
    ("admin", "/projects/kinase-atlas/automation/webhooks/"),
    ("admin", "/projects/kinase-atlas/automation/triggers/"),
    ("org_admin", "/admin/webhooks/"),
    ("compute_admin", "/admin/compute/"),
    ("compute_admin", "/admin/compute/workers/"),
    ("compute_admin", "/admin/compute/slurm/"),
    ("compute_admin", "/admin/compute/quotas/"),
    ("compute_admin", "/admin/compute/images/"),
    ("org_admin", "/admin/registries/"),
    ("org_admin", "/admin/signatures/"),
    ("org_admin", "/admin/people/"),
    ("org_admin", "/admin/sign-in/"),
    ("auditor", "/admin/audit/"),
    ("admin", "/entities/"),
    ("admin", "/entities/types/acme/protein.Protein/"),
    ("admin", "/requests/"),
    ("admin", "/runs/"),
    ("admin", "/files/"),
    ("admin", "/projects/kinase-atlas/files/"),
]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_pages_are_accessible_and_keep_the_policy(open_as: Any, color_scheme: str) -> None:
    for principal, path in PAGES:
        page = open_as(principal, path, color_scheme)
        assert_clean(page)
        page.context.close()


def _focused(locator: Locator) -> bool:
    return bool(locator.evaluate("e => e === document.activeElement"))


@pytest.mark.parametrize(
    ("path", "color_scheme"),
    # The web app's sidebar and the server's, in each theme.
    [("/projects/", "light"), ("/teams/", "dark")],
)
def test_the_sidebar_by_keyboard_and_as_a_drawer_on_a_phone(
    open_as: Any, path: str, color_scheme: str
) -> None:
    """The sidebar (ADR 0176): Entities is closed away from its pages until its reader opens
    it, then stays as they left it, the account menu opens and closes from the keyboard, its
    button opens search as ⌘K does, and on a phone it's a drawer that holds the focus until
    it closes, with the page never wider than the screen."""
    page = open_as("org_admin", path, color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    nav = page.get_by_role("navigation", name="Main")
    entities = nav.get_by_role("button", name="Entities")
    assert entities.get_attribute("aria-expanded") == "false"
    # Closed, it takes no room: what's below it moves up.
    all_entities = page.locator("#nav-entities").get_by_role("link", name="All entities")
    assert all_entities.count() == 0 or not all_entities.is_visible()
    assert page.locator("#nav-entities").evaluate("e => e.getBoundingClientRect().height") == 0
    entities.focus()
    page.keyboard.press("Enter")
    assert entities.get_attribute("aria-expanded") == "true"
    nav.get_by_role("link", name="All entities").wait_for()
    page.reload()
    shown(page)
    assert entities.get_attribute("aria-expanded") == "true"

    account = page.get_by_role("button", name="Org Admin")
    account.focus()
    page.keyboard.press("Enter")
    page.get_by_role("link", name="Notification preferences").wait_for()
    page.get_by_role("button", name="Sign out").wait_for()
    assert_clean(page)
    page.keyboard.press("Escape")
    assert account.get_attribute("aria-expanded") == "false"
    assert _focused(account)

    # Search everything: the palette, from the shortcut and from the button.
    palette = page.get_by_role("dialog", name="Search everything")
    page.keyboard.press("ControlOrMeta+k")
    palette.wait_for()
    page.keyboard.press("Escape")
    palette.wait_for(state="hidden")
    page.get_by_role("button", name="Search everything").click()
    palette.wait_for()
    page.keyboard.press("Escape")
    palette.wait_for(state="hidden")

    page.set_viewport_size({"width": 390, "height": 844})
    width = "document.documentElement.scrollWidth - window.innerWidth <= 1"
    assert page.evaluate(width)
    nav.wait_for(state="hidden")
    menu = page.get_by_role("button", name="Menu", exact=True)
    menu.click()
    drawer = page.get_by_role("dialog", name="Menu")
    close = drawer.get_by_role("button", name="Close menu")
    page.wait_for_function("() => document.activeElement?.closest('[role=dialog]') !== null")
    assert _focused(close)
    assert page.evaluate(width)
    assert_clean(page)
    page.keyboard.press("Escape")
    drawer.wait_for(state="hidden")
    assert _focused(menu)
    menu.click()
    drawer.get_by_role("link", name="Types").click()
    page.wait_for_url("**/types/")
    drawer.wait_for(state="hidden")
    assert page.evaluate(width)
    page.context.close()


@pytest.mark.parametrize(
    ("path", "current"),
    # The web app's sidebar and the server's.
    [("/projects/", "All projects"), ("/teams/", "People and teams")],
)
def test_the_sidebars_list_scrolls_between_its_head_and_foot_to_the_current_item(
    open_as: Any, path: str, current: str
) -> None:
    """On a short screen, the sidebar's list scrolls on its own, between search at its top
    and the account at its foot, which stay put; on load it's scrolled to the page's item."""
    page = open_as("admin", path)
    page.context.add_cookies([{"name": "iki_entities", "value": "open", "url": page.url}])
    page.set_viewport_size({"width": 1440, "height": 320})
    page.reload()
    shown(page)
    nav = page.get_by_role("navigation", name="Main")
    item = nav.get_by_role("link", name=current, exact=True)
    item.wait_for()
    page.wait_for_function(
        "() => { const n = document.querySelector('.sidebar-nav');"
        " const i = n.querySelector('[aria-current=page]');"
        " const a = n.getBoundingClientRect(), b = i.getBoundingClientRect();"
        " return b.top >= a.top - 1 && b.bottom <= a.bottom + 1; }"
    )
    assert item.get_attribute("aria-current") == "page"
    assert nav.evaluate("n => n.scrollHeight > n.clientHeight && n.scrollTop > 0")
    search = page.get_by_role("button", name="Search everything").bounding_box()
    account = page.get_by_role("button", name="Admin", exact=True).bounding_box()
    box = nav.bounding_box()
    assert search is not None and account is not None and box is not None
    assert search["y"] + search["height"] <= box["y"] + 1
    assert account["y"] >= box["y"] + box["height"] - 1
    assert account["y"] + account["height"] <= 320 + 1
    assert page.evaluate("window.scrollY") == 0
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_pages_breadcrumb_and_actions_are_in_a_bar_across_its_top(
    open_as: Any, color_scheme: str
) -> None:
    """The bar across the top of the page (ADR 0176): 46px tall, across the whole column
    beside the sidebar, with the breadcrumb on its left and the page's actions on its right;
    then the page's title, with what it is in one line beside it and more in a toggletip.
    On a phone the page is never wider than the screen."""
    page = open_as("admin", "/entities/types/acme/protein.Protein/", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    bar = page.locator(".page-top")
    crumbs = bar.get_by_role("navigation", name="Breadcrumb")
    crumbs.get_by_role("link", name="Entities").wait_for()
    assert crumbs.get_by_text("Proteins").get_attribute("aria-current") == "page"
    box = bar.bounding_box()
    side = page.locator("#sidebar").bounding_box()
    assert box is not None and side is not None
    assert box["height"] == pytest.approx(46, abs=1)
    assert box["y"] == pytest.approx(0, abs=1)
    assert box["x"] == pytest.approx(side["x"] + side["width"], abs=1)
    assert box["x"] + box["width"] == pytest.approx(1440, abs=1)
    new = bar.get_by_role("button", name="New protein")
    new_box = new.bounding_box()
    crumbs_box = crumbs.bounding_box()
    assert new_box is not None and crumbs_box is not None
    # The breadcrumb on the left and the actions on the right, both in the bar's middle.
    assert crumbs_box["x"] < box["x"] + 30
    assert new_box["x"] + new_box["width"] == pytest.approx(box["x"] + box["width"] - 20, abs=2)
    middle = box["y"] + box["height"] / 2
    for part in (new_box, crumbs_box):
        assert part["y"] + part["height"] / 2 == pytest.approx(middle, abs=2)

    # The title below the bar, and the line about it beside it.
    title = page.get_by_role("heading", level=1, name="Proteins").bounding_box()
    about = page.get_by_role("button", name="About Proteins")
    about_box = about.bounding_box()
    assert title is not None and about_box is not None
    assert title["y"] > box["y"] + box["height"]
    assert about_box["x"] > title["x"] + title["width"]
    assert about_box["y"] + about_box["height"] / 2 == pytest.approx(
        title["y"] + title["height"] / 2, abs=3
    )
    about.click()
    toggletip = page.get_by_role("dialog", name="About Proteins")
    toggletip.get_by_role("link", name=re.compile("^About the Protein type")).wait_for()
    assert_clean(page)
    page.keyboard.press("Escape")
    toggletip.wait_for(state="hidden")

    # On a phone the title comes first: the bar keeps the breadcrumb, and the actions go
    # under the title.
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth <= 1")
    below = page.locator(".page-heading").get_by_role("button", name="New protein")
    below.wait_for()
    assert bar.get_by_role("button", name="New protein").count() == 0
    assert bar.get_by_role("navigation", name="Breadcrumb").is_visible()
    heading = page.get_by_role("heading", level=1, name="Proteins").bounding_box()
    shown_at = below.bounding_box()
    assert heading is not None and shown_at is not None
    assert heading["y"] + heading["height"] <= shown_at["y"], (heading, shown_at)
    assert_clean(page)
    page.context.close()


@pytest.mark.parametrize(
    ("principal", "path", "frame", "trail", "action"),
    [
        ("team_member", "/teams/", "#sidebar", ["Teams"], None),
        (
            "team_member",
            "/teams/structural-biology/",
            "#sidebar",
            ["Teams", "Structural Biology"],
            "New project",
        ),
        ("org_admin", "/admin/people/", "#admin-sidebar", ["Admin", "People"], "Service accounts"),
    ],
)
def test_a_server_pages_header_is_the_apps(
    open_as: Any,
    world: World,
    principal: str,
    path: str,
    frame: str,
    trail: list[str],
    action: str | None,
) -> None:
    """A server-rendered page has the bar the app's pages have (ADR 0176): 46px tall beside
    the sidebar, its breadcrumb on the left and its actions on the right, then its title. A
    line about it that doesn't fit opens whole in a toggletip. On a phone the actions go
    under the title, and the page is never wider than the screen."""
    world.team.description = (
        "Crystallography and cryo-EM of the kinases the atlas covers, from expression and"
        " purification to the deposited structure, with the beamline rota, the grid"
        " screening queue and the processing pipelines the team keeps for everyone."
    )
    world.team.save()
    page = open_as(principal, path)
    page.set_viewport_size({"width": 1440, "height": 900})
    crumbs = page.get_by_role("navigation", name="Breadcrumb")
    bar = page.locator(".page-top, .admin-top").filter(has=crumbs)
    assert crumbs.get_by_role("listitem").all_inner_texts() == trail
    assert crumbs.get_by_text(trail[-1], exact=True).get_attribute("aria-current") == "page"
    box = bar.bounding_box()
    side = page.locator(frame).bounding_box()
    assert box is not None and side is not None
    assert box["height"] == pytest.approx(46, abs=1)
    assert box["y"] == pytest.approx(0, abs=1)
    assert box["x"] == pytest.approx(side["x"] + side["width"], abs=1)
    assert box["x"] + box["width"] == pytest.approx(1440, abs=1)
    crumbs_box = crumbs.bounding_box()
    assert crumbs_box is not None
    assert crumbs_box["x"] < box["x"] + 30
    middle = box["y"] + box["height"] / 2
    assert crumbs_box["y"] + crumbs_box["height"] / 2 == pytest.approx(middle, abs=2)
    title = page.get_by_role("heading", level=1).bounding_box()
    assert title is not None and title["y"] > box["y"] + box["height"]
    if action:
        button = bar.get_by_role("link", name=action)
        at = button.bounding_box()
        assert at is not None
        assert at["x"] + at["width"] == pytest.approx(box["x"] + box["width"] - 20, abs=2)
        assert at["y"] + at["height"] / 2 == pytest.approx(middle, abs=2)
        # Drawn twice, for a phone too, and shown once.
        header = page.locator(".page-top, .admin-top, .page-heading")
        assert header.get_by_role("link", name=action).count() == 1
    else:
        assert bar.get_by_role("link").count() == bar.get_by_role("listitem").count() - 1
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)

    if path == "/teams/structural-biology/":
        # What it is, beside its title: too long for the line, so whole in a toggletip.
        about = page.get_by_role("button", name="About Structural Biology")
        about_box = about.bounding_box()
        assert about_box is not None and about_box["x"] > title["x"] + title["width"]
        assert about.get_attribute("aria-expanded") == "false"
        about.click()
        toggletip = page.get_by_role("dialog", name="About Structural Biology")
        toggletip.get_by_text("processing pipelines the team keeps").wait_for()
        assert about.get_attribute("aria-expanded") == "true"
        assert toggletip.evaluate("d => d === document.activeElement")
        assert_clean(page)
        page.keyboard.press("Escape")
        toggletip.wait_for(state="hidden")
        assert _focused(about)

    # On a phone the title comes first: the bar keeps the breadcrumb, and the actions go
    # under the title.
    page.set_viewport_size({"width": 390, "height": 844})
    assert crumbs.is_visible()
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    if action:
        assert bar.get_by_role("link", name=action).count() == 0
        below = page.locator(".page-heading").get_by_role("link", name=action)
        below.wait_for()
        heading = page.get_by_role("heading", level=1).bounding_box()
        shown_at = below.bounding_box()
        assert heading is not None and shown_at is not None
        assert heading["y"] + heading["height"] <= shown_at["y"], (heading, shown_at)
        assert shown_at["x"] + shown_at["width"] <= 390
    if path == "/teams/structural-biology/":
        # The desktop sidebar still covers the left edge while it slides away on resize.
        page.locator("#sidebar").wait_for(state="hidden")
        page.evaluate(NEXT_FRAMES)
        about = page.get_by_role("button", name="About Structural Biology")
        about.click()
        toggletip = page.get_by_role("dialog", name="About Structural Biology")
        toggletip.wait_for()
        tip = toggletip.bounding_box()
        assert tip is not None and tip["x"] >= 16 - 1 and tip["x"] + tip["width"] <= 390 - 16 + 1
        assert page.evaluate(NO_SIDEWAYS_SCROLL)
        page.mouse.click(5, 800)  # Elsewhere.
        toggletip.wait_for(state="hidden")
    assert_clean(page)
    page.context.close()


def test_a_compute_admin_registers_and_changes_self_hosted_machines(
    open_as: Any, world: World
) -> None:
    """A team's machine, its labels and what it's for (ADR 0112)."""
    from test_workers import LOCAL, ROOMY

    from iki_server.runs import workers
    from iki_server.runs.models import Worker

    registration, _ = workers.create_registration(
        label="Lab 3",
        tier="self_hosted",
        by=world.users["compute_admin"],
        scope=workers.make_scope([world.team.pk], []),
        labels=["chromeleon"],
    )
    workers.register(registration, name="hplc-pc-1", version="0.1.0", capacity=ROOMY, plugins=LOCAL)
    page = open_as("compute_admin", "/admin/compute/workers/")
    listed = page.locator(
        "section", has=page.get_by_role("heading", level=2, name="Workers", exact=True)
    )
    row = listed.get_by_role("row").filter(has=page.get_by_role("rowheader", name="hplc-pc-1"))
    scope = row.get_by_role("cell", name="Structural Biology")
    # A label is text in the cell: it has no role of its own.
    scope.get_by_text("chromeleon", exact=True).wait_for()
    assert_clean(page)

    row.get_by_role("button", name="Change hplc-pc-1").click()
    dialog = page.get_by_role("dialog", name="Change hplc-pc-1")
    dialog.get_by_role("checkbox", name="Structural Biology", exact=True).wait_for()
    assert_clean(page)
    dialog.get_by_role("textbox", name="Labels").fill("chromeleon, empower")
    dialog.get_by_role("button", name="Save").click()
    page.get_by_text("Changed hplc-pc-1.").wait_for()
    scope.get_by_text("empower", exact=True).wait_for()
    assert Worker.objects.get(name="hplc-pc-1").labels == ["chromeleon", "empower"]

    tokens = page.locator(
        "section", has=page.get_by_role("heading", level=2, name="Registration tokens", exact=True)
    )
    tokens.get_by_role("textbox", name="What it’s for").fill("Lab 4")
    tokens.get_by_role("combobox", name="Tier").select_option("self_hosted")
    tokens.get_by_role("checkbox", name="Structural Biology", exact=True).check()
    tokens.get_by_role("textbox", name="Labels").fill("gpu")
    assert_clean(page)
    tokens.get_by_role("button", name="Make token").click()
    tokens.get_by_text("Copy the token now.").wait_for()
    tokens.get_by_text("iki-worker register --url").wait_for()
    assert_clean(page)
    page.context.close()


NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"


def test_a_plugin_preflight_form_can_recover_if_its_module_doesnt_load(open_as: Any) -> None:
    page: Page = open_as("org_admin", "/admin/sign-in/")
    chunk = frontend.manifest()["src/plugin-preflight.tsx"]["file"]
    module_url = urllib.parse.urljoin(page.url, f"/static/app/{chunk}")
    page.route(module_url, lambda route: route.abort())
    page.get_by_role("navigation", name="Admin").get_by_role("link", name="Plugins").click()

    preflight = page.get_by_role("region", name="Schema preflight")
    alert = preflight.get_by_role("alert")
    alert.get_by_text("The preflight form couldn’t load.").wait_for()
    assert preflight.get_by_text("iki plugin preflight", exact=True).is_visible()
    assert page.page_errors(filter="since-navigation") == []

    page.unroute(module_url)
    alert.get_by_role("button", name="Reload the page").click()
    preflight.get_by_role("button", name="Run preflight").wait_for()
    assert alert.is_hidden()
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_plugin_preflight_shows_and_downloads_the_checked_revision(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    import json

    from iki_server.entities import services as entities
    from iki_server.plugins import baseline

    for project in (world.project, world.archived):
        ProjectMembership.objects.create(
            project=project, user=world.users["org_admin"], role=ProjectRole.VIEWER
        )
    baseline.keep()  # The last successful deploy, before proposing a new manifest.
    type_name = "acme/dna.DNASequence"
    made = entities.create(
        world.users["editor"],
        type_name,
        world.project,
        {"bases": "ATGCCGT", "topology": "circular", "name": "Reference"},
    )
    record = Entity.objects.get(pk=made.entity.pk)
    example = f"{record.hrid} ({record.pk})"
    manifest = json.loads((ensure_built() / "iki_plugin_acme__dna" / "manifest.json").read_text())
    manifest["plugin"]["version"] = "2.0.0"
    manifest["plugin"]["types"][type_name]["fields"]["topology"]["values"] = ["linear"]
    page: Page = open_as("org_admin", "/admin/plugins/acme/dna/", color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('input[type="file"]').set_input_files(
        {
            "name": "manifest.json",
            "mimeType": "application/json",
            "buffer": json.dumps(manifest).encode(),
        }
    )
    page.get_by_role("button", name="Run preflight").click()
    page.get_by_text(example, exact=True).first.wait_for()
    page.get_by_text("Exactly 1 affected record", exact=False).first.wait_for()
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    with page.expect_download() as downloading:
        page.get_by_role("button", name="Download report (JSON)").click()
    path = tmp_path / "report.json"
    downloading.value.save_as(path)
    report = json.loads(path.read_text())
    assert report["plugin"] == "acme/dna" and report["version"] == "2.0.0"
    assert report["blocking"]
    page.get_by_text(report["candidate_revision"], exact=True).wait_for()
    page.get_by_text(report["configuration_revision"], exact=True).wait_for()
    affected = next(t for t in report["types"] if t["type"] == type_name)
    assert affected["checked"] == affected["total"] == 1
    assert affected["sampled_checks"] == []
    assert any(
        f["count"] == 1 and not f["sampled"] and example in f["examples"]
        for f in affected["findings"]
    )


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_the_admin_area_has_a_frame_of_its_own(
    open_as: Any, world: World, color_scheme: str, browser_name: str
) -> None:
    """Its sidebar leads to its pages, the app's and the server's alike, and back to Iki;
    a keyboard reaches it all; and on a phone it's a drawer, as the main sidebar is, over
    the page and holding the focus until it closes (ADR 0176)."""
    from iki_server.org.models import OrgRole, OrgRoleAssignment
    from iki_server.web import admin_area

    for role in (OrgRole.COMPUTE_ADMIN, OrgRole.QUARANTINE_ADMIN):
        OrgRoleAssignment.objects.create(user=world.users["org_admin"], role=role)
    page = open_as("org_admin", "/admin/compute/", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("heading", name="Compute", level=1).wait_for()
    admin = page.get_by_role("navigation", name="Admin")
    assert page.get_by_role("navigation", name="Main").count() == 0
    offered = [i.label for g in admin_area.groups() for i in g.items if i.label != "Audit log"]
    assert admin.get_by_role("list").get_by_role("link").all_inner_texts() == offered
    assert admin.get_by_role("link", name="Compute").get_attribute("aria-current") == "page"
    crumbs = page.get_by_role("navigation", name="Breadcrumb")
    assert crumbs.get_by_role("listitem").all_inner_texts() == ["Admin", "Compute"]
    page.get_by_role("banner").get_by_role("link", name="Register a worker").wait_for()
    page.get_by_role("row", name=re.compile("^Backend")).wait_for()
    assert_clean(page)

    # By keyboard: past the skip link, back to Iki first, then the area's pages. WebKit,
    # as Safari, moves through links with Option-Tab.
    tab = "Alt+Tab" if browser_name == "webkit" else "Tab"
    focused = "document.activeElement.textContent.trim()"
    page.keyboard.press(tab)
    assert page.evaluate(focused) == "Skip to content"
    page.keyboard.press(tab)
    assert page.evaluate(focused) == "Back to Iki"
    page.keyboard.press(tab)
    assert page.evaluate(focused) == offered[0]

    # A server page, in the same frame, and from it the app's.
    admin.get_by_role("link", name="Plugins").click()
    page.get_by_role("heading", name="Plugins", level=1).wait_for()
    # The server draws the heading before the preflight form's module has loaded.
    page.get_by_role("button", name="Run preflight").wait_for()
    admin = page.get_by_role("navigation", name="Admin")
    assert admin.get_by_role("link", name="Plugins").get_attribute("aria-current") == "page"
    assert page.get_by_role("navigation", name="Main").count() == 0
    assert_clean(page)
    admin.get_by_role("link", name="Webhooks").click()
    page.get_by_role("heading", name="Webhooks", level=1).wait_for()
    assert (
        page.get_by_role("navigation", name="Admin")
        .get_by_role("link", name="Webhooks")
        .get_attribute("aria-current")
        == "page"
    )

    # On a phone, on either kind of page: no sideways scrolling, and the sidebar a drawer,
    # opened from the bar above the page. Its close button has the focus; Escape, the
    # backdrop and going somewhere close it.
    page.set_viewport_size({"width": 390, "height": 844})
    for path, heading in (("/admin/compute/workers/", "Compute"), ("/admin/sign-in/", None)):
        page.goto(path)
        if heading:
            page.get_by_role("heading", name=heading, level=1).wait_for()
            # The empty Workers list wraps to a second line at this width.
            page.get_by_role("region", name="Workers", exact=True).get_by_text(
                "No workers have registered yet."
            ).wait_for()
        admin = page.get_by_role("navigation", name="Admin")
        admin.wait_for(state="hidden")
        menu = page.get_by_role("button", name="Menu", exact=True)
        assert menu.get_attribute("aria-expanded") == "false"
        bar = menu.bounding_box()
        assert bar and bar["y"] < 10, path
        main = page.get_by_role("main").bounding_box()
        assert main and main["y"] < 120, (path, main)
        assert page.evaluate(NO_SIDEWAYS_SCROLL), path
        menu.click()
        drawer = page.get_by_role("dialog", name="Menu")
        close = drawer.get_by_role("button", name="Close menu")
        page.wait_for_function("() => document.activeElement?.closest('[role=dialog]') !== null")
        assert _focused(close), path
        assert menu.get_attribute("aria-expanded") == "true"
        drawer.get_by_role("link", name="People").wait_for()
        # The page under it doesn't move, and is out of reach.
        assert page.get_by_role("main").bounding_box() == main, path
        assert page.evaluate("document.getElementById('main').closest('[inert]') !== null")
        assert page.evaluate(NO_SIDEWAYS_SCROLL), path
        assert_clean(page)
        page.keyboard.press("Escape")
        drawer.wait_for(state="hidden")
        assert _focused(menu), path
        menu.click()
        page.mouse.click(380, 400)  # The backdrop, beside the drawer.
        drawer.wait_for(state="hidden")
        assert _focused(menu), path
    menu.click()
    page.get_by_role("dialog", name="Menu").get_by_role("link", name="Compute").click()
    page.get_by_role("heading", name="Compute", level=1).wait_for()
    page.get_by_role("dialog", name="Menu").wait_for(state="hidden")
    assert page.evaluate(NO_SIDEWAYS_SCROLL)

    # And back to Iki, in its own frame.
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("navigation", name="Admin").get_by_role("link", name="Back to Iki").click()
    page.get_by_role("navigation", name="Main").wait_for()
    assert page.get_by_role("navigation", name="Admin").count() == 0
    page.context.close()


def test_the_admin_areas_pages_fit_a_phone_and_say_what_isnt_yours(
    open_as: Any, world: World
) -> None:
    """Compute's workers by tier fit a phone whole: what each tier is goes under its name,
    so Idle, Offline and Manage are in view without scrolling the table. Sign-in's token
    actions look like the buttons they are. And /admin/, for someone none of it is for,
    says so without an error."""
    from iki_server.org.models import OrgRole, OrgRoleAssignment
    from iki_server.scim import tokens

    OrgRoleAssignment.objects.create(user=world.users["org_admin"], role=OrgRole.COMPUTE_ADMIN)
    page = open_as("org_admin", "/admin/compute/")
    page.set_viewport_size({"width": 390, "height": 844})
    table = page.get_by_role("table").first
    backend = table.get_by_role("row", name=re.compile("^Backend"))
    manage = backend.get_by_role("link", name="Manage Backend workers")
    manage.wait_for()
    assert table.evaluate("t => t.parentElement.scrollWidth <= t.parentElement.clientWidth")
    edge = table.bounding_box()
    shown_at = manage.bounding_box()
    assert edge and shown_at
    assert shown_at["x"] + shown_at["width"] <= edge["x"] + edge["width"], (edge, shown_at)
    assert (
        backend.get_by_role("rowheader")
        .get_by_text("Workers you run and register with a token")
        .is_visible()
    )
    assert page.get_by_role("columnheader", name="What it is").is_hidden()
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("columnheader", name="What it is").wait_for()
    assert table.get_by_role("cell", name="Workers you run and register with a token").is_visible()
    page.context.close()

    tokens.create(by=world.users["org_admin"])
    page = open_as("org_admin", "/admin/sign-in/")
    look = "b => ((s) => [s.borderTopWidth, s.borderTopColor, s.color])(getComputedStyle(b))"
    replace = page.get_by_role("button", name="Replace token").evaluate(look)
    turn_off = page.get_by_role("button", name="Turn off provisioning").evaluate(look)
    for border in (replace, turn_off):
        assert border[0] != "0px" and border[1] != "rgba(0, 0, 0, 0)", border
    assert turn_off[2] != replace[2]  # In the error's color: it stops sign-ins it provisions.
    assert_clean(page)
    page.context.close()

    for width in (1440, 390):
        page = open_as("viewer", "/admin/")
        page.set_viewport_size({"width": width, "height": 844})
        page.get_by_role("heading", name="The admin area isn’t open to you", level=1).wait_for()
        assert page.get_by_role("navigation", name="Admin").count() == 0
        assert page.evaluate(NO_SIDEWAYS_SCROLL)
        assert_clean(page)
        page.context.close()


@pytest.mark.parametrize(
    "path",
    # The web app's admin frame and the server's.
    ["/admin/compute/", "/admin/plugins/"],
)
def test_the_admin_drawer_ends_with_the_account_menu(open_as: Any, path: str) -> None:
    """On a short phone, the admin area's drawer keeps the account at its foot, below the
    list, which scrolls; it opens the same menu as the main sidebar's, Sign out last."""
    page = open_as("org_admin", path)
    page.set_viewport_size({"width": 390, "height": 480})
    page.get_by_role("button", name="Menu", exact=True).click()
    drawer = page.get_by_role("dialog", name="Menu")
    account = drawer.get_by_role("button", name="Org Admin", exact=True)
    account.wait_for()
    nav = drawer.get_by_role("navigation", name="Admin")
    assert nav.evaluate("n => n.scrollHeight > n.clientHeight")
    foot = account.bounding_box()
    box = nav.bounding_box()
    assert foot is not None and box is not None
    assert foot["y"] >= box["y"] + box["height"] - 1
    assert foot["y"] + foot["height"] <= 480 + 1
    account.click()
    sign_out = drawer.get_by_role("button", name="Sign out")
    sign_out.wait_for()
    assert sign_out.is_visible()
    page.keyboard.press("Escape")
    sign_out.wait_for(state="hidden")
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    page.context.close()


def test_set_a_quota_stays_put_as_the_quotas_arrive(open_as: Any) -> None:
    """The button that opens the dialog doesn't move when the list above it is answered.
    It did, a line down, and a press begun while the list loaded ended on the card instead:
    in Firefox and WebKit, Playwright's own click did, and no dialog opened."""
    page = open_as("compute_admin", "/admin/compute/quotas/")
    held: list[Any] = []

    def listing(url: str) -> bool:
        return urllib.parse.urlsplit(url).path.endswith("/api/v1/compute/quotas")

    def hold(route: Any) -> None:
        held.append(route)

    page.route(listing, hold)
    page.reload()
    page.get_by_text("Loading quotas…").wait_for()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held
    button = page.get_by_role("button", name="Set a quota")
    loading = button.bounding_box()
    for route in held:
        route.continue_()
    page.unroute(listing)
    page.get_by_text("No quotas:").wait_for()
    page.evaluate(NEXT_FRAMES)
    assert button.bounding_box() == loading
    button.click()
    page.get_by_role("dialog", name="Set a quota").wait_for()
    assert_clean(page)
    page.context.close()


def test_a_quota_goes_on_the_team_chosen_whatever_is_searched_next(
    open_as: Any, world: World
) -> None:
    """The chosen team stays listed, and chosen, in place, while another is searched for,
    so the quota saved is on the team the dialog shows ticked."""
    from iki_server.runs.models import RunQuota

    page = open_as("compute_admin", "/admin/compute/quotas/")
    page.get_by_role("button", name="Set a quota").click()
    dialog = page.get_by_role("dialog", name="Set a quota")
    dialog.get_by_role("radio", name="A team").check()
    chosen = dialog.get_by_role("radio", name="Structural Biology", exact=True)
    chosen.check()
    tier = dialog.get_by_role("combobox", name="Tier")
    where = (chosen.bounding_box(), tier.bounding_box())
    dialog.get_by_role("textbox", name="Find a team").fill("chem")
    dialog.get_by_role("radio", name="Chemistry", exact=True).wait_for()
    assert chosen.is_checked()
    # Nothing moved as the answer came: not the chosen team, nor what's below.
    assert (chosen.bounding_box(), tier.bounding_box()) == where
    assert_clean(page)
    dialog.get_by_role("textbox", name="Jobs running at once").fill("2")
    dialog.get_by_role("button", name="Set quota").click()
    page.get_by_text("Set a quota for Structural Biology.").wait_for()
    (quota,) = RunQuota.objects.all()
    assert (quota.team_id, quota.project_id, quota.max_running) == (world.team.pk, None, 2)
    page.context.close()


def test_notifications_arrive_live_and_open_where_they_point(open_as: Any, world: World) -> None:
    admin, viewer = world.users["admin"], world.users["viewer"]
    notify(
        [admin],
        kinds.TEAM_ADDED,
        title="Viewer added you to Structural Biology",
        path="/teams/structural-biology/",
        actor=viewer,
    )
    page = open_as("admin", "/notifications/")
    page.get_by_role("link", name="Inbox 1 unread").wait_for()
    # What the inbox's requests answered, to say why if the new one never shows.
    answered: list[str] = []
    page.on(
        "response",
        lambda r: answered.append(f"{r.status} {r.url}") if "/notifications" in r.url else None,
    )

    # Sent while the page is open: the stream (LISTEN/NOTIFY, then SSE) brings it.
    title = "Viewer added you to Kinase Atlas as Editor"
    notify(
        [admin],
        kinds.PROJECT_ADDED,
        title=title,
        path="/projects/kinase-atlas/",
        project=world.project,
        actor=viewer,
    )
    page.get_by_role("link", name="Inbox 2 unread").wait_for()
    try:
        page.get_by_role("link", name=title).wait_for()
    except PlaywrightTimeoutError:
        pytest.fail(f"the inbox never showed it; its requests answered {answered}")
    assert_clean(page)

    page.get_by_role("link", name=title).click()
    page.wait_for_url("**/projects/kinase-atlas/")
    page.get_by_role("link", name="Inbox 1 unread").wait_for()
    assert_clean(page)


def test_webhooks_show_their_deliveries_and_where_they_send(open_as: Any, world: World) -> None:
    webhooks.create(
        project=world.project,
        url="https://eln.example.com/hooks/iki?token=s3cret",
        description="Our ELN",
        chosen=["project.updated"],
        by=world.users["admin"],
    )
    page = open_as("admin", "/projects/kinase-atlas/automation/webhooks/")
    page.get_by_role("link", name="eln.example.com").click()
    page.get_by_role("button", name="Details of ping").click()
    page.get_by_text("Message ID").wait_for()
    assert_clean(page)
    page.context.close()

    # Org Admins see where it sends, not the address (which may carry a token).
    page = open_as("org_admin", "/admin/webhooks/")
    page.get_by_text("eln.example.com").wait_for()
    assert "s3cret" not in page.content()
    assert_clean(page)


def test_a_projects_page_says_what_it_holds_and_what_changed(open_as: Any, world: World) -> None:
    """A project's page (ADR 0176): where it is, who's in it, what changed lately, the New
    menu by keyboard, Automation's three, and a phone's width. By role, never by label
    (Firefox; see above)."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    made = entities.create(
        world.users["editor"],
        "acme/dna.DNASequence",
        world.project,
        {"bases": "ACGT", "name": "pUC19c"},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    width = "document.documentElement.scrollWidth - window.innerWidth <= 1"
    for scheme in ("light", "dark"):
        page: Page = open_as("admin", f"/projects/{world.project.slug}/", scheme)
        crumbs = page.get_by_role("navigation", name="Breadcrumb")
        crumbs.get_by_role("link", name="Projects", exact=True).wait_for()
        changes = page.get_by_role("list", name="Recent changes")
        changes.get_by_role("link", name=hrid).wait_for()
        changes.get_by_text("Editor created").wait_for()
        page.get_by_role("list", name="Members").get_by_role("listitem").first.wait_for()
        assert_clean(page)
        # New, from the keyboard: it opens, its first item has focus, Escape closes it.
        page.get_by_role("button", name=f"New in {world.project.name}").focus()
        page.keyboard.press("Enter")
        menu = page.get_by_role("menu", name=f"New in {world.project.name}")
        item = menu.get_by_role("menuitem", name="Entity", exact=True)
        item.wait_for()
        assert item.evaluate("e => e === document.activeElement")
        assert_clean(page)
        page.keyboard.press("Escape")
        menu.wait_for(state="detached")
        page.set_viewport_size({"width": 1440, "height": 900})
        assert page.evaluate(width)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate(width)
        page.context.close()

    # Its counts open All entities held to it, by its slug; its Entities tab has the same pills.
    page = open_as("admin", f"/projects/{world.project.slug}/")
    # Once its cards have loaded: until then they grow, and the link moves under the click.
    page.get_by_role("list", name="Recent changes").get_by_role("link", name=hrid).wait_for()
    page.get_by_role("list", name="Members").get_by_role("listitem").first.wait_for()
    page.get_by_role("link", name="Open in Entities, filtered to this project").click()
    page.wait_for_url(re.compile(rf"/entities/\?project={world.project.slug}$"))
    pills = page.get_by_role("group", name="Type")
    pills.get_by_role("button", name=re.compile(r"^DNA sequence \d+ records?$")).wait_for()
    assert_clean(page)
    page.goto(f"/projects/{world.project.slug}/entities/")
    pills.get_by_role("button", name=re.compile(r"^DNA sequence \d+ records?$")).click()
    page.wait_for_url(re.compile(r"\?type=acme%2Fdna\.DNASequence$|\?type=acme/dna\.DNASequence$"))
    assert (
        pills.get_by_role("button", name=re.compile("^DNA sequence")).get_attribute("aria-pressed")
        == "true"
    )
    assert page.get_by_role("combobox", name="Type").count() == 0
    assert_clean(page)
    page.context.close()

    page = open_as("admin", f"/projects/{world.project.slug}/")
    tabs = page.get_by_role("navigation", name="Project")
    tabs.get_by_role("link", name="Automation").click()
    page.wait_for_url(re.compile(r"/automation/workflows/$"))
    automation = page.get_by_role("navigation", name="Automation")
    automation.get_by_role("link", name="Triggers").click()
    page.wait_for_url(re.compile(r"/automation/triggers/$"))
    assert automation.get_by_role("link", name="Triggers").get_attribute("aria-current") == "page"
    assert tabs.get_by_role("link", name="Automation").get_attribute("aria-current") == "page"
    assert_clean(page)
    # Its own address opens the first of the three.
    page.goto(f"/projects/{world.project.slug}/automation/")
    page.wait_for_url(re.compile(r"/automation/workflows/$"))
    shown(page)


def test_a_projects_automation_and_tabs_fit_any_screen(
    open_as: Any, world: World, browser_name: str
) -> None:
    """Automation's toolbar is one row: the status filter as wide as its choices, and Run a
    workflow beside it, not under it. On a phone, the tab you're on is in view, as a
    record's is, though the tabs before it fill the width. By role (Firefox; see above)."""
    path = f"/projects/{world.project.slug}/automation/workflows/"
    for width in (1440, 390):
        page: Page = open_as("admin", path)
        page.set_viewport_size({"width": width, "height": 844})
        page.reload()  # Loaded at this width, as on a phone.
        status = page.get_by_role("combobox", name="Status")
        run = page.get_by_role("link", name="Run a workflow")
        run.wait_for()
        page.get_by_text("No workflow runs yet.").wait_for()
        select, button = status.bounding_box(), run.bounding_box()
        assert select and button
        assert select["width"] < 200, select
        assert abs(select["y"] + select["height"] / 2 - button["y"] - button["height"] / 2) < 2
        # WebKit draws a native select at its own height, whatever its min-height.
        if browser_name != "webkit":
            assert select["height"] == button["height"], (select, button)
        assert page.evaluate(NO_SIDEWAYS_SCROLL)
        assert_clean(page)
        page.context.close()

    # The last tab, on a phone: the ones before it are wider than the screen.
    page = open_as("admin", f"/projects/{world.project.slug}/members/")
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"/projects/{world.project.slug}/settings/")
    tabs = page.get_by_role("navigation", name="Project")
    settings = tabs.get_by_role("link", name="Settings")
    page.wait_for_function(
        "() => document.querySelector('nav[aria-label=Project] [aria-current=page]')"
        "?.textContent === 'Settings'"
    )
    page.wait_for_function(
        "() => { const n = document.querySelector('nav[aria-label=Project]');"
        " return n.scrollWidth > n.clientWidth && n.scrollLeft > 0; }"
    )
    box, current = tabs.bounding_box(), settings.bounding_box()
    assert box and current
    assert (
        box["x"] <= current["x"] and current["x"] + current["width"] <= box["x"] + box["width"] + 1
    )
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    page.context.close()


@pytest.mark.parametrize("press", ["across", "after"])
def test_a_projects_tabs_stay_put_as_their_counts_and_tabs_arrive(
    open_as: Any, world: World, press: str
) -> None:
    """A tab is where it's first drawn. The Entities count and the installed types (which
    say whether there's a Requests tab) are held back, then answered, while Runs is pressed
    on a phone: "across" presses it as soon as it can be pressed, and lets go after they
    arrive; "after" clicks, once they have, where Runs was first drawn. Either opens Runs.
    It didn't: drawn before them, Runs moved right as they arrived, so a press that began
    on it ended on Entities, and Firefox clicked their row instead, which opens nothing;
    and a click where it had been opened Entities or Requests."""
    from iki_server.entities import requests

    # A service: it has a Requests tab, which it knows of once the types are answered.
    requests.set_intake(
        world.project,
        types=["acme/protein.AssayRequest"],
        from_registry=True,
        sources=[],
        by=world.users["admin"],
    )
    page: Page = open_as("admin", f"/projects/{world.project.slug}/")
    page.set_viewport_size({"width": 390, "height": 844})
    held: list[Route] = []
    counts = re.compile(rf"/api/v1/entities/counts\?project={world.project.id}$")
    types = re.compile(r"/api/v1/entity-types(\?[^/]*)?$")
    page.route(counts, lambda route: held.append(route))
    page.route(types, lambda route: held.append(route))
    page.reload()
    deadline = time.monotonic() + 10
    while len(held) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held) == 2, [r.request.url for r in held]

    def answer_them() -> None:
        for route in held:
            route.continue_()
        page.unroute(counts)
        page.unroute(types)

    tabs = page.get_by_role("navigation", name="Project")
    runs = tabs.get_by_role("link", name="Runs", exact=True)
    page.evaluate(NEXT_FRAMES)
    # What's in the tabs' place while they're held: as tall as the tabs, where they'll be.
    holding = page.locator(".tabs.project-tabs:visible").bounding_box()
    # Where Runs can first be pressed: now, if the tabs can be pressed before they're known.
    early = runs.is_visible()
    if not early:
        answer_them()
    runs.wait_for()
    first = runs.bounding_box()
    assert first
    x, y = first["x"] + first["width"] / 2, first["y"] + first["height"] / 2

    def answered() -> None:
        tabs.get_by_role("link", name=re.compile(r"^Entities \d")).wait_for()
        tabs.get_by_role("link", name=re.compile(r"^Requests\b")).wait_for()
        page.evaluate(NEXT_FRAMES)

    if press == "across":
        page.mouse.move(x, y)
        page.mouse.down()
        if early:
            answer_them()
        answered()
        settled = runs.bounding_box()
        page.mouse.up()
    else:
        if early:
            answer_them()
        answered()
        settled = runs.bounding_box()
        page.mouse.click(x, y)
    try:
        page.wait_for_url(re.compile(r"/runs/$"), timeout=5000)
    except PlaywrightTimeoutError as error:
        raise AssertionError(
            f"Pressed Runs at {(x, y)}, drawn at {first}, then at {settled}. " + what_happened(page)
        ) from error
    assert settled == first
    assert holding == tabs.bounding_box()
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    page.context.close()


@pytest.mark.parametrize("press", ["across", "after"])
def test_a_records_tabs_stay_put_as_its_links_and_apps_arrive(
    open_as: Any, world: World, press: str
) -> None:
    """A record's tab is where it's first drawn, as a project's is. Its links, which its
    Lineage tab counts, and the apps about it, each a tab of its own, are held back, then
    answered, while Audit trail is pressed on a phone: "across" presses it as soon as it can
    be pressed, and lets go after they arrive; "after" clicks, once they have, where it was
    first drawn. Either opens its audit trail. It didn't: drawn before them, Audit trail
    moved 31 px right as Lineage's count arrived, so a press near its start that began on
    it ended on Lineage, and a click there opened Lineage.

    Then the tabs stay as they are while the audit trail opens. Its tab's link carries the
    record's project, so the record is read again under it, held back here: each tab drew
    its own strip, so the one pressed was replaced when the audit trail came."""
    from iki_server.entities import services as entities

    made = entities.create(
        world.users["admin"],
        "acme/protein.Protein",
        world.project,
        {"sequence": "MLEICLKLVG", "name": "ABL1"},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page: Page = open_as("admin", f"/entities/{hrid}/")
    page.set_viewport_size({"width": 390, "height": 844})
    held: list[Route] = []
    # By its HRID, or its ID if it's a registry entry's record.
    graph = re.compile(r"/api/v1/entities/[^/?]+/graph\?up=1&down=1$")
    apps = re.compile(r"/api/v1/entities/[^/?]+/apps(\?[^/]*)?$")
    page.route(graph, lambda route: held.append(route))
    page.route(apps, lambda route: held.append(route))
    page.reload()
    deadline = time.monotonic() + 10
    while len(held) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert sorted(r.request.url.rsplit("/", 1)[-1] for r in held) == [
        # The tabs' first page of apps (APP_TABS_SHOWN in web/app/src/apps.ts).
        "apps?limit=20",
        "graph?up=1&down=1",
    ], [r.request.url for r in held]

    def answer_them() -> None:
        for route in held:
            route.continue_()
        page.unroute(graph)
        page.unroute(apps)

    tabs = page.get_by_role("navigation", name="Record")
    trail = tabs.get_by_role("link", name=re.compile(r"^Audit trail\b"))
    page.evaluate(NEXT_FRAMES)
    # What's in the tabs' place while they're held: as tall as the tabs, where they'll be.
    holding = page.locator(".tabs.record-tabs:visible").bounding_box()
    # Where Audit trail can first be pressed: now, if the tabs can be pressed before
    # they're known.
    early = trail.is_visible()
    if not early:
        answer_them()
    trail.wait_for()
    first = trail.bounding_box()
    assert first
    # Near its start, as a thumb reaching from the left takes it: its count makes it wide
    # enough that its middle stays on it when it moves.
    x, y = first["x"] + 8, first["y"] + first["height"] / 2

    def answered() -> None:
        tabs.get_by_role("link", name=re.compile(r"^Lineage\W+\d")).wait_for()
        page.evaluate(NEXT_FRAMES)

    # The record, read again under its project once the audit trail is asked for.
    again = re.compile(rf"/api/v1/entities/{hrid}\?project=")
    record: list[Route] = []
    page.route(again, lambda route: record.append(route))
    if press == "across":
        page.mouse.move(x, y)
        page.mouse.down()
        if early:
            answer_them()
        answered()
        settled = trail.bounding_box()
        page.mouse.up()
    else:
        if early:
            answer_them()
        answered()
        settled = trail.bounding_box()
        page.mouse.click(x, y)
    try:
        page.wait_for_url(re.compile(rf"/entities/{hrid}/audit/"), timeout=5000)
    except PlaywrightTimeoutError as error:
        raise AssertionError(
            f"Pressed Audit trail at {(x, y)}, drawn at {first}, then at {settled}. "
            + what_happened(page)
        ) from error
    assert settled == first
    # The strip on screen while the record is read again is the one the audit trail has.
    deadline = time.monotonic() + 10
    while not record and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert record
    strip = tabs.element_handle()
    for route in record:
        route.continue_()
    page.unroute(again)
    page.get_by_role("group", name="Show").get_by_role("button", name="Timeline").wait_for()
    page.evaluate(NEXT_FRAMES)
    assert strip.evaluate("(nav) => nav.isConnected")
    assert holding == strip.bounding_box() == tabs.bounding_box()
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    page.context.close()


def test_entities_from_the_grid_to_their_audit_trail(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    made = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
    )
    entity = Entity.objects.get(pk=made.entity.pk)
    entities.update(admin, entity, {"name": "ABL1 kinase"}, expected_revision=1, reason="Full name")
    for path in ("/types/", "/types/acme/protein.Protein/"):
        page = open_as("admin", path)
        assert_clean(page)
        page.context.close()

    page = open_as("admin", "/projects/kinase-atlas/entities/?type=acme/protein.Protein")
    grid = page.get_by_role("table", name="1 protein")
    grid.get_by_role("cell", name="ABL1 kinase").wait_for()
    assert_clean(page)
    grid.get_by_role("link", name=entity.hrid).click()
    page.get_by_role("heading", level=1, name="ABL1 kinase").wait_for()
    # Who changed a value is in its history, on asking (ADR 0176), not under every value.
    assert page.get_by_text("Changed by").count() == 0
    page.get_by_role("button", name="History of Name").click()
    history = page.get_by_role("dialog", name="History of Name")
    history.get_by_text("Last change to Name").wait_for()
    history.get_by_text("“Full name”").wait_for()
    assert_clean(page)
    history.get_by_role("link", name="Name in the audit trail").click()
    page.wait_for_url(
        re.compile(rf"/entities/{entity.hrid}/audit/\?project=kinase-atlas&field=name$")
    )
    page.get_by_role("button", name="Show changes in revision 2").click()
    page.get_by_role("cell", name="ABL1", exact=True).wait_for()
    assert_clean(page)


#: pUC19c's 2,686 bases (GenBank L09137.2): a value far wider than any window.
PUC19 = "".join((Path(__file__).parent / "data" / "L09137.2.fasta").read_text().splitlines()[1:])


def test_a_long_value_never_widens_the_page(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    made = entities.create(
        world.users["admin"],
        "acme/dna.DNASequence",
        world.project,
        {"bases": PUC19, "name": "pUC19c"},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/?project=kinase-atlas")
    page.set_viewport_size({"width": 1440, "height": 900})
    values = page.get_by_role("region", name=re.compile("^Registry entry"))
    values.get_by_text("2,686 characters.").wait_for()
    # Within a pixel: a phone's width can round a layout's last fraction of one up.
    width = "document.documentElement.scrollWidth - window.innerWidth <= 1"
    assert page.evaluate(width)
    values.get_by_role("button", name="Show all of Bases").click()
    values.get_by_role("button", name="Show less of Bases").wait_for()
    # All of it, wrapped within its cell: the page scrolls down, never across.
    assert page.evaluate(width)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(width)
    # And at 320 pixels, the width WCAG's reflow criterion (1.4.10) asks a page to fit,
    # where the record's tabs no longer fit in a row and scroll within it instead.
    page.set_viewport_size({"width": 320, "height": 640})
    assert page.evaluate(width)
    assert_clean(page)


def test_a_fields_description_opens_beside_its_label(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    made = entities.create(
        world.users["admin"],
        "acme/dna.DNASequence",
        world.project,
        {"bases": PUC19[:60], "name": "pUC19c start", "topology": "circular"},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/?project=kinase-atlas")
    about = page.get_by_role("button", name="About Topology")
    described = page.get_by_text("Whether its ends are joined, as a plasmid's are.")
    assert about.get_attribute("aria-expanded") == "false"
    assert not described.is_visible()
    about.focus()
    page.keyboard.press("Enter")
    described.wait_for()
    assert about.get_attribute("aria-expanded") == "true"
    assert_clean(page)
    # Its target is at least 24 pixels square (WCAG 2.5.8).
    box = about.bounding_box()
    assert box is not None and box["width"] >= 24 and box["height"] >= 24
    page.keyboard.press("Enter")
    described.wait_for(state="hidden")


def test_the_grids_toolbar_is_one_row_above_one_row_of_types(open_as: Any) -> None:
    """ADR 0176: search, Project, Filter and sort on one row with Columns and CSV at its
    end; the type pills on one row beneath, scrolling across at a phone's width; and the
    newest changes first."""
    page = open_as("admin", "/entities/")
    search = page.get_by_role("searchbox", name="Search")
    search.wait_for()
    pills = page.get_by_role("group", name="Type")
    pills.get_by_role("button", name=re.compile(r"^All types")).wait_for()
    # A bordered 30 pixel field: no smaller than WCAG's target size (2.5.8).
    box = search.locator("xpath=..").bounding_box()
    assert box is not None and abs(box["height"] - 30) <= 1, box
    controls = [
        search.locator("xpath=.."),
        page.get_by_role("combobox", name="Project"),
        page.get_by_role("button", name="Filter", exact=True),
        page.get_by_role("button", name=re.compile(r"^Sort")),
        page.get_by_role("button", name="Columns"),
        page.get_by_role("link", name="Download CSV"),
    ]
    middles = []
    for control in controls:
        found = control.bounding_box()
        assert found is not None and found["height"] >= 24, (control, found)
        middles.append(found["y"] + found["height"] / 2)
    assert max(middles) - min(middles) <= 2, middles
    tops = {
        round(b["y"])
        for b in pills.get_by_role("button").evaluate_all(
            "all => all.map(b => b.getBoundingClientRect().toJSON())"
        )
    }
    assert len(tops) == 1, tops
    assert page.get_by_role("button", name=re.compile(r"^Sort")).inner_text().strip() == (
        "Changed, newest first"
    )
    assert_clean(page)

    page.set_viewport_size({"width": 390, "height": 844})
    tops = {
        round(b["y"])
        for b in pills.get_by_role("button").evaluate_all(
            "all => all.map(b => b.getBoundingClientRect().toJSON())"
        )
    }
    assert len(tops) == 1, tops
    assert page.evaluate("() => document.documentElement.scrollWidth - innerWidth <= 1")
    assert_clean(page)


def test_a_runs_id_is_never_cut_short(open_as: Any, world: World) -> None:
    """Runs (ADR 0176): an ID shows whole, at any width; what's left gives way."""
    from iki_server.entities import services as entities
    from iki_server.runs import catalog, services

    admin = world.users["admin"]
    action = catalog.action("acme/protein.compute_properties")
    assert action is not None
    made = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MKVL", "name": "ABL1"}
    )
    (run,) = services.launch_each(admin, action, world.project, "protein", [made.entity.pk], {}, {})
    page = open_as("admin", "/runs/")
    table = page.get_by_role("table")
    # The ID, and beneath it the batch it ran in.
    table.get_by_role("cell", name=re.compile(rf"^{run.hrid}\b.*in BAT-")).wait_for()
    for width in (1440, 390):
        page.set_viewport_size({"width": width, "height": 844})
        cells = page.locator("table td:first-child")
        assert cells.evaluate_all("all => all.every(c => c.scrollWidth <= c.clientWidth)")
        assert page.evaluate("() => document.documentElement.scrollWidth - innerWidth <= 1")
    assert_clean(page)


def test_searching_filtering_and_sorting_the_grid(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities

    admin = world.users["admin"]
    for name, sequence, organism, temperature in (
        ("ABL1", "MKVL", "Homo sapiens", 37.0),
        ("SRC", "MSTL", "Homo sapiens", 30.0),
        ("LacZ", "MTMI", "Escherichia coli", 25.0),
    ):
        entities.create(
            admin,
            "acme/protein.Protein",
            world.project,
            {
                "sequence": sequence,
                "name": name,
                "host": organism,
                "temperature_optimum": temperature,
            },
        )
    page = open_as("admin", "/projects/kinase-atlas/entities/?type=acme/protein.Protein")
    page.get_by_role("table", name="3 proteins").wait_for()

    page.get_by_role("searchbox", name="Search").fill("ab")
    page.get_by_role("table", name="1 of 3 proteins").wait_for()
    page.get_by_role("searchbox", name="Search").fill("")
    page.get_by_role("table", name="3 proteins").wait_for()

    editor = add_filter(page, "Host")
    editor.get_by_role("combobox", name="Filter 1: condition").select_option(label="is")
    editor.get_by_role("textbox", name="Filter 1: value").fill("Homo sapiens")
    editor.get_by_role("button", name="Done").click()
    editor = add_filter(page, "Temperature optimum")
    editor.get_by_role("combobox", name="Filter 2: condition").select_option(label="is more than")
    editor.get_by_role("textbox", name="Filter 2: value").fill("90")
    editor.get_by_role("combobox", name="Unit for Filter 2: value").select_option("[degF]")
    editor.get_by_role("button", name="Done").click()
    grid = page.get_by_role("table", name="1 of 3 proteins")
    grid.get_by_role("cell", name="ABL1", exact=True).wait_for()
    filters = page.get_by_role("group", name="Filters")
    filters.get_by_role("button", name="Filter 1: Host is Homo sapiens", exact=True).wait_for()
    assert_clean(page)

    # The address keeps it: a reload, or a shared link, shows the same.
    page.wait_for_url(re.compile(r"filters=.*temperature_optimum"))
    page.reload()
    page.get_by_role("table", name="1 of 3 proteins").wait_for()
    chip = filters.get_by_role("button", name=re.compile(r"^Filter 2: Temperature optimum"))
    chip.click()
    editor = page.get_by_role("dialog", name="Filter 2: Temperature optimum")
    assert editor.get_by_role("textbox", name="Filter 2: value").input_value() == "90"
    page.keyboard.press("Escape")
    filters.get_by_role("button", name=re.compile(r"^Remove filter 2:")).click()
    grid = page.get_by_role("table", name="2 of 3 proteins")
    grid.wait_for()

    grid.get_by_role("button", name="Name").click()
    page.wait_for_url(re.compile(r"sort=name"))
    grid.get_by_role("button", name="Name").click()
    page.wait_for_url(re.compile(r"desc=true"))
    first = grid.get_by_role("row").nth(1)
    first.get_by_role("cell", name="SRC", exact=True).wait_for()
    assert grid.get_by_role("columnheader", name="Name").get_attribute("aria-sort") == "descending"
    assert_clean(page)


#: The M13 forward (-20) and reverse sequencing primers, and the T7 promoter.
M13_FORWARD = "GTAAAACGACGGCCAGT"
M13_REVERSE = "CAGGAAACAGCTATGAC"
T7_PROMOTER = "TAATACGACTCACTATAGGG"


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_the_lists_across_projects_hold_only_what_you_can_read(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """All entities, a type's table, Requests and Runs (ADR 0176): every project the
    reader is in, and nothing of one they aren't, in a row or a count."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import ProjectLink

    editor = world.users["editor"]
    elsewhere = Project.objects.create(team=world.other_team, name="Vector Reference QC")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["mixed"], role=ProjectRole.EDITOR
    )
    dna = "acme/dna.DNASequence"
    forward = cast(
        "Entity",
        entities.create(
            editor, dna, world.project, {"bases": M13_FORWARD, "name": "M13 fwd"}
        ).entity,
    )
    entities.create(editor, dna, world.project, {"bases": M13_REVERSE, "name": "M13 rev"})
    hidden = entities.create(
        world.users["mixed"], dna, elsewhere, {"bases": T7_PROMOTER, "name": "T7 promoter"}
    )
    ProjectLink.objects.create(entity=forward, project=elsewhere)
    hidden_hrid = cast("Entity", hidden.entity).hrid

    page: Page = open_as("viewer", "/entities/", color_scheme)
    page.get_by_role("heading", level=1, name="All entities").wait_for()
    grid = page.get_by_role("table", name="2 entities")
    grid.wait_for()
    pills = page.get_by_role("group", name="Type")
    pills.get_by_role("button", name="DNA sequence 2 records").wait_for()
    pills.get_by_role("button", name="All types 2 records").wait_for()
    assert grid.get_by_role("link", name=hidden_hrid).count() == 0
    # The forward primer is in Vector Reference QC too, which the viewer isn't in.
    assert page.get_by_text("Vector Reference QC").count() == 0
    assert_clean(page)
    # Nor does a search for it by its ID find it.
    page.get_by_role("searchbox", name="Search").fill(hidden_hrid)
    page.get_by_text("No entities you can read match.").wait_for()
    page.get_by_role("searchbox", name="Search").fill("")
    grid.wait_for()

    # At a phone's width it scrolls down, never across.
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("() => document.documentElement.scrollWidth - innerWidth <= 1")
    page.set_viewport_size({"width": 1440, "height": 900})

    # A type's table: its fields as columns, a sequence found by a motif (IUPAC: N is any).
    pills.get_by_role("button", name="DNA sequence 2 records").click()
    page.wait_for_url(re.compile(r"type=acme"))
    page.goto(f"/entities/types/{dna}/")
    page.get_by_role("heading", level=1, name="DNA sequences").wait_for()
    table = page.get_by_role("table", name="2 DNA sequences")
    table.wait_for()
    editor_box = add_filter(page, "Bases")
    editor_box.get_by_role("textbox", name="Filter 1: value").fill("CAGNAAAC")
    editor_box.get_by_role("button", name="Done").click()
    table = page.get_by_role("table", name="1 of 2 DNA sequences")
    table.get_by_role("cell", name="M13 rev", exact=True).wait_for()
    views = page.get_by_role("navigation", name="Views")
    assert views.get_by_role("link", name="All DNA sequences").get_attribute("aria-current") is None
    assert_clean(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("() => document.documentElement.scrollWidth - innerWidth <= 1")
    page.set_viewport_size({"width": 1440, "height": 900})

    for path, heading in (("/requests/", "Requests"), ("/runs/", "Runs")):
        page.goto(path)
        page.get_by_role("heading", level=1, name=heading).wait_for()
        page.get_by_role("combobox", name="Project").wait_for()
        assert_clean(page)
    page.context.close()

    # Someone in both projects sees both, and the linked primer once, in each.
    page = open_as("mixed", "/entities/", color_scheme)
    page.get_by_role("group", name="Type").get_by_role(
        "button", name="DNA sequence 3 records"
    ).wait_for()
    grid = page.get_by_role("table", name="3 entities")
    row = grid.get_by_role("row").filter(has=page.get_by_role("link", name=forward.hrid))
    row.get_by_role(
        "cell", name=re.compile(r"^Kinase Atlas.*also in Vector Reference QC")
    ).wait_for()
    page.context.close()


def test_saving_and_sharing_a_search(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities

    for name, sequence in (("ABL1", "MKVL"), ("SRC", "MSTL")):
        entities.create(
            world.users["admin"],
            "acme/protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    page = open_as("editor", "/projects/kinase-atlas/entities/?type=acme/protein.Protein&q=abl")
    page.get_by_role("table", name="1 of 2 proteins").wait_for()
    page.get_by_role("button", name="Save this search").click()
    dialog = page.get_by_role("alertdialog", name="Save this search")
    dialog.get_by_label("Name").fill("ABL proteins")
    dialog.get_by_label(re.compile("Share with everyone in Kinase Atlas")).check()
    assert_clean(page)
    dialog.get_by_role("button", name="Save").click()
    page.get_by_text("Saved “ABL proteins”.").wait_for()
    saved = page.get_by_role("navigation", name="Saved searches")
    assert saved.get_by_role("link", name="ABL proteins").get_attribute("aria-current") == "page"
    assert_clean(page)

    other = open_as("viewer", "/projects/kinase-atlas/entities/")
    other.get_by_role("navigation", name="Saved searches").get_by_role(
        "link", name="ABL proteins"
    ).click()
    other.get_by_role("table", name="1 of 2 proteins").wait_for()
    # Viewers can't remove what someone else shared.
    assert other.get_by_role("button", name="Remove saved search ABL proteins").count() == 0
    assert_clean(other)


def test_saving_a_search_as_a_collection(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities

    for name, sequence in (("ABL1", "MKVL"), ("ABL2", "MKVI"), ("SRC", "MSTL")):
        entities.create(
            world.users["admin"],
            "acme/protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    page = open_as("editor", "/projects/kinase-atlas/entities/?type=acme/protein.Protein&q=abl")
    page.get_by_role("table", name="2 of 3 proteins").wait_for()
    page.get_by_role("button", name="Save as collection").click()
    dialog = page.get_by_role("alertdialog", name="Save as a collection")
    dialog.get_by_label("Name").fill("ABL kinases")
    assert_clean(page)
    dialog.get_by_role("button", name="Save collection").click()
    page.get_by_text(re.compile(r"Saved the collection COL-\d+\.")).wait_for()
    members = page.locator("section", has=page.get_by_role("heading", name="Members"))
    members.get_by_text("Everything this search finds").wait_for()
    assert_clean(page)

    # New matches join it, for everyone who can read it.
    entities.create(
        world.users["admin"],
        "acme/protein.Protein",
        world.project,
        {"sequence": "MKVA", "name": "ABL3"},
    )
    members.get_by_role("link", name="Show its members").click()
    page.get_by_role("table", name="3 proteins").wait_for()
    page.get_by_text(re.compile("Members of ABL kinases")).wait_for()
    assert_clean(page)


def test_freezing_a_collection(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities

    for name, sequence in (("ABL1", "MKVL"), ("ABL2", "MKVI")):
        entities.create(
            world.users["admin"],
            "acme/protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    from iki_server.entities.models import Entity

    made = Entity.objects.get(
        pk=entities.create(
            world.users["admin"],
            "iki/core.Collection",
            world.project,
            {"name": "ABL kinases", "search": {"type": "acme/protein.Protein", "q": "abl"}},
        ).entity.pk
    )
    page = open_as("editor", f"/entities/{made.hrid}/")
    page.get_by_role("button", name="Freeze as a fixed list").click()
    dialog = page.get_by_role("alertdialog", name="Freeze ABL kinases?")
    dialog.get_by_label(re.compile("Reason")).fill("For the paper")
    assert_clean(page)
    dialog.get_by_role("button", name="Freeze").click()
    page.get_by_text(f"Froze {made.hrid}: it lists 2 entities.").wait_for()
    page.get_by_text("It lists 2 entities. You see those you can read.").wait_for()
    assert page.get_by_role("button", name="Freeze as a fixed list").count() == 0
    assert_clean(page)


def test_creating_editing_and_archiving_an_entity(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    gene = Entity.objects.get(
        pk=entities.create(
            world.users["admin"],
            "acme/dna.DNASequence",
            world.project,
            {"bases": "ATG", "name": "abl1"},
        ).entity.pk
    )
    page = open_as("admin", "/projects/kinase-atlas/entities/?type=acme/protein.Protein")
    page.get_by_role("link", name="New protein").click()
    page.get_by_role("heading", level=2, name="New protein").wait_for()
    assert_clean(page)

    page.get_by_role("button", name="Create protein").click()
    sequence = page.get_by_role("textbox", name="Sequence")
    assert sequence.get_attribute("aria-invalid") == "true"
    assert page.evaluate("document.activeElement.id") == sequence.get_attribute("id")
    assert_clean(page)

    sequence.fill("MKVL")
    page.get_by_role("textbox", name="Name (optional)").fill("Kinase X")
    page.get_by_role("textbox", name="Temperature optimum (optional)").fill("98.6")
    page.get_by_role("combobox", name="Unit for Temperature optimum").select_option("[degF]")
    picker = page.get_by_role("combobox", name="Encoded by (optional)")
    # The search for "abl" is held. Until it's answered the picker says it's searching and
    # lists nothing: not what the empty box found (abl1 among it), which the answer would
    # move under the pointer, so a click on one record chose another, or nothing.
    held: list[Any] = []

    def searching_abl(url: str) -> bool:
        return "/api/v1/entities?" in url and "q=abl" in url

    def hold(route: Any) -> None:
        held.append(route)

    page.route(searching_abl, hold)
    picker.fill("abl")
    page.get_by_text("Searching…").wait_for()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held and page.get_by_role("option").count() == 0
    for route in held:
        route.continue_()
    page.unroute(searching_abl)
    page.get_by_role("option", name=re.compile("abl1")).click()
    assert picker.input_value() == f"abl1 ({gene.hrid})"
    page.get_by_role("button", name="Add Uniprot ids").click()
    page.get_by_role("textbox", name="Uniprot ids, item 1").fill("P00519")
    assert_clean(page)
    page.get_by_role("button", name="Create protein").click()
    # A registry entry's ID names its record in the project it was made in (ADR 0115).
    page.wait_for_url(re.compile(r"/entities/PRT-\d+/\?project=kinase-atlas$"))
    made = Entity.objects.get(type="acme/protein.Protein", owner_project=world.project)
    page.get_by_text(
        f"Registered {made.hrid}, a new entry in the Main registry. This is Kinase Atlas’s "
        "record of it."
    ).wait_for()
    values = entities.current_values_of([made])[made.pk]
    assert values["temperature_optimum"] == pytest.approx(37.0)
    assert values["encoded_by"] == gene.entry_id and values["uniprot_ids"] == ["P00519"]
    assert_clean(page)

    page.get_by_role("link", name="Edit").click()
    page.get_by_role("heading", level=1, name="Edit Kinase X").wait_for()
    assert page.get_by_role("textbox", name="Sequence").is_disabled()
    page.get_by_role("textbox", name="Name (optional)").fill("Kinase Y")
    page.get_by_role("textbox", name="Reason (optional)").fill("Renamed")
    assert_clean(page)
    page.get_by_role("button", name="Save changes").click()
    page.get_by_text(f"Saved revision 2 of {made.hrid}.").wait_for()

    page.get_by_role("button", name=f"More actions for {made.hrid}").click()
    page.get_by_role("menuitem", name="Archive…").click()
    dialog = page.get_by_role("alertdialog", name=f"Archive {made.hrid}?")
    dialog.get_by_role("textbox", name="Reason (optional)").fill("Duplicate of another")
    assert_clean(page)
    dialog.get_by_role("button", name="Archive").click()
    page.get_by_text(f"Archived {made.hrid}.").wait_for()
    assert Entity.objects.get(pk=made.pk).archived_at is not None


def test_leaving_an_edit_with_changes_unsaved_asks_first(open_as: Any, world: World) -> None:
    """A link in the sidebar, or Back, from an edit with a change typed and not saved asks
    first: staying keeps what was typed, going leaves the record as it was. Saving goes to
    the record without asking."""
    from iki_server.entities import services as entities

    made = Entity.objects.get(
        pk=entities.create(
            world.users["editor"],
            "acme/protein.Protein",
            world.project,
            {"sequence": "GPSGVSPNYDKWEMERTDITMKHKLGGGQYGEVYEGVWKK", "name": "ABL1 kinase domain"},
        ).entity.pk
    )
    page = open_as("editor", f"/entities/{made.hrid}/?project=kinase-atlas")
    page.get_by_role("link", name="Edit").click()
    page.get_by_role("heading", level=1, name="Edit ABL1 kinase domain").wait_for()
    name = page.get_by_role("textbox", name="Name (optional)")
    name.fill("ABL1 KD, construct 2")
    nav = page.get_by_role("navigation", name="Main")
    nav.get_by_role("link", name="My work").click()
    dialog = page.get_by_role("alertdialog", name="Discard your changes?")
    expect(dialog).to_contain_text(
        f"Name has changes that aren’t saved. Discarding them leaves {made.hrid} as it is."
    )
    assert_clean(page)
    dialog.get_by_role("button", name="Keep editing").click()
    expect(dialog).to_have_count(0)
    expect(page).to_have_url(re.compile(r"/edit/"))
    expect(name).to_have_value("ABL1 KD, construct 2")
    # Back, to the record's page, asks too.
    page.go_back()
    dialog.wait_for()
    dialog.get_by_role("button", name="Keep editing").click()
    expect(dialog).to_have_count(0)
    expect(page).to_have_url(re.compile(r"/edit/"))
    expect(name).to_have_value("ABL1 KD, construct 2")

    # Saving goes to the record.
    page.get_by_role("button", name="Save changes").click()
    page.get_by_text(f"Saved revision 2 of {made.hrid}.").wait_for()
    expect(dialog).to_have_count(0)
    # Changed again, and left: nothing more is saved.
    page.get_by_role("link", name="Edit").click()
    name.fill("Something else")
    nav.get_by_role("link", name="My work").click()
    dialog.get_by_role("button", name="Discard changes").click()
    page.wait_for_url(lambda url: "/entities/" not in url)
    values = entities.current_values_of([made])[made.pk]
    assert values["name"] == "ABL1 KD, construct 2"


def test_a_picker_takes_a_press_as_the_page_scrolls(open_as: Any, world: World) -> None:
    """A press on a picker's option while the page scrolls (a trackpad still coasting, or
    the browser bringing the option into view) chooses it. The list is in the field and
    moves with it: scrolling neither closes it nor swaps its rows, as a popover that closed
    on scroll and opened again at once did, so the press landed on a row that was gone."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    gene = Entity.objects.get(
        pk=entities.create(
            world.users["admin"],
            "acme/dna.DNASequence",
            world.project,
            {"bases": "ATG", "name": "abl1"},
        ).entity.pk
    )
    page = open_as("admin", "/projects/kinase-atlas/entities/new/?type=acme/protein.Protein")
    picker = page.get_by_role("combobox", name="Encoded by (optional)")
    picker.scroll_into_view_if_needed()
    picker.fill("abl")
    option = page.get_by_role("option", name=re.compile("abl1"))
    row = option.element_handle()
    box = row.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.evaluate("window.scrollBy(0, 1)")
    page.evaluate(NEXT_FRAMES)
    assert page.evaluate("window.scrollY") > 0
    assert row.evaluate("(row) => row.isConnected"), "scrolling replaced the list"
    page.mouse.up()
    assert picker.input_value() == f"abl1 ({gene.hrid})"
    assert_clean(page)


def test_escape_closes_a_pickers_list_then_puts_back_what_was_chosen(
    open_as: Any, world: World
) -> None:
    """Escape closes the list and keeps what's typed, and the list stays closed; Escape
    again, or leaving the box, puts back what's chosen, or clears the box if nothing is
    (WAI-ARIA's combo box pattern). Each step is checked as soon as it's done: a list that
    opened again at once would be caught."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    gene = Entity.objects.get(
        pk=entities.create(
            world.users["admin"],
            "acme/dna.DNASequence",
            world.project,
            {"bases": "ATG", "name": "abl1"},
        ).entity.pk
    )
    page = open_as("admin", "/projects/kinase-atlas/entities/new/?type=acme/protein.Protein")
    picker = page.get_by_role("combobox", name="Encoded by (optional)")
    picker.fill("abl")
    page.get_by_role("option", name=re.compile("abl1")).wait_for()
    picker.press("Escape")
    page.evaluate(NEXT_FRAMES)
    assert picker.get_attribute("aria-expanded") == "false"
    assert page.get_by_role("listbox").count() == 0
    assert picker.input_value() == "abl"
    picker.press("Escape")
    page.evaluate(NEXT_FRAMES)
    assert picker.input_value() == ""
    assert picker.get_attribute("aria-expanded") == "false"

    # Typed and left (by Tab): nothing chosen, so nothing's left in the box.
    picker.fill("abl")
    page.get_by_role("option", name=re.compile("abl1")).wait_for()
    picker.press("Tab")
    page.evaluate(NEXT_FRAMES)
    assert picker.input_value() == ""
    assert picker.get_attribute("aria-expanded") == "false"

    # Chosen, typed over and left: what's chosen comes back.
    picker.fill("abl")
    page.get_by_role("option", name=re.compile("abl1")).click()
    assert picker.input_value() == f"abl1 ({gene.hrid})"
    picker.fill("xyz")
    # The list opens again, the chosen record in it.
    page.get_by_role("listbox").wait_for()
    picker.press("Escape")
    page.evaluate(NEXT_FRAMES)
    assert picker.input_value() == "xyz"
    assert picker.get_attribute("aria-expanded") == "false"
    picker.press("Tab")
    page.evaluate(NEXT_FRAMES)
    assert picker.input_value() == f"abl1 ({gene.hrid})"
    assert_clean(page)


@pytest.fixture
def browser_name(browser: Browser) -> str:
    return browser.browser_type.name


@pytest.fixture
def tasks(server: Server, request: pytest.FixtureRequest) -> Iterator[Server]:
    """A task runner beside the server, for work done after the request (checking uploads)."""
    with run_tasks(server.env) as runner:
        yield runner
        call = getattr(request.node, "rep_call", None)
        if call is not None and call.failed:
            request.node.add_report_section("teardown", "the task runner", went_wrong(runner, 0))


def test_uploading_a_file_into_a_file_field(open_as: Any, world: World, tasks: Server) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.files.models import File

    protein: Any = entities.create(
        world.users["admin"],
        "acme/protein.Protein",
        world.project,
        {"sequence": "MKVL", "name": "abl1"},
    ).entity
    page = open_as(
        "admin", "/projects/kinase-atlas/entities/new/?type=acme/protein.ProteinStructure"
    )
    page.get_by_role("heading", level=2, name="New protein structure").wait_for()
    structure = page.get_by_role("group", name="Structure")
    # Big enough to go in several parts, and typeless, as browsers give .pdb files.
    data = b"ATOM      1  N   ALA A   1\n" * 400_000
    structure.get_by_label("Upload a file").set_input_files(
        {"name": "1abc.pdb", "mimeType": "", "buffer": data}
    )
    # Once Iki has checked its bytes, it's the field's file.
    structure.get_by_role("button", name="Remove 1abc.pdb").wait_for(timeout=60_000)
    stored = File.objects.get(name="1abc.pdb")
    assert (stored.name, stored.media_type, stored.size) == (
        "1abc.pdb",
        "chemical/x-pdb",
        len(data),
    )
    assert_clean(page)

    page.get_by_role("button", name="Add Chains").click()
    page.get_by_role("combobox", name="Chains, item 1").fill("abl")
    page.get_by_role("option", name=re.compile("abl1")).click()
    page.get_by_role("button", name="Create protein structure").click()
    page.wait_for_url(re.compile(r"/entities/PST-\d+/$"))
    made = Entity.objects.get(type="acme/protein.ProteinStructure")
    values = entities.current_values_of([made])[made.pk]
    assert values["structure"] == stored.pk and values["chains"] == [protein.entry_id]
    page.get_by_role("link", name="1abc.pdb").wait_for()
    assert_clean(page)


def test_exporting_a_collection_and_downloading_its_bundle(
    open_as: Any, world: World, tasks: Server, tmp_path: Path
) -> None:
    from iki_server.bundles import keys, reading
    from iki_server.entities import services as entities

    protein: Any = entities.create(
        world.users["admin"],
        "acme/protein.Protein",
        world.project,
        {"sequence": "MKVL", "name": "ABL1"},
    ).entity
    kinases: Any = entities.create(
        world.users["admin"],
        "iki/core.Collection",
        world.project,
        {"name": "Validated kinases", "members": [str(protein.pk)]},
    ).entity
    page = open_as("admin", "/projects/kinase-atlas/settings/")
    exporting = page.locator("section", has=page.get_by_role("heading", name="Export", exact=True))
    exporting.get_by_role("combobox", name="Collection").select_option(kinases.hrid)
    exporting.get_by_role("textbox", name="Why").fill("For production")
    assert_clean(page)
    exporting.get_by_role("button", name="Export").click()
    page.get_by_text(f"Exporting {kinases.hrid} (Validated kinases), with its members.").wait_for()
    link = exporting.get_by_role("link", name=re.compile(r"^Download "))
    link.wait_for(timeout=60_000)
    exporting.get_by_text("2 entities, 2 revisions, 0 files").wait_for()
    assert_clean(page)
    with page.expect_download() as downloading:
        link.click()
    saved = tmp_path / downloading.value.suggested_filename
    downloading.value.save_as(saved)
    assert saved.name.startswith("kinase-atlas-") and saved.name.endswith(".iki-bundle.tar.gz")
    public = keys.public_text(keys.private_key(settings.IKI_BUNDLE_SIGNING_KEY))
    checked = reading.read(saved, {}, limit=1024**3)
    source = checked.manifest["source"]["deployment"]
    bundle = reading.read(saved, {source: (public,)}, limit=1024**3)
    assert bundle.trusted_as == source
    assert {e["hrid"] for e in bundle.lines("entities.ndjson")} == {kinases.hrid, protein.hrid}


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_reader_exports_the_proteins_picked_as_one_fasta_file(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    """A Viewer picks rows to export (ADR 0229), though they run and edit nothing, and the
    file is the records picked, in the table's order, each at its revision."""
    from Bio import SeqIO

    from iki_server.audit.models import AuditEvent
    from iki_server.entities import services as entities

    made: list[Any] = [
        entities.create(
            world.users["editor"],
            "acme/protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        ).entity
        for name, sequence in (("HBA1", "MVLSPADKTNVKAAWGKVGAHAG"), ("HBB", "MVHLTPEEKSAVTALWGKV"))
    ]
    page: Page = open_as(
        "viewer", "/projects/kinase-atlas/entities/?type=acme/protein.Protein", color_scheme
    )
    page.get_by_role("checkbox", name=f"Select {made[0].hrid}").check()
    page.get_by_role("checkbox", name=f"Select {made[1].hrid}").check()
    page.get_by_text("2 selected", exact=True).wait_for()
    page.get_by_role("button", name="Export selected").click()
    dialog = page.get_by_role("dialog", name="Export 2 selected")
    dialog.get_by_role("radio", name=re.compile("^FASTA")).wait_for()
    assert dialog.get_by_role("radio", name=re.compile("^FASTA")).is_checked()
    assert_clean(page)
    with page.expect_download() as downloading:
        dialog.get_by_role("button", name="Export").click()
    saved = tmp_path / downloading.value.suggested_filename
    downloading.value.save_as(saved)
    # WebKit saves it with underscores for spaces.
    assert saved.name.replace("_", " ") == "Kinase Atlas Proteins (2).fasta"
    with saved.open() as text:
        shown = [r.id for r in SeqIO.parse(text, "fasta")]  # type: ignore[no-untyped-call]
    # In the table's order: last changed first, as it opens.
    newest_first = [e.hrid for e in reversed(made)]
    assert shown == newest_first
    dialog.get_by_role("status").get_by_text("2 records", exact=False).wait_for()
    assert_clean(page)
    taken = AuditEvent.objects.get(action="entities.exported")
    assert [r["hrid"] for r in taken.data["records"]] == newest_first
    assert taken.data["writer"]["builtin"] == "protein-fasta"
    dialog.get_by_role("button", name="Done").click()
    page.get_by_role("button", name="Export selected").wait_for()
    assert page.get_by_role("dialog").count() == 0


def test_a_whole_table_is_written_in_the_background_and_downloaded_when_ready(
    open_as: Any, world: World, tasks: Server, tmp_path: Path
) -> None:
    """Past a hundred records, the file is written by a task: the dialog shows how far it's
    got, then a link to the file."""
    from Bio import SeqIO

    from iki_server.entities import services as entities

    residues = "ACDEFGHIKLMNPQRSTVWY"
    for n in range(101):
        entities.create(
            world.users["editor"],
            "acme/protein.Protein",
            world.project,
            {"sequence": residues[n % 20] + residues + residues[: n % 17], "name": f"Variant {n}"},
        )
    page: Page = open_as("viewer", "/projects/kinase-atlas/entities/?type=acme/protein.Protein")
    page.get_by_role("button", name="Export", exact=True).click()
    dialog = page.get_by_role("dialog", name="Export all proteins the table shows")
    dialog.get_by_role("button", name="Export", exact=True).click()
    link = dialog.get_by_role("link", name="Kinase Atlas Proteins (101).fasta")
    link.wait_for(timeout=60_000)
    dialog.get_by_role("status").get_by_text("101 records", exact=False).wait_for()
    assert_clean(page)
    with page.expect_download() as downloading:
        link.click()
    saved = tmp_path / downloading.value.suggested_filename
    downloading.value.save_as(saved)
    with saved.open() as text:
        assert len(list(SeqIO.parse(text, "fasta"))) == 101  # type: ignore[no-untyped-call]


def test_importing_a_bundle_and_reading_its_plan(
    open_as: Any, world: World, tasks: Server, tmp_path: Path
) -> None:
    from django.db import transaction

    from iki_server.bundles import export
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.org.models import OrgRole, OrgRoleAssignment

    admin = world.users["admin"]
    OrgRoleAssignment.objects.create(user=admin, role=OrgRole.ORG_ADMIN)
    out = tmp_path / "staging.iki-bundle.tar.gz"

    class UndoError(Exception):
        pass

    # A bundle of a protein this deployment doesn't have: made, exported, then undone. Its
    # HRID is ahead of this deployment's, so keeping it moves the sequence on, which the
    # app's role can only do through iki_hrid_advance.
    with contextlib.suppress(UndoError), transaction.atomic():
        made: Any = entities.create(
            admin, "acme/protein.Protein", world.project, {"sequence": "MKWWYYHH", "name": "ABL1"}
        ).entity
        # Its registry entry's HRID, which its record has too (ADR 0115).
        Entity.objects.filter(pk__in=[made.pk, made.entry_id]).update(hrid="PRT-000900")
        made.refresh_from_db()
        export.write(
            world.project, [made], out, by=admin, reason="For production", selection="ABL1"
        )
        raise UndoError
    page = open_as("admin", "/projects/kinase-atlas/settings/")
    importing = page.locator("section", has=page.get_by_role("heading", name="Import", exact=True))
    importing.locator("input[type=file]").set_input_files(str(out))
    page.wait_for_url(re.compile(r"/imports/[0-9a-f-]+/$"), timeout=60_000)
    page.get_by_text("Nothing in it conflicts with what’s here").wait_for(timeout=60_000)
    page.get_by_text("1 new entity").wait_for()
    page.get_by_role("cell", name=re.compile("ABL1")).wait_for()
    assert_clean(page)

    # Applying it asks why, and to confirm: then it's here, and its page says where from.
    page.get_by_role("textbox", name="Why").fill("Moving the kinase work")
    page.get_by_role("button", name="Import", exact=True).click()
    dialog = page.get_by_role("alertdialog", name="Import into Kinase Atlas?")
    dialog.get_by_text("1 new entity and 0 entities with newer revisions").wait_for()
    assert_clean(page)
    dialog.get_by_role("button", name="Import", exact=True).click()
    page.get_by_role("heading", name="Imported", exact=True).wait_for(timeout=60_000)
    page.get_by_text("Moving the kinase work").wait_for()
    assert_clean(page)
    page.get_by_role("link", name=re.compile(r"^PRT-\d+$")).click()
    page.get_by_role("link", name="the import").wait_for()
    page.get_by_text(re.compile("^test’s Kinase Atlas, where it’s PRT-")).wait_for()
    assert_clean(page)
    after: Any = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MKAFTER"}
    ).entity
    assert int(after.hrid.removeprefix("PRT-")) > 900  # sequences outlive each test


def test_starting_a_run_and_following_it_live(open_as: Any, world: World) -> None:
    from test_workers import LOCAL, ROOMY

    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.runs import queue, workers
    from iki_server.runs.models import ActionRun

    made = entities.create(
        world.users["admin"],
        "acme/protein.Protein",
        world.project,
        {"sequence": "MKV", "name": "ABL1"},
    )
    protein = Entity.objects.get(pk=made.entity.pk)
    page = open_as("admin", f"/entities/{protein.hrid}/")
    page.get_by_role("link", name="Run action").click()
    page.get_by_role("heading", level=2, name="Run an action").wait_for()
    page.get_by_role("combobox", name="Action").select_option(label="Compute properties")
    page.get_by_text("It runs on one of Iki’s backend workers").wait_for()
    assert_clean(page)
    page.get_by_role("textbox", name="Reason (optional)").fill("Fill in weights")
    page.get_by_role("button", name="Start run").click()
    page.wait_for_url(re.compile(r"/runs/RUN-\d+/$"))
    run = ActionRun.objects.get()
    page.get_by_text(f"Started {run.hrid}.").wait_for()
    page.get_by_text("Not picked up yet").wait_for()
    assert_clean(page)

    # A worker takes it, prints and reports progress: the page follows, live.
    registration, _ = workers.create_registration(
        label="Pods", tier="backend", by=world.users["admin"]
    )
    pod, _ = workers.register(
        registration, name="pod-1", version="0.1.0", capacity=ROOMY, plugins=LOCAL
    )
    leased = queue.lease(pod)
    assert leased is not None
    job = leased[0]
    assert job.lease_token is not None
    page.get_by_text("On pod-1").wait_for()
    queue.append_log(pod, job.pk, job.lease_token, 0, "Weighing ABL1\n")
    log = page.get_by_role("log", name="Log of attempt 1")
    log.get_by_text("Weighing ABL1").wait_for()
    queue.progress(pod, job.pk, job.lease_token, 0.5, "Halfway")
    page.get_by_text("50% · Halfway").wait_for()
    assert_clean(page)
    queue.fail(pod, job.pk, job.lease_token, "failed", "ValueError: no weights")
    page.get_by_role("alert").get_by_text("ValueError: no weights").wait_for()
    assert page.get_by_role("button", name="Cancel run").count() == 0
    assert_clean(page)

    page.get_by_role("main").get_by_role("link", name="Kinase Atlas").click()
    # The project's runs, one row each (ADR 0176's table, as every run's list).
    row = page.get_by_role("row").filter(has_text=run.hrid)
    row.get_by_text("Failed", exact=True).wait_for()
    assert_clean(page)


def test_picking_rows_and_running_an_action_on_each(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities
    from iki_server.runs.models import ActionRun

    for name, sequence in [("ABL1", "MKV"), ("SRC", "MGS")]:
        entities.create(
            world.users["admin"],
            "acme/protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    page = open_as("admin", "/projects/kinase-atlas/entities/?type=acme/protein.Protein")
    page.get_by_role("checkbox", name="Select all 2 shown").check()
    page.get_by_text("2 selected").wait_for()
    assert_clean(page)
    page.get_by_role("link", name="Run action").click()
    page.get_by_role("heading", level=2, name="Run an action").wait_for()
    page.get_by_role("combobox", name="Action").select_option(label="Compute properties")
    page.get_by_text("One run for each of these 2").wait_for()
    assert_clean(page)
    page.get_by_role("button", name="Start 2 runs").click()
    # The batch's page follows them.
    page.get_by_text(re.compile(r"^Started 2 runs of Compute properties, as BAT-")).wait_for()
    page.get_by_role("region", name="2 runs, one for each Protein").wait_for()
    runs = page.get_by_role("link", name=re.compile(r"^On PRT-"))
    runs.first.wait_for()
    assert runs.count() == 2
    assert ActionRun.objects.count() == 2
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_approving_a_run_an_ai_agent_asked_for(
    open_as: Any, world: World, color_scheme: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iki_server.audit import log as audit
    from iki_server.audit.models import Via
    from iki_server.entities import services as entities
    from iki_server.runs import approvals, catalog, services
    from iki_server.runs.models import ActionRun, RunStatus

    admin = world.users["admin"]
    action = catalog.action("acme/protein.compute_properties")
    assert action is not None
    made = [
        entities.create(admin, "acme/protein.Protein", world.project, {"sequence": s, "name": n})
        for n, s in [("ABL1", "MKV"), ("SRC", "MGS")]
    ]
    # The test deployment has backend workers only: hold that tier, as Iki holds the
    # container and VM tiers, for an agent acting for the admin.
    monkeypatch.setattr(approvals, "APPROVAL_TIERS", approvals.APPROVAL_TIERS | {"backend"})
    with audit.using(audit.Context(via=Via.MCP, oauth_client="Claude")):
        first, second = services.launch_each(
            admin, action, world.project, "protein", [m.entity.pk for m in made], {}, {}
        )

    page = open_as("admin", f"/runs/{first.hrid}/", color_scheme)
    heading = page.get_by_role("heading", name="Waiting for your approval")
    request = page.locator("section", has=heading)
    request.get_by_text("Claude asked to start these 2 runs").wait_for()
    assert request.get_by_role("link", name=second.hrid).is_visible()
    assert_clean(page)

    request.get_by_role("button", name="Decline").click()
    dialog = page.get_by_role("alertdialog", name="Decline 2 runs?")
    dialog.get_by_text("They end without running").wait_for()
    assert_clean(page)
    dialog.get_by_role("button", name="Keep waiting").click()

    request.get_by_role("button", name="Approve 2 runs").click()
    page.get_by_text("Approved 2 runs.").wait_for()
    page.get_by_text("Not picked up yet").wait_for()
    assert heading.count() == 0
    page.get_by_text(re.compile("^Approved by ")).wait_for()
    assert set(ActionRun.objects.values_list("status", flat=True)) == {RunStatus.QUEUED}
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_declining_a_run_an_ai_agent_asked_for_says_why(
    open_as: Any, world: World, color_scheme: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decline is kept with its reason, which the agent that asked reads: the dialog
    sends none until one is given, and a blank or only spaces isn't one."""
    from iki_server.audit import log as audit
    from iki_server.audit.models import Via
    from iki_server.entities import services as entities
    from iki_server.runs import approvals, catalog, services
    from iki_server.runs.models import ActionRun, RunApproval, RunStatus

    admin = world.users["admin"]
    action = catalog.action("acme/protein.compute_properties")
    assert action is not None
    made = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MKV", "name": "ABL1"}
    )
    monkeypatch.setattr(approvals, "APPROVAL_TIERS", approvals.APPROVAL_TIERS | {"backend"})
    with audit.using(audit.Context(via=Via.MCP, oauth_client="Claude")):
        (run,) = services.launch_each(
            admin, action, world.project, "protein", [made.entity.pk], {}, {}
        )

    page = open_as("admin", f"/runs/{run.hrid}/", color_scheme)
    request = page.locator(
        "section", has=page.get_by_role("heading", name="Waiting for your approval")
    )
    request.get_by_role("button", name="Decline").click()
    dialog = page.get_by_role("alertdialog", name=f"Decline {run.hrid}?")
    reason = dialog.get_by_role("textbox", name="Reason")
    for blank in ("", "   "):
        reason.fill(blank)
        dialog.get_by_role("button", name="Decline").click()
        expect(reason).to_have_attribute("aria-invalid", "true")
        expect(dialog.get_by_text("Say why it’s declined.")).to_be_visible()
        expect(reason).to_be_focused()
    assert RunApproval.objects.get().state == "pending"
    assert_clean(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth <= 1")
    assert_clean(page)
    page.set_viewport_size({"width": 1440, "height": 900})

    reason.fill("  Not on the shared tier this week ")
    expect(reason).not_to_have_attribute("aria-invalid", "true")
    with page.expect_response(lambda r: r.url.endswith("/decline")) as declined:
        dialog.get_by_role("button", name="Decline").click()
    assert declined.value.status == 200, declined.value.text()
    assert declined.value.request.post_data_json == {"note": "Not on the shared tier this week"}
    page.get_by_text(f"Declined {run.hrid}.").wait_for()
    page.get_by_text(
        re.compile(r"^Declined by .+: Not on the shared tier this week$")
    ).first.wait_for()
    decided = RunApproval.objects.get()
    assert (decided.state, decided.note) == ("declined", "Not on the shared tier this week")
    assert ActionRun.objects.get(pk=run.pk).status == RunStatus.DECLINED
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_an_org_admin_releases_a_legal_hold_saying_why(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """Releasing a hold opens its row asking why, as the service requires: a blank reason
    isn't sent, one of only spaces is refused with the reason kept to fix, and the reason
    given is kept with the hold and in its audit event (ADR 0092)."""
    from iki_server.audit.models import AuditEvent
    from iki_server.org import holds
    from iki_server.org.models import LegalHold

    hold = holds.place(world.users["org_admin"], world.project, "Case 24-117")
    page = open_as("org_admin", "/admin/legal-holds/", color_scheme)
    held = page.get_by_role("list").filter(has_text="Case 24-117").get_by_role("listitem")
    assert_clean(page)
    held.get_by_role(
        "link", name=f"Release the hold on {world.project.name} for Case 24-117…"
    ).click()
    form = page.get_by_role(
        "form", name=f"Release the hold on {world.project.name} for Case 24-117"
    )
    reason = form.get_by_role("textbox", name="Reason for releasing it")

    def checked(page: Page) -> None:
        assert_clean(page)

    expect(reason).to_be_focused()
    release = form.get_by_role("button", name="Release the hold")
    # The browser asks for one before sending.
    release.click()
    assert reason.evaluate("(e) => e.validity.valueMissing")
    assert holds.is_held(world.project)
    checked(page)

    reason.fill("   ")
    release.click()
    summary = page.get_by_role("alert").filter(has_text="There’s a problem")
    expect(summary).to_contain_text("Say why the legal hold is being released.")
    expect(reason).to_have_attribute("aria-invalid", "true")
    assert holds.is_held(world.project)
    checked(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth <= 1")
    checked(page)
    page.set_viewport_size({"width": 1440, "height": 900})

    reason.fill("Case closed; counsel confirmed on 2026-11-20.")
    release.click()
    page.get_by_text(f"Released the hold on {world.project.name} for Case 24-117.").wait_for()
    page.get_by_text("Case closed; counsel confirmed on 2026-11-20.").wait_for()
    assert not holds.is_held(world.project)
    hold = LegalHold.objects.get(pk=hold.pk)
    assert (hold.released_by, hold.release_reason) == (
        world.users["org_admin"],
        "Case closed; counsel confirmed on 2026-11-20.",
    )
    event = AuditEvent.objects.get(action="legal_hold.released", target_id=hold.pk)
    assert event.data["reason"] == "Case closed; counsel confirmed on 2026-11-20."
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_following_lineage_from_entity_to_entity(
    open_as: Any, world: World, color_scheme: str
) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.runs import catalog, services
    from iki_server.runs.models import Lineage

    admin = world.users["admin"]
    action = catalog.action("acme/protein.compute_properties")
    assert action is not None
    made = []
    for name, sequence in [("ABL1 precursor", "MKV"), ("ABL1", "MKVL"), ("ABL1 fragment", "KVL")]:
        created = entities.create(
            admin, "acme/protein.Protein", world.project, {"sequence": sequence, "name": name}
        )
        made.append(Entity.objects.get(pk=created.entity.pk))
    for parent, child in itertools.pairwise(made):
        run = services.launch(admin, action, world.project, {"protein": [parent.pk]}, {})
        Lineage.objects.create(
            entity=child, parent=parent, parent_revision=1, run=run, input="protein"
        )

    middle = made[1]
    page = open_as("admin", f"/entities/{middle.hrid}/", color_scheme)
    # The record's Lineage tab, not the rail's link to the same page, which a phrase of it
    # also matches.
    page.get_by_role("navigation", name="Record").get_by_role("link", name="Lineage").click()
    # What it was derived from, as a tree, walked with the keyboard.
    tree = page.get_by_role("tree", name="What it was derived from")
    tree.wait_for()
    item = tree.get_by_role("treeitem", name=re.compile("^ABL1 precursor"))
    item.wait_for()
    assert_clean(page)
    item.focus()
    page.keyboard.press("End")
    assert item.evaluate("e => e === document.activeElement")

    # What was derived from it, as the impact list, with why.
    page.get_by_role("link", name="Used by").click()
    page.get_by_role("link", name="List").click()
    table = page.get_by_role("table")
    table.get_by_role("link", name=re.compile("^ABL1 fragment")).wait_for()
    table.get_by_text(re.compile(r"made .* from .* \(its protein, at revision 1\)")).wait_for()
    assert_clean(page)

    page.get_by_role("link", name="Graph").click()
    graph = page.get_by_role("group", name=re.compile("^3 entities and 2 runs"))
    graph.wait_for()
    assert_clean(page)
    graph.get_by_role("link", name=re.compile("^ABL1 precursor")).hover()
    assert_clean(page)

    graph.get_by_role("link", name=re.compile("^ABL1 fragment")).click()
    page.get_by_role("heading", level=1, name="ABL1 fragment").wait_for()
    page.get_by_role("group", name=re.compile("^3 entities and 2 runs")).wait_for()
    assert page.get_by_role("link", name="Show later").count() == 0


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_proteins_sequence_in_its_viewer(open_as: Any, world: World, color_scheme: str) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    sequence = "MLEICLKLVGCKSKKGLSSSSSCYLEEALQRPVASDFEPQGLSEAARWNSKENLLAGPSENDPNLFVALYDFVASG"
    made = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": sequence, "name": "ABL1"}
    )
    from iki_plugin_acme__protein.models import Protein  # type: ignore[import-not-found]

    # Its hydropathy, as compute_properties sets it: Kyte-Doolittle over a window of nine.
    window = [KYTE_DOOLITTLE[r] for r in sequence]
    profile = [
        round(sum(window[i - 4 : i + 5]) / 9, 2) if 4 <= i < len(sequence) - 4 else None
        for i in range(len(sequence))
    ]
    Protein.objects.filter(pk=made.entity.pk).update(hydropathy=profile)
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/", color_scheme)
    page.get_by_role("link", name="Sequence").click()
    page.get_by_text(f"{len(sequence)} residues").wait_for()
    figure = page.get_by_role("figure", name="Sequence of ABL1")
    figure.get_by_text("61", exact=True).wait_for()
    legend = page.get_by_role("list", name="Tracks")
    legend.get_by_text(
        re.compile(r"^Hydropathy, on a scale from -4\.5 to 4\.5: highest ")
    ).wait_for()
    bars = figure.locator("svg.value-track rect")
    assert bars.count() == len(sequence) - 8
    box = bars.first.bounding_box()
    assert box is not None and box["width"] > 0 and box["height"] > 0
    assert_clean(page)


#: Stands in for the clipboard, keeping what the page copies: a test can't read the
#: system's in every browser.
# Whether the element is (at least partly) in the window, read at once: no waiting for it to
# come back into view.
IN_VIEW = """(selector) => {
  const box = document.querySelector(selector)?.getBoundingClientRect();
  return !!box && box.bottom > 0 && box.top < innerHeight;
}"""

CLIPBOARD = """() => {
  window.__copied = [];
  navigator.clipboard.writeText = (text) => {
    window.__copied.push(text);
    return Promise.resolve();
  };
}"""


def _record(world: World, tmp_path: Path, accession: str, path: Path) -> tuple[str, dict[str, Any]]:
    """An NCBI record as the DNA plugin's import reads it, registered as a plasmid: its
    HRID, and the values it was given."""
    from test_sequence_files import imported

    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    read = imported(tmp_path, path.read_bytes(), f"{accession}.gb")
    values = {
        "name": accession,
        "bases": read["bases"],
        "topology": read["topology"],
        "strandedness": read.get("strandedness", "double"),
        "features": read.get("features", []),
    }
    made = entities.create(world.users["admin"], "acme/dna.Plasmid", world.project, values)
    return Entity.objects.get(pk=made.entity.pk).hrid, values


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_dna_feature_library_workflow_in_the_sequence_viewer(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    """Show the source sequence, launch annotation, and locate the T7 promoter the source
    record OL989372.1 places at 4984..5002. Inject the first-party result and action into
    the browser server's fixture plugin. The dev cluster runs the actual worker action.
    """
    accession = "OL989372.1"
    hrid, _ = _record(
        world, tmp_path, accession, Path(__file__).parent / "data" / "plasmids" / f"{accession}.gb"
    )
    type_name = "acme/dna.Plasmid"
    action_name = "iki/plasmid.annotate_dna_sequence"
    result: list[dict[str, Any]] | None = None
    page = open_as("admin", "/projects/kinase-atlas/", color_scheme)
    action_out = page.request.get("/api/v1/actions/acme/protein.compute_properties").json()
    action_out.update(
        name=action_name,
        plugin="iki/plasmid",
        label="Annotate from feature library",
        inputs=[
            {**action_out["inputs"][0], "name": "sequence", "type": type_name, "label": "Sequence"}
        ],
        params=[],
        params_schema={"type": "object", "properties": {}},
        updates=[{"input": "sequence", "fields": ["library_features"]}],
    )

    def with_library_field(route: Route) -> None:
        response = route.fetch()
        types = response.json()
        # The installed types come a page at a time (ADR 0291): the plasmid's is on one.
        for sequence in (t for t in types["items"] if t["name"] == type_name):
            original = next(f for f in sequence["fields"] if f["name"] == "features")
            sequence["fields"].append({**original, "name": "library_features", "computed": True})
        route.fulfill(response=response, json=types)

    def with_library_action(route: Route) -> None:
        route.fulfill(json={"items": [action_out], "next_cursor": None})

    def with_library_result(route: Route) -> None:
        response = route.fetch()
        entity = response.json()
        entity["values"].update(
            library_features=result,
            library_annotated_with="Routine cloning-vector features 1.0"
            if result is not None
            else None,
        )
        route.fulfill(response=response, json=entity)

    page.route(re.compile(r"/api/v1/entity-types(\?[^/]*)?$"), with_library_field)
    page.route(re.compile(r"/api/v1/actions\?takes="), with_library_action)
    page.route(re.compile(rf"/api/v1/entities/{hrid}(?:\?.*)?$"), with_library_result)
    page.set_viewport_size({"width": 390, "height": 844})
    viewer = f"/entities/{hrid}/viewers/sequence/?project=kinase-atlas"
    page.goto(viewer)
    panel = page.locator("section", has=page.get_by_role("heading", name="Library features"))
    panel.get_by_text("No library search has run.").wait_for()
    launch = page.get_by_role("link", name="Annotate from library")
    launch.wait_for()
    assert_clean(page)
    launch.click()
    page.get_by_role("heading", level=2, name="Run an action").wait_for()
    action = page.get_by_role("combobox", name="Action")
    action.wait_for()
    assert action.input_value() == action_name
    page.get_by_role("group", name="Sequence").get_by_text(accession).wait_for()

    # The Lineage tab's count is held back until after the feature is shown, so the record's
    # tabs settle (and the one you're on is brought into view across them) with the reader
    # scrolled down to the feature: that mustn't take the page back up to the tabs.
    lineage: list[Route] = []
    graph = re.compile(r"/api/v1/entities/[^/]+/graph\?")
    page.route(graph, lambda route: lineage.append(route))
    result = [
        {
            "type": "promoter",
            "name": "T7 promoter",
            "start": 4984,
            "end": 5002,
            "strand": "forward",
            "qualifiers": {
                "source": "INSDC OL989372.1 4984..5002 (Cloning vector pET-28a-ZaTdT12)",
            },
        }
    ]
    page.goto(viewer)
    panel.get_by_text("Routine cloning-vector features 1.0").wait_for()
    row = panel.get_by_role("row").filter(has_text="Cloning vector pET-28a-ZaTdT12")
    row.get_by_role("cell", name="4984..5002", exact=True).wait_for()
    row.get_by_role("cell", name="forward", exact=True).wait_for()
    show = row.get_by_role("button", name="Show T7 promoter")
    show.focus()
    show.press("Enter")
    assert (
        row.get_by_role("button", name="Hide T7 promoter").get_attribute("aria-pressed") == "true"
    )
    # Where it is read from its box.
    shown = '.feature-shown[title="T7 promoter (promoter, 4984..5002)"]'
    page.locator(shown).wait_for()
    assert page.evaluate(IN_VIEW, shown)
    tabs = page.locator("nav.record-tabs")
    assert lineage and tabs.is_hidden()
    page.unroute(graph)
    for held in lineage:
        held.continue_()
    tabs.wait_for()
    assert page.evaluate(IN_VIEW, shown)
    assert page.get_by_role("heading", name="Features", exact=True).is_visible()
    assert page.evaluate("() => document.documentElement.scrollWidth - innerWidth <= 1")
    assert_clean(page)
    page.unroute_all(behavior="wait")


def _place(page: Page, row: int, after: int) -> tuple[float, float]:
    """Where to point to put the caret after the `after`th base (0 to 60) of a row, from 0:
    just short of the boundary, which the viewer rounds to."""
    line = page.locator(f".sequence-row[data-index='{row}'] .sequence-residues").first
    group = line.locator(".sequence-group").first
    box, unit = line.bounding_box(), group.bounding_box()
    assert box is not None and unit is not None
    ch = unit["width"] / 10
    column = after + max(after - 1, 0) // 10
    return box["x"] + (column - 0.2) * ch, box["y"] + box["height"] / 2


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_duplex_in_its_viewer_shows_both_strands_and_says_what_is_selected(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parents[2] / "plugins" / "dna" / "scripts"))
    import sequences  # type: ignore[import-not-found]

    hrid, values = _record(
        world, tmp_path, "L09137.2", Path(__file__).parent / "data" / "plasmids" / "L09137.2.gb"
    )
    bases = values["bases"]
    page = open_as("admin", f"/entities/{hrid}/viewers/sequence/", color_scheme)
    page.get_by_text("2,686 bp · circular · 0 features").wait_for()
    rows = page.get_by_role("application", name="Bases of L09137.2")
    rows.wait_for()
    # Under each row, the other strand, 3′ to 5′: pUC19c's TCGCGCGTTT pairs with AGCGCGCAAA.
    other = page.locator(".sequence-row[data-index='0'] .sequence-complement")
    assert (other.text_content() or "")[:10] == "AGCGCGCAAA"
    page.evaluate(CLIPBOARD)

    # Its first 24 bases, by pointer: from before the first to after the 24th, with the row
    # in view (not under what stays over the rows).
    page.set_viewport_size({"width": 1280, "height": 1400})
    page.locator(".sequence-row[data-index='0']").scroll_into_view_if_needed()
    page.mouse.click(*_place(page, 0, 0))
    page.keyboard.down("Shift")
    page.mouse.click(*_place(page, 0, 24))
    page.keyboard.up("Shift")
    first = bases[:24]
    gc, tm = sequences.gc_content(first), sequences.melting_temperature(first)
    assert (gc, tm) == (62.5, 64.2)
    readout = page.get_by_text(re.compile("^Bases 1–24 selected"))
    readout.wait_for()
    assert readout.text_content() == (
        f"Bases 1–24 selected: 24 bp · GC {gc:g}% · Tm {tm} °C (50 mM Na⁺)"
    )
    assert_clean(page)
    page.get_by_role("button", name="Copy", exact=True).click()
    page.get_by_role("status").filter(has_text="Copied 24 bases.").wait_for()
    page.get_by_role("button", name="Copy reverse complement").click()
    page.get_by_role("status").filter(has_text="reverse complement").wait_for()
    # Spelled out rather than taken from the plugin: what is under test is the button, and
    # a reverse complement the test computed the same way the page does would prove nothing.
    backwards = first.translate(str.maketrans("ACGT", "TGCA"))[::-1]
    assert page.evaluate("window.__copied") == [first, backwards]

    # By keyboard: the whole of it, whose GC content is what compute_composition records.
    rows.focus()
    page.keyboard.press("Control+a")
    whole = sequences.composition(bases)["gc_content"]
    page.get_by_text(
        f"Bases 1–2,686 selected: 2,686 bp · GC {whole:g}% · no Tm past 200 bases"
    ).wait_for()
    page.keyboard.press("Escape")
    page.keyboard.press("Control+Home")
    for _ in range(3):
        page.keyboard.press("Shift+ArrowRight")
    page.get_by_text("Bases 1–3 selected: 3 bp").wait_for()
    # Drawn once its row is, which the keys bring into view.
    page.locator(".sequence-caret").wait_for()
    assert page.locator(".sequence-caret").count() == 1
    assert_clean(page)

    # What's selected stays in view as the rows scroll under it.
    page.mouse.wheel(0, 3000)
    page.wait_for_function("() => window.scrollY > 1000")
    tools = page.locator(".sequence-tools").bounding_box()
    assert tools is not None and -1 <= tools["y"] <= 1, tools


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_finding_sites_in_a_plasmid_in_its_viewer(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    """pUC19c's polylinker, where the cloning tests cut it: EcoRI after base 396 and
    HindIII after 447 (server/tests/test_plasmid_cloning.py); and AatII near its end, at
    2,617 (Biopython's nt_search), brought into view below what stays over the rows."""
    hrid, _ = _record(
        world, tmp_path, "L09137.2", Path(__file__).parent / "data" / "plasmids" / "L09137.2.gb"
    )
    page = open_as("admin", f"/entities/{hrid}/viewers/sequence/", color_scheme)
    search = page.get_by_role("search", name="Find bases")
    box = search.get_by_role("searchbox", name="Find")
    box.fill("GAATTC")
    search.get_by_text("1 match.").wait_for()
    box.press("Enter")
    search.get_by_text("Match 1 of 1: bases 396–401, on both strands.").wait_for()
    page.get_by_text("Bases 396–401 selected: 6 bp").wait_for()
    assert_clean(page)
    box.fill("aagctt")
    box.press("Enter")
    search.get_by_text("Match 1 of 1: bases 447–452, on both strands.").wait_for()
    box.fill("GACGTC")
    search.get_by_role("button", name="Next").click()
    search.get_by_text("Match 1 of 1: bases 2,617–2,622, on both strands.").wait_for()
    page.wait_for_function(
        """() => {
          const mark = document.querySelector(".residue-current");
          const tools = document.querySelector(".sequence-tools");
          if (!mark || !tools) return false;
          const at = mark.getBoundingClientRect();
          return at.top >= tools.getBoundingClientRect().bottom && at.bottom <= innerHeight;
        }"""
    )
    box.fill("GAZ")
    search.get_by_text("Z isn't an IUPAC nucleotide code.").wait_for()
    assert box.get_attribute("aria-invalid") == "true"
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_plasmids_coding_sequences_read_as_genbank_translates_them(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    """pBR322 (J01749.1): tet, rop and bla, the last on the reverse strand, by the
    bacterial code its record names (table 11). Each lane reads what the record's own
    /translation says."""
    hrid, values = _record(
        world, tmp_path, "J01749.1", Path(__file__).parent / "data" / "plasmids" / "J01749.1.gb"
    )
    stated = {
        f["name"]: f["qualifiers"]["translation"]
        for f in values["features"]
        if f["type"] == "CDS" and "translation" in f.get("qualifiers", {})
    }
    assert len(stated) == 3, stated
    page = open_as("admin", f"/entities/{hrid}/viewers/sequence/", color_scheme)
    # Tall enough that every row is drawn, so each lane can be read whole.
    page.set_viewport_size({"width": 1280, "height": 9000})
    page.get_by_role("application", name="Bases of J01749.1").wait_for()
    page.wait_for_function(
        "() => document.querySelectorAll('.sequence-row').length >= Math.ceil(4361 / 60)"
    )
    read = page.evaluate(
        """() => {
          const out = {};
          for (const codon of document.querySelectorAll(".translation-track .codon")) {
            const [, name, number] = /^(.*), codon (\\d+):/.exec(codon.title);
            (out[name] ??= []).push([Number(number), codon.textContent]);
          }
          return Object.fromEntries(
            Object.entries(out).map(([name, codons]) => [
              name,
              codons.sort((a, b) => a[0] - b[0]).map((c) => c[1]).join("").replace(/\\*$/, ""),
            ]),
          );
        }"""
    )
    assert read == stated
    page.set_viewport_size({"width": 1280, "height": 900})
    frames = page.get_by_role("group", name="Reading frames")
    frames.get_by_role("checkbox", name="+1").check()
    frames.get_by_role("checkbox", name="\N{MINUS SIGN}3").check()
    assert page.locator(".translation[data-label='+1']").first.is_visible()
    assert_clean(page)
    # On a phone, the rows scroll across within their figure; the page never does.
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("() => document.documentElement.scrollWidth - innerWidth <= 1")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_records_page_and_its_audit_trail(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    """A record's page as ADR 0176 lays it out: its values without who set each, a history
    button beside each with its last change, a rail with its map, and an Audit trail tab
    that narrows by field, shows each field's last change, and downloads whole as CSV for
    an inspector. At a phone's width nothing scrolls across."""
    import csv

    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    hrid, _ = _record(
        world, tmp_path, "J01749.1", Path(__file__).parent / "data" / "plasmids" / "J01749.1.gb"
    )
    # A plasmid is a registry type (ADR 0115): Kinase Atlas's record of its entry.
    record = Entity.objects.get(hrid=hrid, owner_project=world.project)
    entities.update(
        world.users["admin"],
        record,
        {"name": "pBR322"},
        expected_revision=1,
        reason="Its common name",
    )
    page = open_as("admin", f"/entities/{hrid}/?project=kinase-atlas", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("heading", level=1, name="pBR322").wait_for()
    rail = page.get_by_role("complementary", name="About this record")
    rail.get_by_role("img", name=re.compile(r"^Circular map of pBR322, 4,361 bp: ")).wait_for()
    rail.get_by_role("region", name="Details").get_by_text("Last changed").wait_for()
    tabs = page.get_by_role("navigation", name="Record")
    # Its record's revisions, and its entry's, each said.
    record.refresh_from_db()
    assert record.entry is not None
    count = record.revision + record.entry.revision
    tabs.get_by_role(
        "link",
        name=re.compile(
            rf"^Audit trail\s*,\s*{count}\s*revisions: {record.revision} of this record, "
            rf"{record.entry.revision} of its entry$"
        ),
    ).wait_for()
    assert page.get_by_text("Changed by").count() == 0
    # Its rail is 260 to 300 pixels wide, and a fact's value has room to read on a line.
    details = rail.get_by_role("region", name="Details")
    rail_box = rail.bounding_box()
    assert rail_box is not None and 260 <= rail_box["width"] <= 300, rail_box
    created = details.locator("dd").filter(has_text="Admin,").first.bounding_box()
    assert created is not None and created["width"] >= 150, created
    assert_clean(page)

    page.get_by_role("button", name="History of Name").click()
    history = page.get_by_role("dialog", name="History of Name")
    history.get_by_text("“Its common name”").wait_for()
    history.get_by_text(re.compile(r"\d\d:\d\d UTC$")).wait_for()
    # Before and After line up with Who, How and When.
    values_x = {
        round(box["x"])
        for box in (
            history.locator(f".{list_} dd").nth(0).bounding_box()
            for list_ in ("history-change", "history-facts")
        )
        if box is not None
    }
    assert len(values_x) == 1, values_x
    assert_clean(page)
    page.keyboard.press("Escape")
    history.wait_for(state="hidden")

    # The ⋯ menu has the rest of its actions, the audit trail's CSV among them.
    page.get_by_role("button", name=f"More actions for {hrid}").click()
    page.get_by_role("menuitem", name="Download audit trail (CSV)").wait_for()
    assert_clean(page)
    page.keyboard.press("Escape")

    tabs.get_by_role("link", name=re.compile("^Audit trail")).click()
    page.get_by_text("Revision 2: Edited by Admin").wait_for()
    page.get_by_role("heading", name=f"Registry entry {hrid}").wait_for()
    # At a desk its views, its filters, what can't change and its download share one row,
    # each label on its select's line.
    toolbar = [
        page.get_by_role("button", name="Timeline"),
        page.get_by_role("combobox", name="Person"),
        page.get_by_role("link", name="Export for inspection"),
    ]
    middles = []
    for control in toolbar:
        box = control.bounding_box()
        assert box is not None
        middles.append(box["y"] + box["height"] / 2)
    assert max(middles) - min(middles) <= 2, middles
    label = page.locator(".trail-toolbar label").first.bounding_box()
    select = page.get_by_role("combobox", name="Person").bounding_box()
    assert label is not None and select is not None
    assert abs((label["y"] + label["height"] / 2) - (select["y"] + select["height"] / 2)) <= 2
    # Each revision exports as it was, the latest too, from its title's row.
    for number in (1, 2):
        export = page.get_by_role("button", name=f"Export revision {number}")
        title = page.locator(".trail-title").filter(has=export)
        assert title.count() == 1
        box = export.bounding_box()
        assert box is not None and box["height"] <= 26, box
    assert_clean(page)
    page.get_by_role("combobox", name="Field").select_option("name")
    page.wait_for_url(re.compile(r"[?&]field=name"))
    page.get_by_role("button", name="By field").click()
    table = page.get_by_role("table", name="Each field’s last change")
    table.get_by_role("row", name=re.compile(r"^Name pBR322 Admin")).wait_for()
    assert table.get_by_role("row").count() == 2
    assert_clean(page)

    with page.expect_download() as downloading:
        page.get_by_role("link", name="Export for inspection").click()
    saved = tmp_path / downloading.value.suggested_filename
    downloading.value.save_as(saved)
    # WebKit on Linux suggests it with underscores for its spaces.
    assert saved.name.replace("_", " ") == f"{hrid} Kinase Atlas audit trail.csv"
    rows = list(csv.DictReader(saved.read_text(encoding="utf-8-sig").splitlines()))
    # The whole trail, whatever the page was narrowed to.
    assert {r["Revision"] for r in rows} == {"1", "2"}
    renamed = next(r for r in rows if r["Revision"] == "2")
    assert (renamed["Field"], renamed["Before"], renamed["After"]) == ("Name", "J01749.1", "pBR322")
    assert renamed["Reason"] == "Its common name" and renamed["Who"] == "Admin"

    # On a phone, its rail goes under its values, and nothing scrolls across.
    wide = "() => document.documentElement.scrollWidth - innerWidth <= 1"
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(wide)
    assert_clean(page)
    page.goto(f"/entities/{hrid}/?project=kinase-atlas")
    page.get_by_role("heading", level=1, name="pBR322").wait_for()
    rail.get_by_role("img", name=re.compile("^Circular map")).wait_for()
    assert page.evaluate(wide)
    # Its title comes first, then its actions.
    heading = page.get_by_role("heading", level=1).bounding_box()
    edit = page.get_by_role("link", name="Edit", exact=True).bounding_box()
    assert heading is not None and edit is not None
    assert heading["y"] + heading["height"] <= edit["y"], (heading, edit)
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_records_lists_of_objects_and_its_map_from_another_records_sequence(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    """A vector's sequence is its plasmid's, by reference, as a plasmid of the plasmid
    plugin's is its DNA sequence's (ADR 0163): its rail draws its own features along that
    sequence, and its features field says how many, with them listed in a Features card.
    A list of objects nothing draws reads as a table, never as JSON."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    hrid, _ = _record(
        world, tmp_path, "J01749.1", Path(__file__).parent / "data" / "plasmids" / "J01749.1.gb"
    )
    plasmid = Entity.objects.get(hrid=hrid, owner_project=world.project)
    features = [
        {"type": "promoter", "name": "tet promoter", "start": 20, "end": 80, "strand": "reverse"},
        {"type": "CDS", "name": "TcR", "start": 86, "end": 1276, "strand": "forward"},
        {"type": "rep_origin", "name": "ori", "start": 2534, "end": 3122, "strand": "reverse"},
    ]
    made = entities.create(
        world.users["admin"],
        "acme/dna.Vector",
        world.project,
        {
            "name": "pBR322 vector",
            "plasmid": str(plasmid.entry_id),
            "annotated_features": [
                {**f, "qualifiers": {"note": "found by the annotation"}} for f in features
            ],
        },
    ).entity
    vector = Entity.objects.get(pk=made.pk)
    page = open_as("admin", f"/entities/{vector.hrid}/", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("heading", level=1, name="pBR322 vector").wait_for()
    rail = page.get_by_role("complementary", name="About this record")
    rail.get_by_role(
        "img", name="Circular map of pBR322 vector, 4,361 bp: tet promoter, TcR, ori"
    ).wait_for()
    values = page.get_by_role("region", name="Values")
    values.get_by_text(re.compile(r"^3 features, drawn in its map")).wait_for()
    page.get_by_role("table", name="Features of pBR322 vector").wait_for()
    assert "{" not in values.inner_text()
    assert_clean(page)

    # Features that don't lie along its sequence, as pBR322's TcR doesn't along pUC19c's
    # 2,686 bp, can't be drawn: they're a table of their own, as any list of objects is.
    puc, _ = _record(
        world, tmp_path, "L09137.2", Path(__file__).parent / "data" / "plasmids" / "L09137.2.gb"
    )
    other = entities.create(
        world.users["admin"],
        "acme/dna.Vector",
        world.project,
        {
            "name": "pUC19c misannotated",
            "plasmid": str(Entity.objects.get(hrid=puc, owner_project=world.project).entry_id),
            "annotated_features": [
                {"type": "CDS", "name": "TcR", "start": 3086, "end": 4276, "strand": "forward"}
            ],
        },
    ).entity
    page.goto(f"/entities/{Entity.objects.get(pk=other.pk).hrid}/")
    page.locator("h1", has_text="pUC19c misannotated").wait_for()
    own = page.locator("section[aria-labelledby='values-title']")
    table = own.locator(".object-list table")
    assert table.locator("caption").inner_text() == "Annotated features"
    assert table.locator("th").all_inner_texts() == ["Name", "Type", "Position", "Strand"]
    assert table.locator("tbody td").all_inner_texts() == ["TcR", "CDS", "3,086–4,276", "forward"]
    assert "{" not in own.inner_text()
    assert page.locator(".record-rail svg").count() == 0
    assert_clean(page)


def _stored_file(
    world: World,
    s3_endpoint: str,
    bucket: str,
    path: Path,
    media_type: str,
    *,
    scanned: bool = True,
    **fields: Any,
) -> Any:
    """A file in the project, its bytes put straight into the store the server reads: found
    clean by the scan, or, if not ``scanned``, still waiting for it. ``fields`` are what an
    upload also records (when its data was acquired, say): a file is never changed after."""
    import hashlib

    import boto3
    from scan_fixtures import clean_blob

    from iki_server.files.models import Blob, File

    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    key = f"browser-files/{sha256}"
    boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
    ).put_object(Bucket=bucket, Key=key, Body=data)
    uri = f"s3://{bucket}/{key}"
    blob = (
        clean_blob(sha256=sha256, size=len(data), storage_uri=uri)
        if scanned
        else Blob.objects.create(sha256=sha256, size=len(data), storage_uri=uri)
    )
    return File.objects.create(
        blob=blob,
        name=path.name,
        media_type=media_type,
        project=world.project,
        created_by=world.users["admin"],
        **fields,
    )


#: Kyte and Doolittle's hydropathy values (1982).
KYTE_DOOLITTLE = dict(
    zip(
        "ARNDCQEGHILKMFPSTWYV",
        (
            1.8,
            -4.5,
            -3.5,
            -3.5,
            2.5,
            -3.5,
            -3.5,
            -0.4,
            -3.2,
            4.5,
            3.8,
            -3.9,
            1.9,
            2.8,
            -1.6,
            -0.8,
            -0.7,
            -0.9,
            -1.3,
            4.2,
        ),
        strict=True,
    )
)

#: What a frame's code finds when it tries to reach beyond it (SANDBOX_PROBES).
ISOLATED = {
    "origin": "null",
    "api": "refused",
    "cookie": "refused",
    "storage": "refused",
    "page": "refused",
    "pageStorage": "refused",
}

#: Whether the frame's browser draws 3D.
WEBGL = """() => {
  const canvas = document.createElement("canvas");
  return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
}"""


#: How long the structure viewer gives Mol* to start, and a margin (SHOW_TIMEOUT_MS in
#: web/app/src/pages/structure-viewer.tsx): by then the page has said it's drawn, can't
#: be, or failed, so a wait this long ends with the page's own answer.
MOL_STAR_STARTS = 60_000 + 10_000


def drawn_in_mol_star(page: Page, names: str) -> Frame | None:
    """Waits for the page to draw ``names`` in Mol*, through each of its steps, and returns
    Mol*'s frame once it has; or None if this browser can't draw 3D, as the page says.

    First the files are read ("Reading …"), then Mol* starts in its frame ("Drawing …"),
    and then the frame is shown, or the page says why not. Waiting for "Drawing …" to go
    is no signal by itself: while the files are still being read it isn't there yet."""
    cant = page.get_by_text("This browser can’t draw 3D structures")
    failed = page.locator(".alert-error")
    # The frame is made once the files are read, as Mol* starts.
    page.locator("iframe.structure-frame").or_(cant).or_(failed).first.wait_for()
    page.get_by_text(f"Drawing {names}…").wait_for(state="detached", timeout=MOL_STAR_STARTS)
    assert not failed.count(), failed.all_inner_texts()
    if cant.count():
        return None
    # The frame's element, then its page: under load, WebKit can list a frame before it's
    # loaded, so `page.frames` isn't to be read for it.
    frame = page.locator("iframe.structure-frame").element_handle().content_frame()
    assert frame is not None
    frame.wait_for_url("**/viewers/structure/")
    # The page says it's shown when Mol* says so, once it has drawn its canvas.
    frame.locator("canvas").first.wait_for()
    return frame


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_structure_in_mol_star_in_its_frame(
    open_as: Any,
    world: World,
    color_scheme: str,
    browser_name: str,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    crambin = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "TTCCPSIVAR", "name": "Crambin"}
    ).entity
    file = _stored_file(
        world,
        _s3_endpoint,
        _s3_bucket,
        Path(__file__).parent / "data" / "crambin.pdb",
        "chemical/x-pdb",
    )
    made = entities.create(
        admin,
        "acme/protein.ProteinStructure",
        world.project,
        {"structure": file.pk, "method": "xray", "chains": [crambin.pk]},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/", color_scheme)
    page.get_by_role("link", name="Structure", exact=True).click()
    page.get_by_role("link", name="Download crambin.pdb").wait_for()
    frame = drawn_in_mol_star(page, "crambin.pdb")
    if frame is None:
        # Some browsers can't draw 3D at all (no GPU, WebGL off): the page says so, in place
        # of the frame, and offers the file to open elsewhere.
        assert browser_name == "firefox", f"{browser_name} has no WebGL"
        assert not page.locator("iframe.structure-frame").count()
        assert_clean(page)
        return
    assert frame.evaluate(WEBGL)
    frame.get_by_text("Sequence of").first.wait_for()
    assert_clean(page)

    # A saved view keeps where the camera is, and opens on its revision.
    page.get_by_role("button", name="Save view").click()
    dialog = page.get_by_role("alertdialog", name="Save this view")
    dialog.get_by_role("textbox", name="Name").fill("Crambin, N-terminus")
    dialog.get_by_role("button", name="Save view").click()
    page.wait_for_url(re.compile(r"/entities/VIEW-\d+/$"))
    page.get_by_text(f"Revision 1 of {hrid}, as the view saved it.").wait_for()
    assert drawn_in_mol_star(page, "crambin.pdb")
    saved = entities.current_values_of([Entity.objects.get(type="iki/core.View")])
    (state,) = [v["state"] for v in saved.values()]
    assert set(state) == {"version", "camera", "targets"} and state["version"] == 2
    assert len(state["camera"]["position"]) == 3 and state["targets"] == [{}]
    assert_clean(page)
    page.goto(page.url.split("/entities/")[0] + f"/entities/{hrid}/viewers/structure/")
    page.get_by_role("link", name="Crambin, N-terminus").wait_for()

    # Mol* can't read Iki's cookies, storage or page, or call the API as the person.
    frame = drawn_in_mol_star(page, "crambin.pdb")
    assert frame is not None
    assert frame.evaluate(SANDBOX_PROBES) == ISOLATED
    frame.evaluate("() => { location.href = 'about:blank'; }")
    page.get_by_role("alert").get_by_text(
        "The structure viewer tried to open another page, so Iki closed it."
    ).wait_for()
    assert page.locator("iframe").count() == 0


def _moved_pdb(source: Path, out: Path, dx: float) -> Path:
    """The same structure moved ``dx`` Å along x: another file, with the same residues."""
    lines = []
    for line in source.read_text().splitlines():
        if line.startswith(("ATOM", "HETATM")):
            x = float(line[30:38]) + dx
            line = f"{line[:30]}{x:8.3f}{line[38:]}"
        lines.append(line)
    out.write_text("\n".join(lines) + "\n")
    return out


def test_a_view_superposes_its_structures_in_one_mol_star_scene(
    open_as: Any,
    world: World,
    browser_name: str,
    tmp_path: Path,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> None:
    """Two structures in one view (ADR 0110): each its own component in Mol*'s scene,
    superposed on the first, and saved with how it was moved. The first has crambin's
    first six residues and the moved one all ten, so the fit is over the six paired."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    crambin = Path(__file__).parent / "data" / "crambin.pdb"
    first = tmp_path / "crambin.pdb"
    first.write_text(
        "\n".join(
            line
            for line in crambin.read_text().splitlines()
            if not line.startswith(("ATOM", "HETATM")) or int(line[22:26]) <= 6
        )
        + "\n"
    )
    made = []
    for path in (first, _moved_pdb(crambin, tmp_path / "moved.pdb", 10.0)):
        file = _stored_file(world, _s3_endpoint, _s3_bucket, path, "chemical/x-pdb")
        made.append(
            entities.create(
                admin, "acme/protein.ProteinStructure", world.project, {"structure": file.pk}
            ).entity
        )
    values = {
        "name": "Crambin, twice",
        "targets": [e.pk for e in made],
        "target_revisions": [1, 1],
        "viewers": ["structure", "structure"],
        "state": {"version": 2, "targets": [{"label": "first"}, {"label": "moved"}]},
    }
    view = entities.create(admin, "iki/core.View", world.project, values).entity
    hrid = Entity.objects.get(pk=view.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/")
    page.get_by_role("list", name="Structures").wait_for()
    if drawn_in_mol_star(page, "crambin.pdb and moved.pdb") is None:
        assert browser_name == "firefox", f"{browser_name} has no WebGL"
        assert_clean(page)
        return
    page.get_by_role("button", name="Superpose on first").click()
    said = page.locator(".notices").get_by_text(re.compile(r"^moved"))
    said.wait_for(timeout=60_000)
    assert said.inner_text() == "moved: RMSD 0.00 Å over 6 residues."
    page.get_by_role("button", name="Save the view").click()
    page.get_by_text(f"Saved {hrid}, revision 2.").wait_for()
    (saved,) = entities.current_values_of([Entity.objects.get(pk=view.pk)]).values()
    first, moved = saved["state"]["targets"]
    assert first == {"label": "first", "colour": "#0072b2"}
    assert moved["superposed"] == {"onto": 0, "rmsd": 0.0, "residues": 6}
    # Moved back the 10 Å it was moved along x.
    assert abs(moved["transform"][12] + 10.0) < 0.01, moved["transform"]
    assert_clean(page)

    # It opens as it was saved, moved as it was.
    page.reload()
    assert drawn_in_mol_star(page, "crambin.pdb and moved.pdb")
    page.get_by_role("button", name="Save the view").click()
    # Saved as it opened: nothing moved, so there may be nothing new to keep.
    page.get_by_text(re.compile(rf"^Saved {hrid}, revision [23]\.$")).wait_for()
    (again,) = entities.current_values_of([Entity.objects.get(pk=view.pk)]).values()
    assert again["state"]["targets"][1]["transform"] == moved["transform"]
    assert_clean(page)


#: How many colours the canvas has drawn, sampled: a blank one has one.
CANVAS_COLOURS = """() => {
  const canvas = document.querySelector(".alignment-canvas canvas");
  const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
  const seen = new Set();
  for (let i = 0; i < data.length; i += 4 * 7) {
    seen.add((data[i] << 16) | (data[i + 1] << 8) | data[i + 2]);
  }
  return seen.size;
}"""

#: The policy the parsers' worker script was served with.
WORKER_POLICY = """async () => {
  const url = performance.getEntriesByType("resource").map((r) => r.name)
    .find((name) => /\\/assets\\/worker-[\\w-]+\\.js$/.test(name));
  return url ? (await fetch(url)).headers.get("Content-Security-Policy") : null;
}"""


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_an_alignment_in_its_viewer(
    open_as: Any,
    world: World,
    color_scheme: str,
    tmp_path: Path,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    abl1 = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
    ).entity
    abl2 = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MLEVCLKLVGA", "name": "ABL2"}
    ).entity
    hrid1, hrid2 = (Entity.objects.get(pk=e.pk).hrid for e in (abl1, abl2))
    fasta = tmp_path / "abl.afa"
    # Wide enough to scroll: the viewer draws only what's in view.
    tail = "ACDEFGHIKLMNPQRSTVWY" * 15
    fasta.write_text(
        f">{hrid2} ABL2\nMLEVCLKLVGA{tail}\n"
        f">{hrid1} ABL1\nMLEICLKLVG-{tail}\n"
        f">read_7 a Sanger read\nMLEICLR-VGA{tail}\n"
    )
    file = _stored_file(world, _s3_endpoint, _s3_bucket, fasta, "chemical/seq-aa-fasta")
    made = entities.create(
        admin,
        "iki/core.Alignment",
        world.project,
        {
            "name": "ABL kinases",
            "reference": abl2.pk,
            "members": [abl1.pk, abl2.pk],
            "aligned": file.pk,
            "method": "By hand",
        },
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/", color_scheme)
    page.get_by_role("navigation", name="Record").get_by_role("link", name="Alignment").click()
    grid = page.get_by_role("application", name="Alignment of abl.afa")
    grid.wait_for()
    page.get_by_text("3 rows, 311 columns").wait_for()
    # iki-wasm read it in its worker, which runs under a policy of its own.
    policy = page.evaluate(WORKER_POLICY)
    assert policy is not None and "script-src 'self' 'wasm-unsafe-eval'" in policy
    assert page.evaluate(CANVAS_COLOURS) > 4
    reference = page.locator(".alignment-label-pinned")
    assert reference.get_by_role("link").inner_text() == hrid2
    assert grid.get_by_role("link", name=hrid1).get_attribute("href") == f"/entities/{hrid1}/"
    grid.get_by_text("read_7", exact=True).wait_for()
    assert_clean(page)

    # The keys move the cursor, which the page reads out.
    grid.focus()
    page.keyboard.press("ArrowDown")
    for _ in range(10):
        page.keyboard.press("ArrowRight")
    at = page.locator(".alignment-at")
    assert at.inner_text().startswith(f"{hrid1}, column 11: a gap.")
    page.keyboard.press("End")
    assert at.inner_text().startswith(f"{hrid1}, column 311: Y, residue 310 of 310.")
    page.get_by_role("switch", name=f"Only where they differ from {hrid2}").check()
    page.get_by_role("combobox", name="Colour").select_option("all")
    assert page.evaluate(CANVAS_COLOURS) > 4
    assert_clean(page)

    # The sequences in Iki can be picked, to run an action on.
    page.get_by_role("button", name="Select every sequence in Iki (2)").click()
    run = page.get_by_role("link", name="Run action on them")
    assert run.get_attribute("href") == (
        f"/projects/{world.project.slug}/runs/new/?entities={hrid2}%2C{hrid1}"
    )
    assert_clean(page)

    # A saved view keeps where the cursor is.
    page.get_by_role("button", name="Save view").click()
    dialog = page.get_by_role("alertdialog", name="Save this view")
    dialog.get_by_role("textbox", name="Name").fill("ABL1's last residue")
    dialog.get_by_role("button", name="Save view").click()
    page.wait_for_url(re.compile(r"/entities/VIEW-\d+/$"))
    page.get_by_text(f"Revision 1 of {hrid}, as the view saved it.").wait_for()
    page.get_by_role("application", name="Alignment of abl.afa").wait_for()
    assert page.locator(".alignment-at").inner_text().startswith(f"{hrid1}, column 311")
    saved = entities.current_values_of([Entity.objects.get(type="iki/core.View")])
    (state,) = [v["state"] for v in saved.values()]
    assert state == {"version": 2, "targets": [{"position": {"column": 311, "row": 2}}]}
    assert_clean(page)


def test_a_row_on_its_sequences_other_strand_is_numbered_down(
    open_as: Any, world: World, tmp_path: Path, _s3_endpoint: str, _s3_bucket: str
) -> None:
    """The dna plugin's alignments head a sequence turned round DNA-…/end-start, as Rfam
    writes a reverse-strand row: iki-wasm reads it in the browser as that sequence, linked,
    and the cursor numbers its bases down from its last."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    # pUC19's EcoRI-to-SacI stretch, and a clone of it registered from its other strand.
    forward = "GAATTCGAGCTCGGTACCCGGG"
    clone = "CCCGGGTACCGAGCTCGAATTC"
    designed = entities.create(
        admin, "acme/dna.DNASequence", world.project, {"bases": forward, "name": "MCS"}
    ).entity
    turned = entities.create(
        admin, "acme/dna.DNASequence", world.project, {"bases": clone + "A", "name": "MCS+A"}
    ).entity
    hrid1, hrid2 = (Entity.objects.get(pk=e.pk).hrid for e in (designed, turned))
    fasta = tmp_path / "mcs.fasta"
    # The clone's reverse complement: its 23rd base (T, of the A added) first, down to 1.
    fasta.write_text(f">{hrid1}\n-{forward}\n>{hrid2}/23-1\nT{forward}\n")
    file = _stored_file(world, _s3_endpoint, _s3_bucket, fasta, "chemical/seq-na-fasta")
    made = entities.create(
        admin,
        "iki/core.Alignment",
        world.project,
        {
            "name": "MCS clones",
            "reference": designed.pk,
            "members": [designed.pk, turned.pk],
            "aligned": file.pk,
            "method": "MAFFT --adjustdirection",
        },
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/viewers/alignment/")
    grid = page.get_by_role("application", name="Alignment of mcs.fasta")
    grid.wait_for()
    assert grid.get_by_role("link", name=hrid2).get_attribute("href") == f"/entities/{hrid2}/"
    grid.focus()
    page.keyboard.press("ArrowDown")
    at = page.locator(".alignment-at")
    assert at.inner_text().startswith(f"{hrid2}, column 1: T, residue 23 of 23–1, its reverse")
    page.keyboard.press("End")
    assert at.inner_text().startswith(f"{hrid2}, column 23: G, residue 1 of 23–1")
    assert_clean(page)


# What a component's code could try, run in its frame, with its frame's origin and policy.
SANDBOX_PROBES = """
async () => {
  const tried = async (what) => {
    try {
      await what();
      return "allowed";
    } catch (error) {
      return "refused";
    }
  };
  return {
    origin: self.origin,
    api: await tried(() => fetch("/api/v1/me")),
    cookie: await tried(() => document.cookie),
    storage: await tried(() => localStorage.length),
    page: await tried(() => window.parent.document.title),
    pageStorage: await tried(() => window.parent.localStorage.length),
  };
}
"""


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_plugins_component_runs_sandboxed_in_its_tab(
    open_as: Any, world: World, color_scheme: str
) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    made = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/", color_scheme)
    page.get_by_role("link", name="Residues").click()
    shown = page.frame_locator('iframe[title="Residues"]')
    shown.get_by_text(f"{hrid}: 10 residues").wait_for()
    assert_clean(page)

    # What a person picks in it, the page reads back and offers to the run page.
    shown.get_by_role("button", name="Select").click()
    page.get_by_role("status").filter(has_text="1 protein selected").wait_for()
    assert_clean(page)

    # Its code can't read Iki's cookies, storage or page, or call the API as the person.
    (frame,) = [f for f in page.frames if "/plugin-components/acme/protein.residues/" in f.url]
    assert frame.evaluate(SANDBOX_PROBES) == {
        "origin": "null",
        "api": "refused",
        "cookie": "refused",
        "storage": "refused",
        "page": "refused",
        "pageStorage": "refused",
    }
    # Nor can it go elsewhere and stay: the page closes it.
    frame.evaluate("() => { location.href = 'about:blank'; }")
    page.get_by_role("alert").get_by_text(
        "Residues tried to open another page, so Iki closed it."
    ).wait_for()
    assert page.locator("iframe").count() == 0

    page.get_by_role("link", name="Run action on it").click()
    page.wait_for_url(re.compile(rf"/projects/kinase-atlas/runs/new/\?entities={hrid}$"))


def test_a_witness_opens_a_pinned_revision_as_iki_shows_it(open_as: Any, world: World) -> None:
    """A revision a component's entity pins, as an experiment pins the results its witness
    signs (ADR 0189), opens in the app, through its router: the record, or its registry
    entry, as it was then, read only, saying it isn't the record as it is now, with that
    and the revision's JSON a link away. Only someone who can read the record sees it.
    The fixture plugins have no type that pins revisions, so the protein's response is
    given the pins an experiment's ``result_pins`` would hold, of the DNA it refers to."""
    from iki_server.entities import services as entities

    editor = world.users["editor"]
    made = entities.create(
        editor, "acme/dna.DNASequence", world.project, {"bases": "ATGGCC", "name": "abl.dna"}
    )
    dna = Entity.objects.get(pk=made.entity.pk)
    entities.update(
        editor, dna, {"name": "ABL1 cDNA"}, expected_revision=1, reason="Named for its insert"
    )
    dna_hrid, dna_entry = Entity.objects.values_list("hrid", "entry_id").get(pk=dna.pk)
    protein = entities.create(
        editor,
        "acme/protein.Protein",
        world.project,
        {"sequence": "MKVL", "name": "ABL1", "encoded_by": dna_entry},
    ).entity
    hrid = Entity.objects.get(pk=protein.pk).hrid
    pin = {
        "record_id": str(dna.pk),
        "record_revision": 1,
        "entry_id": str(dna_entry),
        "entry_revision": 1,
    }

    def pinned(route: Route) -> None:
        response = route.fetch()
        body = response.json()
        body["values"]["evidence"] = [pin]
        route.fulfill(response=response, json=body)

    page = open_as("viewer", "/")
    page.route(re.compile(rf"/api/v1/entities/{hrid}(\?.*)?$"), pinned)
    page.goto(f"/entities/{hrid}/components/acme/protein.residues/?project=kinase-atlas")
    tab = page.frame_locator('iframe[title="Residues"]')
    tab.get_by_text(f"{hrid}: 4 residues").wait_for()
    (frame,) = [f for f in page.frames if "/plugin-components/acme/protein.residues/" in f.url]
    ask = """([entity, revision]) => document.querySelector("protein-residues").dispatchEvent(
        new CustomEvent("iki-open", { bubbles: true, detail: { entity, revision } }))"""
    # A revision no pin names opens nothing, not even the record as it is now.
    frame.evaluate(ask, [str(dna.pk), 2])
    page.wait_for_timeout(200)
    assert "/components/acme/protein.residues/" in page.url
    # The pinned one: Iki's page, in the app, never the API's document.
    documents: list[str] = []
    page.on("framenavigated", lambda f: documents.append(f.url) if f == page.main_frame else None)
    frame.evaluate(ask, [str(dna.pk), 1])
    page.wait_for_url(re.compile(rf"/entities/{dna.pk}/revisions/1/$"))
    page.get_by_role("heading", level=1, name=f"Revision 1 of {dna_hrid}").wait_for()
    assert not any("/api/" in url for url in documents)
    note = page.get_by_role("note", name="A past revision")
    expect(note).to_contain_text(f"You’re reading revision 1 of {dna_hrid} as it was saved on")
    # Named since, it's later now (and later still if a run has computed something since).
    expect(note).to_contain_text(re.compile(r"It’s at revision [2-9]\d* now"))
    values = page.get_by_role("region", name="Values at revision 1")
    expect(values).to_contain_text("abl.dna")
    expect(values).not_to_contain_text("ABL1 cDNA")
    assert page.get_by_role("navigation", name="Record").count() == 0
    json = note.get_by_role("link", name="Revision 1 as JSON")
    assert json.get_attribute("href") == f"/api/v1/entities/{dna.pk}/revisions/1"
    assert_clean(page)
    # Its JSON is the API's answer for the same person, and so is the record as it is now.
    answer = page.request.get(f"/api/v1/entities/{dna.pk}/revisions/1")
    assert answer.ok and answer.json()["values"]["name"] == "abl.dna"
    note.get_by_role("link", name=f"Open {dna_hrid} as it is now").click()
    page.wait_for_url(re.compile(rf"/entities/{dna_hrid}/\?project=kinase-atlas$"))
    page.get_by_role("heading", level=1, name="ABL1 cDNA").wait_for()

    # Its registry entry's pinned revision: the entry's page at that revision.
    page.go_back()
    page.go_back()
    tab.get_by_text(f"{hrid}: 4 residues").wait_for()
    (frame,) = [f for f in page.frames if "/plugin-components/acme/protein.residues/" in f.url]
    frame.evaluate(ask, [str(dna_entry), 1])
    page.wait_for_url(re.compile(rf"/entries/{dna_entry}/revisions/1/$"))
    page.get_by_role("heading", level=1, name=f"Revision 1 of {dna_hrid}").wait_for()
    entry_note = page.get_by_role("note", name="A past revision")
    expect(entry_note).to_contain_text(f"You’re reading revision 1 of {dna_hrid}")
    assert entry_note.get_by_role("link", name="Revision 1 as JSON").get_attribute("href") == (
        f"/api/v1/entries/{dna_entry}/revisions/1"
    )
    page.get_by_role("region", name="Values at revision 1").get_by_text("ATGGCC").wait_for()
    assert_clean(page)
    page.context.close()

    # Each, at a phone's width, in the light and the dark, fits and passes axe.
    for path in (f"/entities/{dna.pk}/revisions/1/", f"/entries/{dna_entry}/revisions/1/"):
        for scheme in ("light", "dark"):
            phone = open_as("viewer", path, scheme)
            phone.set_viewport_size({"width": 390, "height": 844})
            phone.get_by_role("region", name="Values at revision 1").wait_for()
            assert phone.evaluate(NO_SIDEWAYS_SCROLL), (path, scheme)
            assert_clean(phone)
            phone.context.close()

    # Someone who can't read the record finds nothing there, as its revision API answers.
    outsider = open_as("outsider", f"/entities/{dna.pk}/revisions/1/")
    outsider.get_by_role("heading", level=1, name="Not found").wait_for()
    assert outsider.get_by_text("abl.dna").count() == 0
    assert outsider.request.get(f"/api/v1/entities/{dna.pk}/revisions/1").status == 404
    outsider.goto(f"/entries/{dna_entry}/revisions/1/")
    outsider.get_by_role("heading", level=1, name="Not found").wait_for()
    assert outsider.request.get(f"/api/v1/entries/{dna_entry}/revisions/1").status == 404
    outsider.context.close()


def test_a_component_shows_many_entities_in_each_of_its_places(open_as: Any, world: World) -> None:
    """A tab, the grid shown another way, a project's section and a person's home, each
    given what its search finds as the person may read it, paged and narrowed only as the
    page allows (ADR 0117)."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    editor = world.users["editor"]
    dna = entities.create(
        editor, "acme/dna.DNASequence", world.project, {"bases": "ATGGCC", "name": "abl.dna"}
    ).entity
    dna_hrid, dna_entry = Entity.objects.values_list("hrid", "entry_id").get(pk=dna.pk)
    made = [
        entities.create(
            editor,
            "acme/protein.Protein",
            world.project,
            {
                "sequence": sequence,
                "name": name,
                **({"encoded_by": dna_entry} if dna_of else {}),
            },
        ).entity
        for sequence, name, dna_of in (
            ("MKVL", "ABL1", True),
            ("MKAAG", "ABL2", True),
            ("MSTN", "SRC", False),
        )
    ]
    abl1, abl2, src = (Entity.objects.get(pk=e.pk).hrid for e in made)

    # An entity's tab: what its search finds about the DNA.
    page = open_as("viewer", f"/entities/{dna_hrid}/")
    page.get_by_role("link", name="Proteins it encodes").click()
    tab = page.frame_locator('iframe[title="Proteins it encodes"]')
    tab.get_by_text("Proteins it encodes: 2 shown").wait_for()
    tab.get_by_text(f"{abl1} ABL1 (4 aa, {dna_hrid})").wait_for()
    tab.get_by_text(f"{abl2} ABL2 (5 aa, {dna_hrid})").wait_for()
    assert tab.get_by_text(src).count() == 0
    assert_clean(page)

    # Narrowed as it asks, on a field it reads; cleared, it's all it was given again.
    tab.get_by_role("textbox", name="Name starts with").fill("ABL2")
    tab.get_by_role("button", name="Filter").click()
    tab.get_by_text("Proteins it encodes: 1 shown").wait_for()
    tab.get_by_text(f"{abl2} ABL2").wait_for()
    # It can't widen it: a refinement on a field it doesn't read is refused by the page.
    (frame,) = [f for f in page.frames if "/plugin-components/acme/protein.board/" in f.url]
    frame.evaluate(
        """() => document.querySelector("protein-board").dispatchEvent(new CustomEvent(
            "iki-refine",
            { bubbles: true, detail: { where: { field: "lims_id", op: "is_set", value: false } } },
        ))"""
    )
    tab.get_by_role("textbox", name="Name starts with").fill("")
    tab.get_by_role("button", name="Filter").click()
    tab.get_by_text("Proteins it encodes: 2 shown").wait_for()

    # Picking one offers the run page; opening one is Iki's navigation, only to what it shows.
    tab.get_by_role("button", name=f"Select {abl1}").click()
    page.get_by_role("status").filter(has_text="1 protein selected").wait_for()
    assert_clean(page)
    frame.evaluate(
        f"""() => document.querySelector("protein-board").dispatchEvent(new CustomEvent(
            "iki-open", {{ bubbles: true, detail: {{ entity: "{src}" }} }},
        ))"""
    )
    tab.get_by_role("button", name=f"Open {abl2}").click()
    page.wait_for_url(re.compile(rf"/entities/{abl2}/(\?project=kinase-atlas)?$"))
    page.get_by_role("heading", level=1, name=re.compile("ABL2")).wait_for()

    # The grid's results, shown as the board: the same search, narrowed by the grid's text.
    # Each place in a page of its own, closed once it's checked.
    page.context.close()
    page = open_as("viewer", "/projects/kinase-atlas/entities/?type=acme/protein.Protein&q=ABL")
    page.get_by_role("table").wait_for()
    page.get_by_role("combobox", name="Show as").select_option("acme/protein.board")
    board = page.frame_locator('iframe[title="Board"]')
    board.get_by_text("Board: 2 shown").wait_for()
    assert page.get_by_role("table").count() == 0
    assert_clean(page)

    # A section of the project's page, of everything there.
    page.context.close()
    page = open_as("viewer", "/projects/kinase-atlas/")
    section = page.get_by_role("region", name="Protein board")
    section.frame_locator("iframe").get_by_text("Protein board: 3 shown").wait_for()
    assert_clean(page)

    # A person's home: what they added. The viewer added nothing, so has no section.
    page.context.close()
    page = open_as("editor", "/")
    home = page.get_by_role("region", name="Proteins you added")
    home.frame_locator("iframe").get_by_text("Proteins you added: 3 shown").wait_for()
    assert_clean(page)
    page.context.close()
    page = open_as("viewer", "/")
    page.get_by_role("heading", level=1).wait_for()
    quiet()
    assert page.get_by_role("region", name="Proteins you added").count() == 0

    # An app's panel, beside Iki's own: what's picked in it, the panel after it reads.
    spec = {
        "version": 1,
        "panels": [
            {
                "id": "board",
                "title": "Kinases",
                "component": "acme/protein.board",
                "props": {
                    "type": "acme/protein.Protein",
                    "where": {"field": "name", "op": "starts_with", "value": "ABL"},
                },
            },
            {
                "id": "picked",
                "component": "iki.text",
                "props": {"text": "{{ size(board.selection) }} picked."},
            },
        ],
    }
    made_app = entities.create(
        editor, "iki/core.App", world.project, {"name": "Kinase board", "spec": spec}
    ).entity
    page.context.close()
    page = open_as("viewer", f"/entities/{Entity.objects.get(pk=made_app.pk).hrid}/")
    panel = page.get_by_role("region", name="Kinases")
    shown = panel.frame_locator("iframe")
    shown.get_by_text("Kinases: 2 shown").wait_for()
    page.get_by_text("0 picked.").wait_for()
    shown.get_by_role("button", name=f"Select {abl2}").click()
    page.get_by_text("1 picked.").wait_for()
    assert_clean(page)


def test_the_audit_log_reads_in_the_viewers_time_zone_under_the_apps_sidebar(
    open_as: Any, world: World
) -> None:
    """An auditor in California opens the log from the web app's sidebar, through Admin:
    the server's sidebar offers what the app's did, its times are in their zone and say so,
    and the date the app showed a project's creation on finds it."""
    from datetime import UTC, datetime

    from iki_server.audit import log as audit
    from iki_server.audit.models import AuditEvent

    AuditEvent.objects.create(
        occurred_at=datetime(2026, 11, 21, 3, 10, tzinfo=UTC),  # 19:10 on the 20th there
        action="project.created",
        actor=world.users["admin"],
        actor_label="admin@example.com",
        via="web",
        target_type="project",
        target_label="Compute Capacity Study",
    )
    audit.record(
        "quota.updated",
        actor=world.users["compute_admin"],
        data={
            "before": {"tier": "backend", "max_running": 1, "max_queued": 4},
            "after": {"tier": "backend", "max_running": 2, "max_queued": 5},
        },
    )
    page = open_as("auditor", "/", timezone_id="America/Los_Angeles")

    nav = page.get_by_role("navigation", name="Main")

    def sidebar() -> list[str]:
        links: list[str] = nav.get_by_role("link").all_inner_texts()
        return links

    # Admin is one link, to the admin area, which opens on the first page an auditor may.
    in_the_app = sidebar()
    # Entities is closed away from its own pages.
    assert in_the_app[:3] == ["My work", "Inbox", "Requests"]
    assert in_the_app[-3:] == ["Types", "People and teams", "Admin"]
    nav.get_by_role("link", name="Admin").click()
    page.wait_for_url("**/admin/costs/")
    # The admin area (ADR 0176): its own sidebar offers what an auditor may open, and the
    # main sidebar isn't drawn there.
    admin = page.get_by_role("navigation", name="Admin")
    admin.get_by_role("link", name="Audit log").click()
    page.get_by_role("heading", name="Audit log", level=1).wait_for()
    assert page.get_by_role("navigation", name="Main").count() == 0
    admin = page.get_by_role("navigation", name="Admin")
    assert admin.get_by_role("list").get_by_role("link").all_inner_texts() == [
        "Costs and budgets",
        "Audit log",
        "Electronic signatures",
        "Legal holds",
    ]
    assert admin.get_by_role("link", name="Audit log").get_attribute("aria-current") == "page"
    page.get_by_text("20 Nov 2026, 19:10:00 PST").wait_for()
    page.get_by_text("Times are in America/Los_Angeles").wait_for()
    # What the quota change changed, before and after, under the quota form's labels.
    quota = page.locator("li", has_text="Changed a run quota")
    quota.get_by_text("Details", exact=True).click()
    quota.get_by_role("row", name="Jobs running at once 1 2").wait_for()
    quota.get_by_role("row", name="Runs waiting 4 5").wait_for()
    assert_clean(page)

    page.get_by_text("Filter", exact=True).click()
    page.get_by_role("combobox", name="What").select_option(label="Created a project")
    page.locator("input[name=since]").fill("2026-11-20")
    page.locator("input[name=until]").fill("2026-11-20")
    page.get_by_role("button", name="Apply").click()
    page.wait_for_url("**tz=America%2FLos_Angeles**")
    page.get_by_text("Created a project: Compute Capacity Study").wait_for()
    assert_clean(page)
    # Back to Iki: the main sidebar, as it was.
    page.get_by_role("navigation", name="Admin").get_by_role("link", name="Back to Iki").click()
    nav.wait_for()
    assert sidebar() == in_the_app


def test_org_admins_who_arent_members_see_how_to_join(open_as: Any) -> None:
    page = open_as("org_admin", "/projects/kinase-atlas/")
    page.get_by_text("You’re not a member of this project").wait_for()
    assert_clean(page)


def test_pressing_the_new_project_icon_while_saved_views_arrive(open_as: Any) -> None:
    page = open_as("team_member", "/")
    deferred: list[Route] = []
    saved_views = "**/api/v1/saved-searches?*"
    page.route(saved_views, lambda route: deferred.append(route))
    with page.expect_request(saved_views):
        page.reload()

    sidebar = page.locator("iki-sidebar")
    add = sidebar.get_by_role("link", name="New project", exact=True)
    # Capture the native down target on the link: its icon has a separate hit-tested path.
    add.evaluate("""link => link.addEventListener("pointerdown", event => {
        window.pressedNewProjectPath = event.target.matches("svg path");
    }, {once: true, capture: true})""")
    add.locator("svg path").hover()
    page.mouse.down()
    assert page.evaluate("window.pressedNewProjectPath") is True
    # The empty saved-views result refreshes the sidebar without moving the plus.
    before = sidebar.evaluate_handle("sidebar => sidebar.data")
    assert len(deferred) == 1
    with page.expect_response(saved_views) as response:
        deferred[0].continue_()
    assert response.value.json() == []
    page.wait_for_function(
        "before => document.querySelector('iki-sidebar').data !== before", arg=before
    )
    page.mouse.up()
    page.wait_for_url("**/projects/new/")
    page.get_by_role("textbox", name="Project name").wait_for()


def test_creating_a_project(open_as: Any, world: World) -> None:
    page = open_as("team_member", "/")
    page.get_by_role("link", name="New project").first.click()
    page.get_by_role("heading", name="New project", level=1).wait_for()
    # Navigating moves focus to the new page's heading, as a page load would. WebKit moves
    # it a moment after the heading appears, so wait for it.
    page.wait_for_function("() => document.activeElement?.hasAttribute('data-page-title')")

    page.get_by_role("button", name="Create project").click()
    name = page.get_by_role("textbox", name="Project name")
    assert name.get_attribute("aria-invalid") == "true"
    page.wait_for_function("id => document.activeElement?.id === id", arg=name.get_attribute("id"))
    assert_clean(page)

    name.fill("Fragment Screen")
    page.get_by_role("button", name="Create project").click()
    page.wait_for_url("**/projects/fragment-screen/")
    page.get_by_text("Created Fragment Screen. You’re its Admin.").wait_for()
    project = Project.objects.get(slug="fragment-screen")
    assert project.memberships.get(user=world.users["team_member"]).role == ProjectRole.ADMIN
    assert_clean(page)


def test_one_id_in_two_projects_each_with_its_own_record(open_as: Any, world: World) -> None:
    """Registering what another project has gives this one a record of the same registry
    entry; its ID then asks which project's record, and the entry's page lists only the
    records the person reads (ADR 0115)."""
    from iki_server.entities import services as entities

    admin = world.users["admin"]
    antigens = Project.objects.create(team=world.team, name="Antigens")
    ProjectMembership.objects.create(project=antigens, user=admin, role=ProjectRole.EDITOR)
    first: Any = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MKVLAAGG", "name": "ABL1"}
    )
    again: Any = entities.create(
        admin, "acme/protein.Protein", antigens, {"sequence": "MKVLAAGG", "name": "ABL1 antigen"}
    )
    assert again.registered is False and again.entity.entry_id == first.entity.entry_id
    hrid = first.entity.hrid

    page = open_as("admin", f"/entities/{hrid}/")
    page.get_by_role("heading", name="Which project’s record?").wait_for()
    # The API answers which with a 409, which Chromium and WebKit log as a failed load:
    # that, and nothing else.
    PAGE_ERRORS[page][:] = [e for e in PAGE_ERRORS[page] if "status of 409 (Conflict)" not in e]
    assert_clean(page)
    page.get_by_role("main").get_by_role("link", name="Antigens", exact=True).click()
    page.wait_for_url(re.compile(rf"/entities/{hrid}/\?project=antigens$"))
    page.get_by_role("heading", name=f"Registry entry {hrid}").wait_for()
    page.get_by_role("heading", name="Project values").wait_for()
    page.get_by_text("ABL1 antigen").first.wait_for()
    assert_clean(page)

    page.get_by_role("link", name="Open the entry").click()
    page.get_by_role("heading", level=1, name=hrid).wait_for()
    records = page.locator("section", has=page.get_by_role("heading", name="Records you can read"))
    records.get_by_role("link", name=re.compile("Antigens")).wait_for()
    records.get_by_role("link", name=re.compile("Kinase Atlas")).wait_for()
    assert_clean(page)

    # Someone in only one of them reads its record, and nothing of the other's.
    viewer = open_as("viewer", f"/entities/{hrid}/")
    viewer.get_by_role("heading", name="Project values").wait_for()
    assert viewer.get_by_text("ABL1 antigen").count() == 0
    assert_clean(viewer)


def test_a_registrar_supersedes_an_entry_and_a_project_moves_its_record(
    open_as: Any, world: World
) -> None:
    """A registrar replaces a mistaken entry; the project with it is told on its record's
    page, and an Editor moves the record to the right ID (ADR 0115)."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.org import registries
    from iki_server.org.models import Registry, RegistryRole

    admin = world.users["admin"]
    antigens = Project.objects.create(team=world.team, name="Antigens")
    ProjectMembership.objects.create(project=antigens, user=admin, role=ProjectRole.EDITOR)
    wrong: Any = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MKVLAAGG"}
    )
    right: Any = entities.create(admin, "acme/protein.Protein", antigens, {"sequence": "MKVLAAGH"})
    main = Registry.default()
    registries.grant_role(
        main, RegistryRole.Role.REGISTRAR, world.users["team_member"], by=world.users["org_admin"]
    )
    old, new = wrong.entity.hrid, right.entity.hrid

    page = open_as("team_member", f"/registries/{main.pk}/entries/")
    page.get_by_role("link", name=old, exact=True).wait_for()
    assert_clean(page)
    page.get_by_role("link", name=old, exact=True).click()
    page.get_by_role("heading", level=1, name=old).wait_for()
    assert_clean(page)
    page.get_by_role("textbox", name="Replaced by").fill(new)
    page.get_by_role("textbox", name="Reason").last.fill("Registered with a typo")
    page.get_by_role("button", name=f"Supersede {old}…").click()
    dialog = page.get_by_role("alertdialog", name=f"Supersede {old} with {new}?")
    assert_clean(page)
    dialog.get_by_role("button", name=f"Supersede {old}").click()
    page.get_by_text(f"Superseded {old}. Every project with it has been told.").wait_for()
    assert_clean(page)

    record = open_as("admin", f"/entities/{old}/?project=kinase-atlas")
    record.get_by_role("heading", name=f"{old} was superseded by {new}").wait_for()
    assert_clean(record)
    record.get_by_role("button", name=f"Move to {new}").click()
    moving = record.get_by_role("alertdialog", name=f"Move to {new}?")
    moving.get_by_role("textbox", name="Reason").fill("The registrar superseded it")
    assert_clean(record)
    moving.get_by_role("button", name=f"Move to {new}").click()
    record.wait_for_url(re.compile(rf"/entities/{new}/\?project=kinase-atlas$"))
    record.get_by_role("heading", name=f"Registry entry {new}").wait_for()
    assert_clean(record)
    assert Entity.objects.get(pk=wrong.entity.pk).entry_id == right.entity.entry_id


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_count_made_while_someone_draws_is_recounted_not_recorded(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """A stock count is checked against the balance its counter saw (its revision, as
    If-Match).
    When someone draws meanwhile, nothing is recorded: the dialog says who drew what, keeps
    the count, selected, and records it against the new balance only when confirmed again."""
    from decimal import Decimal

    from iki_server.amounts import services as amounts
    from iki_server.amounts.models import Amount, Movement
    from iki_server.entities import services as entities

    made: Any = entities.create(
        world.users["admin"],
        "acme/protein.Sample",
        world.project,
        {"label": "S-1"},
        amount={"value": 100, "unit": "uL"},
    )
    sample = made.entity
    seen = f'"{Amount.objects.get(entity=sample).revision}"'
    page = open_as("admin", f"/entities/{sample.hrid}/", color_scheme)
    volume = page.get_by_role("region", name="Volume")
    volume.get_by_role("button", name="Adjust").click()
    dialog = page.get_by_role("alertdialog", name=f"Adjust {sample.hrid}’s volume")
    expect(dialog).to_contain_text("It had 100 uL when this count began.")
    count = dialog.get_by_role("textbox", name="It has")
    count.fill("95")
    dialog.get_by_role("textbox", name="Reason").fill("Monthly count")
    assert_clean(page)

    amounts.draw(
        world.users["editor"],
        Entity.objects.get(pk=sample.pk),
        amounts.Given(Decimal(20), "uL"),
        reason="Western blot",
    )
    with page.expect_response(lambda r: r.url.endswith("/amount/adjust")) as refused:
        dialog.get_by_role("button", name="Adjust").click()
    assert refused.value.status == 412, refused.value.text()
    assert refused.value.request.headers["if-match"] == seen
    stale = dialog.get_by_role("alert")
    expect(stale).to_contain_text(
        "Your count wasn’t recorded: the volume changed after you started counting."
    )
    expect(stale).to_contain_text("It has 80 uL now, not the 100 uL you started from.")
    since = stale.get_by_role("list", name="Changed since you started")
    expect(since.get_by_role("listitem")).to_have_count(1)
    expect(since.get_by_role("listitem")).to_contain_text(
        re.compile(r"^Drawn: \u221220 uL, by Editor, .+ \(“Western blot”\)\.$")
    )
    # The count is now against the balance shown, kept and selected, to keep or recount.
    expect(dialog).to_contain_text("It had 80 uL when this count began.")
    expect(count).to_be_focused()
    expect(count).to_have_value("95")
    # Chromium and WebKit log the refusal's 412 as a failed load: that, and nothing else.
    PAGE_ERRORS[page][:] = [e for e in PAGE_ERRORS[page] if "status of 412" not in e]
    assert_clean(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth <= 1")
    assert_clean(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    assert Amount.objects.get(entity=sample).value == Decimal(80)

    count.fill("78")
    with page.expect_response(lambda r: r.url.endswith("/amount/adjust")) as recorded:
        dialog.get_by_role("button", name="Adjust").click()
    assert recorded.value.status == 200, recorded.value.text()
    # From the balance it was refused at: the draw's revision.
    recounted_from = Amount.objects.get(entity=sample).revision - 1
    assert recorded.value.request.headers["if-match"] == f'"{recounted_from}"'
    page.get_by_text(f"{sample.hrid} has 78 uL now.").wait_for()
    assert_clean(page)
    assert Amount.objects.get(entity=sample).value == Decimal(78)
    assert list(
        Movement.objects.filter(entity=sample).order_by("created_at").values_list("kind", "reason")
    ) == [("recorded", ""), ("drawn", "Western blot"), ("adjusted", "Monthly count")]


def test_an_org_admin_adds_a_registry_and_a_new_project_goes_in_it(
    open_as: Any, world: World
) -> None:
    """A registry of a client's own, its registrar, and a project made in it (ADR 0115)."""
    page = open_as("org_admin", "/admin/registries/")
    page.get_by_role("textbox", name="Name").fill("Client A")
    page.get_by_role("checkbox", name="Every team's projects may be in it").check()
    page.get_by_role("button", name="Add registry").click()
    page.get_by_role("heading", level=1, name="Client A").wait_for()
    assert_clean(page)
    page.get_by_role("textbox", name="Registrar's email address").fill(
        world.users["team_member"].email
    )
    page.get_by_role("button", name="Appoint registrar").click()
    remove = page.get_by_role("button", name="Remove Team Member as a registrar")
    remove.wait_for()
    assert_clean(page)
    # And a list's curator, a list at a time (ADR 0270).
    page.get_by_role("combobox", name="List").select_option(label="Purification methods")
    page.get_by_role("textbox", name="Curator's email address").fill(world.users["editor"].email)
    page.get_by_role("button", name="Appoint curator").click()
    remove = page.get_by_role("button", name="Remove Editor as a curator of Purification methods")
    remove.wait_for()
    assert_clean(page)

    made = open_as("team_member", "/projects/new/")
    made.get_by_role("textbox", name="Project name").fill("Client A Screen")
    made.get_by_role("combobox", name="Registry").select_option(label="Client A")
    assert_clean(made)
    made.get_by_role("button", name="Create project").click()
    made.wait_for_url("**/projects/client-a-screen/")
    assert Project.objects.get(slug="client-a-screen").registry.name == "Client A"


def test_an_admin_gives_a_member_lab_roles(open_as: Any, world: World) -> None:
    """Lab roles beside a member's role (ADR 0116), chosen in a dialog that says what each
    is for. By role, never by label (Firefox; see above)."""
    from iki_server.org import permissions

    page = open_as("admin", "/projects/kinase-atlas/members/")
    row = page.get_by_role("listitem").filter(has_text="viewer@example.com")
    row.get_by_role("button", name="Lab roles for Viewer").click()
    dialog = page.get_by_role("dialog", name="Lab roles for Viewer")
    qa = dialog.get_by_role("checkbox", name="QA", exact=True)
    qa.wait_for()
    # What needs it, of each type that names it: here, the requests' types, the rules tests'
    # batches, then these.
    needs = dialog.get_by_text("Needed to approve “Weigh” in Release, then weigh; ")
    needs = needs.filter(
        has_text="; to set Status to Submitted, Delivered or Declined on assay requests; to "
        "set Review to Released on assay results; "
    )
    needs.filter(
        has_text="to set Status to Open or Released on lots; to sign “QA approval” to set Status "
        "to Approved on lots; to sign “Responsibility” to set Status to Released on lots; to set "
        "Status to In testing or Released on samples; to set Custody to In the lab, Quarantined "
        "or Disposed on samples; to record draws from samples as “QA sampling”."
    ).wait_for()
    assert_clean(page)
    qa.check()
    dialog.get_by_role("checkbox", name="Reviewer", exact=True).check()
    dialog.get_by_role("button", name="Save").click()
    page.get_by_text("Viewer’s lab roles are now QA, Reviewer.").wait_for()
    row.get_by_text("Lab roles: QA, Reviewer").wait_for()
    viewer = world.users["viewer"]
    assert permissions.lab_roles(viewer, world.project) == {
        "acme/protein.qa",
        "acme/protein.reviewer",
    }
    assert_clean(page)
    page.context.close()


def test_a_record_s_state_fields_are_set_by_their_lab_roles(open_as: Any, world: World) -> None:
    """A sample's state fields (ADR 0125): a control for each, the states its reader may set
    and why not of the others, a reason where a state asks for one, and the lock a state
    puts on it."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    editor = world.users["editor"]
    grant = world.project.memberships.get(user=editor)
    for role in ("acme/protein.analyst", "acme/protein.reviewer"):
        LabRoleGrant.objects.create(membership=grant, role=role, granted_by=world.users["admin"])
    permissions.forget(editor)
    made = entities.create(editor, "acme/protein.Sample", world.project, {"label": "Lot 7"})
    hrid = Entity.objects.get(pk=made.entity.pk).hrid

    page: Page = open_as("editor", f"/entities/{hrid}/")
    heading = page.get_by_role("heading", name="States")
    heading.wait_for()
    card = page.locator("section", has=heading)
    card.get_by_text("Status: Received").wait_for()
    # Custody's states are QA's to set: its control says so.
    custody = card.get_by_role("button", name="Set Custody")
    assert custody.get_attribute("aria-disabled") == "true"
    card.get_by_text("It needs someone with the QA role in").wait_for()
    assert_clean(page)

    card.get_by_role("button", name="Set Status").click()
    dialog = page.get_by_role("alertdialog", name=f"Set Status of {hrid}")
    assert dialog.get_by_role("radio", name=re.compile("^In testing")).is_checked()
    released = dialog.get_by_role("radio", name=re.compile("^Released"))
    assert released.is_disabled()
    dialog.get_by_text("It needs someone with the QA role in").first.wait_for()
    assert_clean(page)
    dialog.get_by_role("textbox", name=re.compile("^Reason")).fill("Bench 4")
    dialog.get_by_role("button", name="Set Status").click()
    page.get_by_text(f"{hrid}’s Status is In testing now.").wait_for()
    card.get_by_text("Status: In testing").wait_for()

    # A state that locks it: its other values can't change, but its states can.
    card.get_by_role("button", name="Set Status").click()
    dialog = page.get_by_role("alertdialog", name=f"Set Status of {hrid}")
    dialog.get_by_role("radio", name=re.compile("^Reviewed")).check()
    dialog.get_by_role("button", name="Set Status").click()
    card.get_by_text("Locked, as its Status is Reviewed").wait_for()
    assert page.get_by_role("link", name="Edit", exact=True).count() == 0
    assert card.get_by_role("button", name="Set Status").get_attribute("aria-disabled") is None
    assert_clean(page)
    page.get_by_role("link", name=re.compile("^Audit trail")).click()
    page.get_by_text("Status set to Reviewed, from In testing by Editor").wait_for()
    page.get_by_text("“Bench 4”").wait_for()
    assert_clean(page)
    page.context.close()


def test_a_state_is_set_from_the_revision_the_reviewer_saw(open_as: Any, world: World) -> None:
    """A colleague relabels a sample while its Set Status dialog is open (ADR 0300): Iki
    refuses the old revision, the dialog says it changed and the page shows their change,
    the reason stays, and confirming again sets it from the revision now shown. Cards and
    dialogs by role, never by label (Firefox; see above)."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    editor = world.users["editor"]
    grant = world.project.memberships.get(user=editor)
    LabRoleGrant.objects.create(
        membership=grant, role="acme/protein.analyst", granted_by=world.users["admin"]
    )
    permissions.forget(editor)
    made = entities.create(editor, "acme/protein.Sample", world.project, {"label": "Lot 7"})
    record = Entity.objects.get(pk=made.entity.pk)

    page: Page = open_as("editor", f"/entities/{record.hrid}/")
    heading = page.get_by_role("heading", name="States")
    heading.wait_for()
    card = page.locator("section", has=heading)
    card.get_by_role("button", name="Set Status").click()
    dialog = page.get_by_role("alertdialog", name=f"Set Status of {record.hrid}")
    dialog.get_by_role("textbox", name=re.compile("^Reason")).fill("Bench 4")
    entities.update(
        world.users["admin"],
        record,
        {"label": "Lot 7 (relabelled)"},
        expected_revision=Entity.objects.get(pk=record.pk).revision,
    )
    dialog.get_by_role("button", name="Set Status").click()
    dialog.get_by_text("Someone changed it while this was open.").wait_for()
    page.get_by_text("Lot 7 (relabelled)").first.wait_for()
    expect(dialog.get_by_role("textbox", name=re.compile("^Reason"))).to_have_value("Bench 4")
    assert (
        entities.current_values(entities.specific(Entity.objects.get(pk=record.pk)))["status"]
        == "received"
    )
    # Chromium and WebKit log the refusal's 412 as a failed load: that, and nothing else.
    PAGE_ERRORS[page][:] = [
        e for e in PAGE_ERRORS[page] if "status of 412 (Precondition Failed)" not in e
    ]
    assert_clean(page)

    dialog.get_by_role("button", name="Set Status").click()
    page.get_by_text(f"{record.hrid}’s Status is In testing now.").wait_for()
    now = Entity.objects.get(pk=record.pk)
    values = entities.current_values(entities.specific(now))
    assert (values["label"], values["status"]) == ("Lot 7 (relabelled)", "in_testing")
    page.context.close()


def test_an_open_edit_says_its_record_was_locked_and_keeps_what_was_typed(
    open_as: Any, world: World
) -> None:
    """A reviewer locks a sample while an analyst has its edit form open (ADR 0300): the
    form says so as soon as the page reads it again, who set which state and when, keeps
    what was typed to copy, and offers no save, so nothing is sent to be refused."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.entities.states import set_state

    _lab_roles(world, editor=("analyst",), admin=("reviewer",))
    editor = world.users["editor"]
    made = entities.create(editor, "acme/protein.Sample", world.project, {"label": "Lot 7"})
    record = Entity.objects.get(pk=made.entity.pk)

    page: Page = open_as("editor", f"/entities/{record.hrid}/edit/")
    label = page.get_by_role("textbox", name=re.compile("^Label"))
    label.fill("Lot 7, re-aliquoted")
    sent: list[str] = []
    page.on("request", lambda r: sent.append(r.url) if r.method == "PATCH" else None)

    tested = set_state(editor, record, "status", "in_testing", expected_revision=record.revision)
    review = set_state(editor, record, "status", "in_review", expected_revision=tested.number)
    set_state(
        world.users["admin"],
        record,
        "status",
        "reviewed",
        expected_revision=review.number,
        reason="Purity in spec",
    )
    # The tab comes back into view, as when the analyst returns to it: the page reads it.
    page.evaluate("window.dispatchEvent(new Event('visibilitychange'))")

    notice = page.get_by_role("alert")
    expect(notice).to_contain_text(
        f"{record.hrid} was locked while you were editing, so nothing can be saved."
    )
    expect(notice).to_contain_text("Admin set its Status to Reviewed")
    expect(notice).to_contain_text("What you typed is still below, to copy.")
    expect(label).to_have_value("Lot 7, re-aliquoted")
    save = page.get_by_role("button", name="Save changes")
    expect(save).to_have_attribute("aria-disabled", "true")
    save.click(force=True)
    assert_clean(page)
    assert sent == []
    now = entities.current_values(entities.specific(Entity.objects.get(pk=record.pk)))
    assert now["label"] == "Lot 7"
    page.context.close()


def test_request_page_indicates_a_sample_the_service_cannot_read(
    open_as: Any, world: World
) -> None:
    from test_requests import build_lab, request_with_private_sample

    made, _, private = request_with_private_sample(build_lab(world))
    page: Page = open_as("tech", f"/entities/{made['hrid']}/")
    expect(page.get_by_text("An entity you can’t read", exact=True).first).to_be_visible()
    assert private["id"] not in page.content() and private["hrid"] not in page.content()
    assert_clean(page)
    page.context.close()

    page = open_as("editor", f"/entities/{made['hrid']}/")
    expect(page.get_by_role("link", name=private["hrid"]).first).to_be_visible()
    assert_clean(page)
    page.context.close()


def test_a_core_takes_requests_a_project_sends_one_and_the_core_works_it(
    open_as: Any, world: World
) -> None:
    """Requests between projects (ADR 0132): a core facility's Admin chooses what it takes,
    a project's Editor sends it an assay request with a sample, for good, and the core's
    technician finds it in its queue and changes only the core's fields. Each card by its
    heading, and by role, never by label (Firefox; see above)."""
    from iki_server.entities import services as entities
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    core = Project.objects.create(team=world.team, name="Protein Core", description="Assays")
    for name, role in (("core_admin", ProjectRole.ADMIN), ("tech", ProjectRole.EDITOR)):
        user = User.objects.create_user(f"{name}@example.com", name.replace("_", " ").title())
        world.users[name] = user
        ProjectMembership.objects.create(project=core, user=user, role=role)
    editor = world.users["editor"]
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=editor),
        role="acme/protein.analyst",
        granted_by=world.users["admin"],
    )
    permissions.forget(editor)
    lot = entities.create(editor, "acme/protein.Sample", world.project, {"label": "Lot 7"})
    lot_hrid = Entity.objects.get(pk=lot.entity.pk).hrid
    made = entities.create(
        editor,
        "acme/protein.AssayRequest",
        world.project,
        {"title": "Purity of the kinases", "samples": [str(lot.entity.pk)]},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid

    # The core's Admin takes assay requests from every project in its registry.
    page: Page = open_as("core_admin", f"/projects/{core.slug}/settings/")
    heading = page.get_by_role("heading", name="Requests it takes")
    heading.wait_for()
    card = page.locator("section", has=heading)
    card.get_by_role("checkbox", name="Assay request", exact=True).check()
    card.get_by_role("radio", name=re.compile("^Every project in the")).check()
    assert_clean(page)
    card.get_by_role("button", name="Save").click()
    page.get_by_text(re.compile("^Protein Core takes assay requests from every project")).wait_for()
    page.context.close()

    # The asking project's Editor sends it, with its sample.
    page = open_as("editor", f"/entities/{hrid}/")
    heading = page.get_by_role("heading", name="Request", exact=True)
    heading.wait_for()
    card = page.locator("section", has=heading)
    card.get_by_text("Not sent yet").wait_for()
    card.get_by_role("link", name=lot_hrid).wait_for()
    page.get_by_role("heading", name="The service’s values").wait_for()
    assert_clean(page)
    card.get_by_role("button", name="Send to a service…").click()
    dialog = page.get_by_role("alertdialog", name=f"Send {hrid} to a service")
    assert dialog.get_by_role("radio", name=re.compile("^Protein Core")).is_checked()
    dialog.get_by_text("It can’t be taken back").wait_for()
    assert_clean(page)
    dialog.get_by_role("textbox", name=re.compile("^Reason")).fill("Q4 batch")
    dialog.get_by_role("button", name="Send to Protein Core").click()
    page.get_by_text(f"Sent {hrid} to Protein Core.").wait_for()
    card.get_by_role("list", name="What it sent").get_by_role("link", name=lot_hrid).wait_for()
    assert card.get_by_role("button", name="Send to a service…").count() == 0
    page.get_by_role("heading", name="Protein Core’s values").wait_for()
    assert_clean(page)
    page.context.close()

    # The core's technician finds it in the core's queue, its Requests tab, with what it sent.
    page = open_as("tech", f"/projects/{core.slug}/")
    heading = page.get_by_role("heading", name="Requests", exact=True)
    heading.wait_for()
    assert_clean(page)
    page.locator("section", has=heading).get_by_role(
        "link", name="Requests sent to Protein Core"
    ).click()
    tabs = page.get_by_role("navigation", name="Project")
    tab = tabs.get_by_role("link", name=re.compile(r"^Requests\b"))
    tab.wait_for()
    assert tab.get_attribute("aria-current") == "page"
    assert tabs.get_by_role("link", name="Entities").get_attribute("aria-current") is None
    grid = page.get_by_role("table", name="1 assay request", exact=True)
    grid.get_by_role("link", name=hrid).wait_for()
    assert_clean(page)
    # By the project that asked: the core can't open it, but it's named.
    chip_editor = add_filter(page, "Asked by")
    chip_editor.get_by_role("combobox", name="Filter 1: value").select_option(label="Kinase Atlas")
    chip_editor.get_by_role("button", name="Done").click()
    grid = page.get_by_role("table", name="1 assay request match")
    grid.get_by_role("link", name=hrid).wait_for()
    assert_clean(page)
    grid.get_by_role("link", name=hrid).click()
    heading = page.get_by_role("heading", name="Request", exact=True)
    heading.wait_for()
    card = page.locator("section", has=heading)
    card.get_by_role("list", name="What it sent").get_by_role("link", name=lot_hrid).wait_for()
    assert_clean(page)
    # It edits the core's fields alone.
    page.get_by_role("link", name="Edit", exact=True).click()
    page.get_by_role("combobox", name=re.compile("^Priority")).select_option("urgent")
    assert page.get_by_role("textbox", name=re.compile("^Title")).count() == 0
    page.get_by_text("are Kinase Atlas’s: its Editors change them.").wait_for()
    assert_clean(page)
    page.get_by_role("button", name="Save changes").click()
    page.get_by_text(f"Saved revision 2 of {hrid}.").wait_for()
    # What it sent opens for the core: it can read it now.
    card.get_by_role("list", name="What it sent").get_by_role("link", name=lot_hrid).click()
    page.get_by_role("heading", name=lot_hrid, level=1).wait_for()
    page.context.close()


def test_a_project_follows_the_requests_it_sent_in_its_requests_tab(
    open_as: Any, world: World
) -> None:
    """The asking side of requests (ADR 0132): a project's Requests tab lists the requests it
    sent, each with its service and status, filtered by service. By role, never by label
    (Firefox; see above)."""
    from iki_server.entities import requests
    from iki_server.entities import services as entities

    core = Project.objects.create(team=world.team, name="Protein Core", description="Assays")
    editor = world.users["editor"]
    requests.set_intake(
        core,
        types=["acme/protein.AssayRequest"],
        from_registry=True,
        sources=[],
        by=world.users["admin"],
    )
    sent = entities.create(
        editor, "acme/protein.AssayRequest", world.project, {"title": "Purity of the kinases"}
    )
    request = Entity.objects.get(pk=sent.entity.pk)
    requests.submit(editor, request, core, expected_revision=request.revision)
    draft = entities.create(
        editor, "acme/protein.AssayRequest", world.project, {"title": "Not sent yet"}
    )
    draft_hrid = Entity.objects.get(pk=draft.entity.pk).hrid

    page: Page = open_as("editor", f"/projects/{world.project.slug}/")
    tabs = page.get_by_role("navigation", name="Project")
    tabs.get_by_role("link", name=re.compile(r"^Requests\b")).click()
    tab = tabs.get_by_role("link", name=re.compile(r"^Requests\b"))
    page.wait_for_url(re.compile(r"/requests/"))
    assert tab.get_attribute("aria-current") == "page"
    # It sends two types of request: its assay requests, by the type they are.
    page.get_by_role("combobox", name="Type").select_option(label="Assay request")
    # Only what it sent: not the request it hasn't sent yet.
    grid = page.get_by_role("table", name="1 assay request", exact=True)
    row = grid.get_by_role("row").filter(has=page.get_by_role("link", name=request.hrid))
    row.get_by_role("cell", name="Protein Core", exact=True).wait_for()
    row.get_by_role("cell", name="Submitted", exact=True).wait_for()
    assert grid.get_by_role("link", name=draft_hrid).count() == 0
    page.get_by_text(f"The requests {world.project.name} sent to services.").wait_for()
    assert_clean(page)

    chip_editor = add_filter(page, "Sent to")
    chip_editor.get_by_role("combobox", name="Filter 1: value").select_option(label="Protein Core")
    chip_editor.get_by_role("button", name="Done").click()
    grid = page.get_by_role("table", name="1 assay request match")
    grid.get_by_role("link", name=request.hrid).wait_for()
    assert_clean(page)
    page.context.close()


def test_a_core_sorts_its_queue_by_its_ranking_then_priority_then_need(
    open_as: Any, world: World
) -> None:
    """A service orders its own work (ADR 0232): its queue sorts by a service field of its
    own, then by priority in its declared order, urgent first, then by when each is needed.
    By role, never by label (Firefox; see above)."""
    from iki_server.entities import requests
    from iki_server.entities import services as entities

    core = Project.objects.create(team=world.team, name="Protein Core", description="Assays")
    tech = User.objects.create_user("tech@example.com", "Tech")
    world.users["tech"] = tech
    ProjectMembership.objects.create(project=core, user=tech, role=ProjectRole.EDITOR)
    editor = world.users["editor"]
    requests.set_intake(
        core,
        types=["acme/protein.RankedAssayRequest"],
        from_registry=True,
        sources=[],
        by=world.users["admin"],
    )
    hrids = {}
    for title, rank, priority, needed_by in (
        ("Intact mass, lot 7", 2, "urgent", "2026-12-04"),
        ("SEC purity, lot 9", 1, "routine", "2026-12-01"),
        ("Peptide map, lot 3", 1, "urgent", "2026-12-09"),
        ("Endotoxin, lot 5", 1, "urgent", "2026-12-02"),
    ):
        made = entities.create(
            editor,
            "acme/protein.RankedAssayRequest",
            world.project,
            {"title": title, "needed_by": needed_by},
        )
        request = Entity.objects.get(pk=made.entity.pk)
        requests.submit(editor, request, core, expected_revision=request.revision)
        request.refresh_from_db()
        entities.update(
            tech,
            request,
            {"client_rank": rank, "priority": priority},
            expected_revision=request.revision,
            acting_in=core,
        )
        hrids[title] = request.hrid

    page: Page = open_as("tech", f"/projects/{core.slug}/requests/")
    grid = page.get_by_role("table", name="4 ranked assay requests", exact=True)
    grid.wait_for()
    page.get_by_role("button", name=re.compile("^Sort: ")).click()
    page.get_by_role("menuitemradio", name="Client rank, lowest first").click()
    page.get_by_role("button", name="Then by…").click()
    page.get_by_role("menuitem", name="Priority, urgent first").click()
    page.get_by_role("button", name="Then by…").click()
    page.get_by_role("menuitem", name="Needed by, oldest first").click()
    page.get_by_role("button", name="then Needed by, oldest first").wait_for()
    assert_clean(page)
    expected = [
        hrids["Endotoxin, lot 5"],
        hrids["Peptide map, lot 3"],
        hrids["SEC purity, lot 9"],
        hrids["Intact mass, lot 7"],
    ]

    def order() -> list[str]:
        links = grid.get_by_role("row").get_by_role("link", name=re.compile("^RAR-"))
        return [links.nth(i).inner_text() for i in range(links.count())]

    deadline = time.monotonic() + 10
    while order() != expected and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert order() == expected
    page.context.close()


BATCH = "acme/protein.Batch"


def _lab_roles(world: World, **given: tuple[str, ...]) -> None:
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    for who, roles in given.items():
        grant = world.project.memberships.get(user=world.users[who])
        for role in roles:
            LabRoleGrant.objects.create(
                membership=grant, role=f"acme/protein.{role}", granted_by=world.users["admin"]
            )
        permissions.forget(world.users[who])


@pytest.mark.wasm
def test_a_rule_holds_a_record_and_someone_else_reviews_it(open_as: Any, world: World) -> None:
    """A rule that holds (ADR 0127): a result outside its specification is saved with why,
    and held, its record says so, and someone else with the rule's lab role reviews it. By
    role, never by label, and the hold's card by its heading (Firefox; see above)."""
    _lab_roles(world, editor=("analyst", "qa"), admin=("qa",))
    page: Page = open_as("editor", f"/projects/kinase-atlas/entities/new/?type={BATCH}")
    page.get_by_role("heading", level=2, name="New batch").wait_for()
    page.get_by_role("textbox", name="Name", exact=True).fill("L-7")
    page.get_by_role("textbox", name=re.compile("^Purity")).fill("82.5")
    # The form checks the rule as it's typed, with iki-wasm: it holds, so it asks why.
    why = page.get_by_role("textbox", name=re.compile("^Why save it anyway"))
    why.wait_for()
    page.get_by_text("It breaks “Purity in specification”, which holds it for review.").wait_for()
    page.get_by_role("button", name="Create batch").click()
    page.get_by_text(
        "Say why it’s saved though it breaks these rules.", exact=True
    ).first.wait_for()
    assert why.get_attribute("aria-invalid") == "true"
    assert_clean(page)
    why.fill("As measured on the SEC; retest booked.")
    page.get_by_role("button", name="Create batch").click()
    page.wait_for_url(re.compile(r"/entities/BTC-\d+/"))
    hrid = Entity.objects.get(type=BATCH).hrid

    heading = page.get_by_role("heading", name="Held for review")
    heading.wait_for()
    held = page.locator("section", has=heading)
    held.get_by_text("Purity in specification", exact=True).wait_for()
    held.get_by_text("“As measured on the SEC; retest booked.”", exact=False).wait_for()
    # Whoever saved it doesn't review it, though they're QA too: the button says why.
    review = held.get_by_role("button", name="Review")
    assert review.get_attribute("aria-disabled") == "true"
    held.get_by_text("Someone else reviews it: you saved what held it.").wait_for()
    assert_clean(page)
    page.context.close()

    page = open_as("admin", f"/entities/{hrid}/")
    heading = page.get_by_role("heading", name="Held for review")
    heading.wait_for()
    page.locator("section", has=heading).get_by_role("button", name="Review").click()
    dialog = page.get_by_role("alertdialog", name=f"Review the hold on {hrid}")
    dialog.get_by_text(re.compile("^As one of its QA reviewers")).wait_for()
    dialog.get_by_role("button", name="Clear the hold").click()
    dialog.get_by_text("Say why it may stand as it is.", exact=True).wait_for()
    assert_clean(page)
    dialog.get_by_role("textbox", name=re.compile("^Why it may stand")).fill(
        "Investigated: the SEC column was failing."
    )
    dialog.get_by_role("button", name="Clear the hold").click()
    page.get_by_text(f"The hold on {hrid} (“Purity in specification”) is reviewed.").wait_for()
    heading.wait_for(state="detached")
    assert_clean(page)
    page.context.close()


def test_the_grid_finds_what_rules_caught_and_says_why(open_as: Any, world: World) -> None:
    """Search filters for what rules caught (ADR 0135): `is:held` in the search box, and a
    rule's filter in the panel, with the Rules column saying which caught each record."""
    from django.db import connection

    from iki_server.entities import breaches, registry
    from iki_server.entities import services as entities
    from iki_server.entities.models import RulesChecked

    editor = world.users["editor"]
    entities.create(
        editor, BATCH, world.project, {"name": "Held", "purity": 82.5}, hold_reason="As measured."
    )
    broken = entities.create(
        editor,
        BATCH,
        world.project,
        {"name": "Broken", "made_on": "2026-11-01", "expires_on": "2026-12-01"},
    )
    entities.create(editor, BATCH, world.project, {"name": "Fine", "purity": 97.0})
    # As if saved before its rule: straight to its table, then a deploy's check.
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{registry.model(BATCH)._meta.db_table}" SET expires_on = %s '  # noqa: S608
            "WHERE entity_id = %s",
            ["2026-10-01", broken.entity.pk],
        )
    RulesChecked.objects.all().delete()
    breaches.recheck()

    page: Page = open_as("viewer", f"/projects/kinase-atlas/entities/?type={BATCH}")
    page.get_by_role("table", name="3 batches").wait_for()
    assert page.get_by_role("columnheader", name="Rules").count() == 0
    page.get_by_role("searchbox", name="Search").fill("is:held")
    grid = page.get_by_role("table", name="1 of 3 batches")
    grid.get_by_role("columnheader", name="Rules").wait_for()
    grid.get_by_role(
        "cell", name=re.compile("^Held for review: Purity in specification")
    ).wait_for()
    assert_clean(page)

    page.get_by_role("searchbox", name="Search").fill("")
    page.get_by_role("table", name="3 batches").wait_for()
    chip_editor = add_filter(page, "Breaks")
    chip_editor.get_by_role("combobox", name="Filter 1: condition").select_option(label="the rule")
    chip_editor.get_by_role("combobox", name="Filter 1: value").select_option(
        label="Expires after it's made"
    )
    chip_editor.get_by_role("button", name="Done").click()
    grid = page.get_by_role("table", name="1 of 3 batches")
    grid.get_by_role("cell", name=re.compile("^Breaks: Expires after it's made")).wait_for()
    assert_clean(page)
    # The address keeps it, and the condition reads back.
    page.reload()
    page.get_by_role("table", name="1 of 3 batches").wait_for()
    page.get_by_role("button", name=re.compile(r"^Filter 1: Breaks")).click()
    condition = page.get_by_role("combobox", name="Filter 1: condition")
    assert condition.locator("option:checked").inner_text() == "the rule"
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_type_s_page_draws_how_its_records_move(open_as: Any, color_scheme: str) -> None:
    """A type's flow (ADR 0199): a batch's states, who moves a record into each and what
    they sign, where an edit returns it, and the same in words with its guards' messages.
    Its states are buttons, reached by Tab and the arrow keys; choosing one lights its
    arrows. Side by side at 1440px, one above another at 390px, with no sideways scroll.
    By role, never by label (Firefox; see above)."""
    page: Page = open_as("admin", f"/types/{BATCH}/", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    section = page.locator("section", has=page.get_by_role("heading", name="How records move"))
    states = section.get_by_role("list", name="Status’s states", exact=True)
    states.wait_for()
    assert states.get_by_role("button").count() == 5
    # Side by side once the page is wide: it lays out again as its width changes.
    named = page.get_by_role("list", name="Status’s states", exact=True)
    section.locator(".flow-row", has=named).wait_for()
    # Each state's name is what it shows, its marks in words.
    approved = states.get_by_role(
        "button", name=re.compile(r"^Approved\s*,\s*locks it\s*\.\s*Set by Reviewer$")
    )
    approved.wait_for()
    states.get_by_role(
        "button",
        name=re.compile(
            r"^Certified\s*\.\s*Set by Reviewer or QA · from any state\s*,\s*"
            r"signed:\s*Review and Approval$"
        ),
    ).wait_for()
    words = section.get_by_role("list", name="Status’s states in words")
    words.get_by_text("A batch is approved from In review.").wait_for()
    words.get_by_text(
        "Reviewer sets it from In review. It locks the record: its values can’t change. "
        "Correcting a value returns it to In testing, with a reason."
    ).wait_for()
    assert_clean(page)

    first = states.get_by_role("button").first
    first.focus()
    page.keyboard.press("ArrowRight")
    focused = page.evaluate("document.activeElement.textContent")
    assert focused.startswith("In review."), focused
    approved.press("Enter")
    assert approved.get_attribute("aria-pressed") == "true"
    lit = page.locator(".flow-edge.is-lit")
    assert sorted(lit.evaluate_all("els => els.map(e => e.dataset.edge)")) == [
        "approved~in_testing",
        "in_review>approved",
    ]

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    section.locator(".flow-column", has=named).wait_for()
    assert_clean(page)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_record_shows_where_it_is_in_its_flow(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """A record's states (ADR 0199): asked, each state field shows its flow, the record's
    state marked as now, and what the reader may move it to next. Compact until asked, and
    no wider than a phone at 390px. By role (Firefox; see above)."""
    from iki_server.entities import services as entities

    _lab_roles(world, editor=("analyst",))
    made = entities.create(world.users["editor"], BATCH, world.project, {"name": "L-9"})
    hrid = cast("Entity", made.entity).hrid

    page: Page = open_as("editor", f"/entities/{hrid}/", color_scheme)
    card = page.locator("section", has=page.get_by_role("heading", name="States"))
    card.get_by_text("Status: In testing").wait_for()
    assert card.get_by_role("list", name="Status’s states", exact=True).count() == 0
    toggle = card.get_by_role("button", name="How it moves").first
    assert toggle.get_attribute("aria-expanded") == "false"
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    flow = card.get_by_role("list", name="Status’s states", exact=True)
    flow.wait_for()
    card.get_by_text("Next for you: In review.").wait_for()
    now = flow.locator("[aria-current='step']")
    assert (now.text_content() or "").startswith("In testing, where records start, Now")
    flow.get_by_role(
        "button",
        name=re.compile(r"^In review\s*,\s*You can\s*\.\s*Set by Analyst · from any state$"),
    ).wait_for()
    assert_clean(page)

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    toggle.click()
    assert card.get_by_role("list", name="Status’s states", exact=True).count() == 0
    page.context.close()


#: The flows the dose-response starter's results follow: plate assays' own (the app's
#: flow-fixtures.json, which test_flow_fixtures keeps what the endpoint serves).
FLOW_FIXTURES = Path(__file__).parents[2] / "web" / "app" / "src" / "flow-fixtures.json"
PLATE = "iki/plate_assays."


def _starter_api(page: Page) -> dict[str, Any]:
    """Answers the starter API as a deployment with plate assays installed would (ADR 0198):
    this one's fixture plugins don't include them. This stubs the API for the screen's sake
    and is not its coverage: what the endpoints themselves do, including role grants,
    is tested against the real server in test_starter_applications, test_starter_api and
    test_dose_response_starter. Members, projects and everything else here are the real
    server's. Like the server, it applies only the plan previewed (ADR 0207): a grant put in
    ``held`` is one the member holds already, and taking it out changes the plan. Returns
    what the page sent, and ``held``."""
    import hashlib
    import json

    fixtures = json.loads(FLOW_FIXTURES.read_text())
    roles = {
        PLATE + "analyst": ("Assay analyst", "editor"),
        PLATE + "reviewer": ("Assay reviewer", "viewer"),
        PLATE + "qa": ("Assay QA", "viewer"),
    }
    flows = {"research": "ResearchAssayResult", "regulated_qc": "QCAssayResult"}
    summaries = {
        "research": "A result is recorded, then one reviewer signs it off. For discovery work, "
        "where one check is enough.",
        "regulated_qc": "The analyst signs the submission, an independent reviewer signs the "
        "review, and QA signs the release: three different people, each signing in again. "
        "For GxP and QC work.",
    }
    state: dict[str, Any] = {"applied": None, "sent": [], "held": set()}

    def starters() -> dict[str, Any]:
        required = list(roles)
        items = [
            {
                "id": "dose-response",
                "version": "2.0.0",
                "label": "Dose-response plate assay",
                "description": "Choose the review flow for new dose-response fits and assign "
                "the assay roles.",
                "governs": "dose-response fits",
                "flows": [
                    {
                        "id": flow,
                        "label": "Research" if flow == "research" else "Regulated QC",
                        "result_type": f"{PLATE}{base}",
                        "required_roles": required if flow == "regulated_qc" else required[:2],
                        "independent_roles": required if flow == "regulated_qc" else [],
                        "summary": summaries[flow],
                        "states": None,
                    }
                    for flow, base in flows.items()
                ],
                "roles": [
                    {"id": r, "label": label, "minimum_project_role": minimum}
                    for r, (label, minimum) in roles.items()
                ],
                "types": [],
                "applied": state["applied"],
            }
        ]
        return {"items": items, "next_cursor": None}

    def flow_of(route: Any) -> None:
        name = urllib.parse.unquote(route.request.url.split("/entity-types/")[1]).removesuffix(
            "/flow"
        )
        route.fulfill(json=next(t for t in fixtures["types"] if t["type"] == name))

    def lab_roles(route: Any) -> None:
        served = route.fetch().json()
        if served["next_cursor"] is None:
            # The last page of the installed roles (ADR 0291) is where the starter's go.
            served["items"] += [
                {
                    "name": r,
                    "label": label,
                    "description": None,
                    "plugin": r[: r.index(".")],
                    "uses": [],
                }
                for r, (label, _) in roles.items()
            ]
        route.fulfill(json=served)

    def setup(route: Any) -> None:
        sent = route.request.post_data_json
        state["sent"].append((route.request.url.rsplit("/", 1)[1], sent))
        applying = route.request.url.endswith("/apply")
        reviewed = sent.get("plan")
        body = {k: v for k, v in sent.items() if k != "plan"}
        grants = {
            role: missing
            for role, members in body["role_assignments"].items()
            if (missing := [m for m in members if (role, m) not in state["held"]])
        }
        applied = {"version": body["version"], "flow": body["flow"]}
        plan = {
            **body,
            "starter": "dose-response",
            "result_type": f"{PLATE}{flows[body['flow']]}",
            "changed": applied != state["applied"] or bool(grants),
            "grants_to_add": grants,
        }
        shown = {**plan, "before": state["applied"]}
        plan["plan"] = hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()
        if applying and plan["changed"] and reviewed != plan["plan"]:
            route.fulfill(
                status=409,
                content_type="application/problem+json",
                json={
                    "type": "about:blank",
                    "title": "Conflict",
                    "status": 409,
                    "code": "starter_plan_changed",
                    "detail": "What applying does changed since it was previewed. Review what "
                    "it does now, then apply again.",
                    "plan": plan,
                },
            )
            return
        if applying:
            state["applied"] = applied
            state["held"].update((role, member) for role, ids in grants.items() for member in ids)
        route.fulfill(json=plan)

    page.route(re.compile(r"/api/v1/starters(\?.*)?$"), lambda r: r.fulfill(json=starters()))
    page.route(re.compile(r"/api/v1/entity-types/.*(?:Research|QC)AssayResult/flow$"), flow_of)
    page.route(re.compile(r"/api/v1/lab-roles(\?[^/]*)?$"), lab_roles)
    page.route(re.compile(r"/api/v1/starters/dose-response/(preview|apply)$"), setup)
    page.reload()
    return state


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_an_admin_sets_a_project_up_from_a_starter(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """An Admin previews the Regulated QC flow and assigns three independent signers.
    The screen validates the grants and previews what applying them will change.
    At 1440px and 390px, in both schemes (ADR 0198)."""
    page: Page = open_as("admin", "/projects/kinase-atlas/settings/", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    sent = _starter_api(page)
    page.get_by_role("link", name="Set it up from a starter").click()
    card = page.get_by_role("region", name="Dose-response plate assay")
    card.get_by_text("Not applied here").wait_for()
    card.get_by_role("radio", name=re.compile("^Regulated QC")).check()
    states = card.get_by_role("list", name="Review’s states", exact=True)
    states.get_by_role("button", name=re.compile(r"^QA released")).wait_for()
    assert states.get_by_role("button").count() == 4

    # Who may hold each role: people of their own, active, with a high enough role.
    analysts = card.get_by_role("group", name="Assay analyst")
    assert analysts.get_by_role("checkbox").count() == 3  # Editor, Runner, Admin
    reviewers = card.get_by_role("group", name="Assay reviewer")
    assert reviewers.get_by_role("checkbox").count() == 5  # and Viewer, Mixed
    qa = card.get_by_role("group", name="Assay QA")
    for group in (analysts, reviewers, qa):
        group.get_by_role("checkbox", name="Editor").check()
    card.get_by_role("button", name="Preview").click()
    card.get_by_role("alert").get_by_text(re.compile("^Choose 3 different people")).wait_for()
    assert sent["sent"] == []
    assert_clean(page)

    reviewers.get_by_role("checkbox", name="Editor").uncheck()
    reviewers.get_by_role("checkbox", name="Viewer").check()
    qa.get_by_role("checkbox", name="Editor").uncheck()
    qa.get_by_role("checkbox", name="Admin").check()
    card.get_by_role("button", name="Preview").click()
    plan = card.get_by_role("status", name="What applying will do")
    plan.get_by_text("Viewer becomes Assay reviewer.").wait_for()
    plan.get_by_text("New results follow the Regulated QC flow.").wait_for()
    assert_clean(page)

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)

    card.get_by_role("button", name="Apply").click()
    page.get_by_text("Applied Dose-response plate assay with the Regulated QC flow.").wait_for()
    card.get_by_text("Applied: Regulated QC, version 2.0.0").wait_for()
    assert [what for what, _ in sent["sent"]] == ["preview", "apply"]
    assert set(sent["sent"][0][1]) == {"project", "version", "flow", "role_assignments"}
    assert set(sent["sent"][1][1]) == {*sent["sent"][0][1], "plan"}
    assert sent["sent"][1][1]["role_assignments"][PLATE + "qa"] == [
        str(world.project.memberships.get(user=world.users["admin"]).pk)
    ]
    assert_clean(page)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_starter_whose_plan_changed_is_reviewed_again_before_it_applies(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """Applying commits the plan previewed (ADR 0207). Here another Admin revokes the
    reviewer's grant after the preview, so applying would also grant it: nothing is applied,
    the screen says what changed and shows what applying does now, and applying that
    applies it. At 390px, in both schemes."""
    page: Page = open_as("admin", "/projects/kinase-atlas/settings/starters/", color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    api = _starter_api(page)
    reviewer = str(world.project.memberships.get(user=world.users["viewer"]).pk)
    api["held"].add((PLATE + "reviewer", reviewer))
    card = page.get_by_role("region", name="Dose-response plate assay")
    card.get_by_text("Not applied here").wait_for()
    card.get_by_role("radio", name=re.compile("^Research")).check()
    card.get_by_role("group", name="Assay analyst").get_by_role("checkbox", name="Editor").check()
    card.get_by_role("group", name="Assay reviewer").get_by_role("checkbox", name="Viewer").check()
    card.get_by_role("button", name="Preview").click()
    plan = card.get_by_role("status", name="What applying will do")
    plan.get_by_text("Editor becomes Assay analyst.").wait_for()
    assert plan.get_by_text("Viewer becomes Assay reviewer.").count() == 0

    api["held"].clear()  # another Admin revoked it
    card.get_by_role("button", name="Apply").click()
    note = card.get_by_role("alert")
    note.get_by_text(
        "What applying does changed while you were looking at it.", exact=False
    ).wait_for()
    note.get_by_text("Applying now also: Viewer becomes Assay reviewer.").wait_for()
    plan.get_by_text("Viewer becomes Assay reviewer.").wait_for()
    assert card.get_by_text("Not applied here").count() == 1
    # Chromium and WebKit log the refusal, which the test expects, as a failed load.
    PAGE_ERRORS[page][:] = [e for e in PAGE_ERRORS[page] if "status of 409 (Conflict)" not in e]
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)

    card.get_by_role("button", name="Apply").click()
    page.get_by_text("Applied Dose-response plate assay with the Research flow.").wait_for()
    card.get_by_text("Applied: Research, version 2.0.0").wait_for()
    applies = [body for what, body in api["sent"] if what == "apply"]
    assert len(applies) == 2 and applies[0]["plan"] != applies[1]["plan"]
    assert note.count() == 0
    assert_clean(page)
    page.context.close()


def test_a_member_reads_a_project_s_starters(open_as: Any, world: World) -> None:
    """A project's members read its starters and their flows, drawn, with what's applied,
    and no controls to change them."""
    page: Page = open_as("viewer", "/projects/kinase-atlas/settings/starters/")
    _starter_api(page)
    card = page.get_by_role("region", name="Dose-response plate assay")
    card.get_by_text("Only the project’s Admins set it up. These are its review flows.").wait_for()
    flows = card.get_by_role("list", name="Review’s states", exact=True)
    flows.nth(1).wait_for()
    assert flows.count() == 2
    assert card.get_by_role("radio").count() == 0
    assert card.get_by_role("button", name="Preview").count() == 0
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    page.context.close()


def test_a_fetch_webkit_refuses_a_leaving_page_is_an_error_that_says_so(
    open_as: Any, server: Server, browser_name: str
) -> None:
    """A page's reads stop as a navigation away begins (page-reads.ts). One started after that
    is a page error in WebKit, which refuses it unsent, so no request fails to tell of it:
    the error says what was refused, and that the page was leaving."""
    page: Page = open_as("admin", "/account/apps/")
    page.evaluate(
        """() => addEventListener("pagehide", () => {
          fetch("/api/v1/me").catch(() => undefined);
        })"""
    )
    page.goto("/projects/")
    shown(page)
    if browser_name == "webkit":
        url = f"{server.url}/api/v1/me"
        said = (
            f"WebKit refused a fetch of {url} as the page was being left: Fetch API cannot load"
            f" {url} due to access control checks.\n"
        )
        errors = PAGE_ERRORS[page]
        assert len(errors) == 1 and errors[0].startswith(said), what_happened(page)
        errors.clear()
    assert_clean(page)


def test_webkits_refusals_are_read_from_how_playwright_splits_them() -> None:
    """Playwright splits WebKit's uncaught error at its first colon, the scheme's: a refusal
    with a stack is read from that, one without (a failed CORS check) from the split."""

    class Split:
        def __init__(self, text: str, stack: str) -> None:
            self.name, _, rest = text.partition(":")
            self.message, self.stack = rest[1:], stack

    other = "http://localhost:9/api/v1/me"
    cors = Split(f"Fetch API cannot load {other} due to access control checks.", "")
    assert cors.message == "/localhost:9/api/v1/me due to access control checks."
    assert refused(cors) == other
    left = "Fetch API cannot load http://127.0.0.1:8/a?b=1 due to access control checks."
    assert refused(Split(left, left + "\n    at unknown (http://127.0.0.1:8/x:1:2)")) == (
        "http://127.0.0.1:8/a?b=1"
    )
    assert refused(Split("TypeError: Load failed", "")) is None


def test_a_name_axe_and_a_locator_both_work_out_leaves_the_page_running(open_as: Any) -> None:
    """axe reads an element's ARIA element properties in the page's world, Playwright's
    locators in theirs. In Firefox before 163 the second read crashed the page (see conftest),
    which is why axe once skipped Firefox on some pages and tests avoided finding by label."""
    page: Page = open_as("admin", "/account/apps/")
    page.evaluate(
        """() => {
          // Built node by node: the app's policy takes no HTML strings (Trusted Types).
          const probe = document.createElement("div");
          probe.id = "probe";
          const label = document.createElement("span");
          label.id = "save-label";
          label.textContent = "Save the plate map";
          const button = document.createElement("button");
          button.setAttribute("aria-labelledby", "save-label");
          probe.append(label, button);
          document.body.append(probe);
        }"""
    )
    read = "document.querySelector('#probe button').getAttribute('aria-labelledby')"
    for _ in range(2):
        page.evaluate(AXE.read_text())
        page.evaluate(RUN_AXE, WCAG_22_AA)
        expect(page.get_by_role("button", name="Save the plate map")).to_be_visible()
        assert page.evaluate(read) == "save-label"
    page.evaluate("document.getElementById('probe').remove()")
    assert_clean(page)


def test_leaving_the_starters_as_they_arrive_leaves_no_error(open_as: Any) -> None:
    """A page's reads stop once a navigation away begins, not only at pagehide: WebKit
    refuses a fetch the departing document starts in between, and raised an access-control
    error for it. Here the starters arrive just after the Admin heads elsewhere, and the
    card they draw would ask for the selected result type’s flow."""
    page: Page = open_as("admin", "/projects/kinase-atlas/settings/starters/")
    result_flow = re.compile(r"/api/v1/entity-types/.*plate_assays\.ResearchAssayResult/flow$")
    with page.expect_request(result_flow):
        _starter_api(page)
    card = page.get_by_role("region", name="Dose-response plate assay")
    card.wait_for()
    starters = re.compile(r"/api/v1/starters\?project=")
    elsewhere = re.compile(r"/account/apps/$")
    held: dict[str, Route] = {}

    def hold(name: str) -> Any:
        return lambda route: held.__setitem__(name, route)

    # The starters wait for the test, ahead of the answers _starter_api gives.
    page.route(starters, hold("starters"))
    with page.expect_request(starters):
        page.reload()
    assert not card.is_visible()
    asked: list[str] = []
    page.on("request", lambda r: asked.append(r.url) if result_flow.search(r.url) else None)

    # The next page waits too, so the old one stays while the starters come.
    page.route(elsewhere, hold("elsewhere"))
    with page.expect_request(elsewhere):
        page.evaluate("setTimeout(() => location.assign('/account/apps/'))")
    # Chromium keeps the old page's loads until the next page arrives, so it draws the card
    # from the starters and its effects start the card's reads. Firefox and WebKit cut the
    # old page's loads off as the navigation begins, and a query would try one again after a
    # second's wait: WebKit reports each read begun meanwhile.
    with contextlib.suppress(Exception):
        held["starters"].fallback()
    page.wait_for_timeout(2500)
    held["elsewhere"].continue_()
    page.wait_for_url(elsewhere)
    shown(page)
    assert asked == []
    assert_clean(page)
    page.context.close()


def test_a_guard_refuses_a_state_and_says_why(open_as: Any, world: World) -> None:
    """A guard (ADR 0128): whoever submitted a batch for review doesn't approve it. The
    state's control says so in the guard's words, and won't set it."""
    from iki_server.entities import services as entities

    _lab_roles(world, editor=("analyst", "reviewer"))
    made = entities.create(world.users["editor"], BATCH, world.project, {"name": "L-8"})
    hrid = cast("Entity", made.entity).hrid

    page: Page = open_as("editor", f"/entities/{hrid}/")
    states = page.locator("section", has=page.get_by_role("heading", name="States"))
    states.get_by_text("Status: In testing").wait_for()
    states.get_by_role("button", name="Set Status").click()
    dialog = page.get_by_role("alertdialog", name=f"Set Status of {hrid}")
    # Approved only from In review: the guard's message by the state.
    assert dialog.get_by_role("radio", name=re.compile("^Approved")).is_disabled()
    dialog.get_by_text("A batch is approved from In review.").wait_for()
    dialog.get_by_role("radio", name=re.compile("^In review")).check()
    dialog.get_by_role("button", name="Set Status").click()
    page.get_by_text(f"{hrid}’s Status is In review now.").wait_for()
    states.get_by_text("Status: In review").wait_for()

    states.get_by_role("button", name="Set Status").click()
    dialog = page.get_by_role("alertdialog", name=f"Set Status of {hrid}")
    approved = dialog.get_by_role("radio", name=re.compile("^Approved"))
    assert approved.is_disabled()
    dialog.get_by_text("You submitted L-8 for review, so someone else approves it.").wait_for()
    assert_clean(page)
    page.context.close()


def test_edit_warns_before_returning_an_approved_batch(open_as: Any, world: World) -> None:
    """A correction shows the return from approval before saving the new measurement."""
    from iki_server.entities import services as entities
    from iki_server.entities.states import set_state

    _lab_roles(world, editor=("analyst",), admin=("reviewer",))
    editor = world.users["editor"]
    batch = cast(
        "Entity",
        entities.create(editor, BATCH, world.project, {"name": "Lot 9", "purity": 95}).entity,
    )
    submitted = set_state(editor, batch, "status", "in_review", expected_revision=batch.revision)
    approved = set_state(
        world.users["admin"], batch, "status", "approved", expected_revision=submitted.number
    )

    page: Page = open_as("editor", f"/entities/{batch.hrid}/")
    page.get_by_role("link", name="Edit", exact=True).click()
    page.get_by_role("textbox", name=re.compile("^Purity")).fill("96")
    page.get_by_role("alert").get_by_text(
        "Saving these changes will return Status from Approved to In testing."
    ).wait_for()
    assert Entity.objects.get(pk=batch.pk).revision == approved.number
    assert_clean(page)
    page.get_by_role("textbox", name="Reason", exact=True).fill("Corrected the purity reading.")
    page.get_by_role("button", name="Save changes").click()
    page.get_by_text(f"Saved revision {approved.number + 2} of {batch.hrid}.").wait_for()
    page.get_by_text("Status: In testing", exact=True).wait_for()
    page.context.close()


def test_people_take_work_name_who_has_it_next_and_reassign_it(open_as: Any, world: World) -> None:
    """People in records (ADR 0122): open work taken from My work, a reviewer named with
    the picker, their work once its Status is In review (ADR 0125), and reassigning from
    the grid. By role, never by label, and each card by its heading (Firefox; see above)."""
    from iki_server.entities import services as entities
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    for who, roles in (
        ("editor", ("analyst",)),
        ("admin", ("reviewer",)),
        ("viewer", ("analyst",)),
    ):
        grant = world.project.memberships.get(user=world.users[who])
        for role in roles:
            LabRoleGrant.objects.create(
                membership=grant, role=f"acme/protein.{role}", granted_by=world.users["admin"]
            )
        permissions.forget(world.users[who])
    made = entities.create(
        world.users["admin"], "acme/protein.Sample", world.project, {"label": "Lot 9"}
    )
    hrid = cast("Entity", made.entity).hrid

    # Nobody has it yet: it's open to the editor's Analyst role.
    page = open_as("editor", "/work/?open=true")
    page.get_by_role("list", name="Open work").wait_for()
    assert_clean(page)
    page.get_by_role("button", name=f"Take {hrid} as Analyst").click()
    page.get_by_text(f"{hrid} is yours, as Analyst.").wait_for()
    page.get_by_role("link", name="Assigned to me").click()
    page.get_by_role("list", name="Your work").get_by_role("link", name=hrid, exact=True).click()

    states = page.locator("section", has=page.get_by_role("heading", name="States"))
    states.get_by_text("Status: Received").wait_for()
    # Whoever reviews it is named before it's theirs.
    page.get_by_role("link", name="Edit", exact=True).click()
    reviewer = page.get_by_role("combobox", name="Reviewer")
    reviewer.fill("adm")
    page.get_by_role("option").filter(has_text="admin@example.com").click()
    page.get_by_role("listbox").wait_for(state="hidden")
    assert "(admin@example.com)" in reviewer.input_value()
    assert_clean(page)
    page.get_by_role("button", name="Save changes").click()
    said = page.get_by_text(f"Saved revision 3 of {hrid}.")
    # If it isn't saved, say why: the form's alert (nothing changed, someone saved first).
    with contextlib.suppress(PlaywrightTimeoutError):
        said.wait_for()
    assert said.is_visible(), page.get_by_role("alert").all_inner_texts()
    page.get_by_role("listitem").filter(has_text="Reviewer: Admin").wait_for()
    for state in ("In testing", "In review"):
        states.get_by_role("button", name="Set Status").click()
        dialog = page.get_by_role("alertdialog", name=f"Set Status of {hrid}")
        dialog.get_by_role("radio", name=re.compile(f"^{state}")).check()
        dialog.get_by_role("button", name="Set Status").click()
        page.get_by_text(f"{hrid}’s Status is {state} now.").wait_for()
    states.get_by_text("Status: In review").wait_for()
    page.context.close()

    # It's the admin's now, and the grid finds it by whom it names.
    page = open_as("admin", "/work/")
    page.get_by_role("list", name="Your work").get_by_role("link", name=hrid, exact=True).wait_for()
    assert_clean(page)
    page.goto("/projects/kinase-atlas/entities/?type=acme/protein.Sample")
    page.get_by_role("searchbox", name="Search").fill("assigned:me")
    grid = page.get_by_role("table", name=re.compile(r"^1 of \d+ samples?$"))
    grid.get_by_role("link", name=hrid).wait_for()
    grid.get_by_role("checkbox", name=f"Select {hrid}").check()
    page.get_by_role("button", name="Reassign").click()
    dialog = page.get_by_role("alertdialog", name="Reassign 1 sample")
    dialog.get_by_role("combobox", name="Field").select_option("Analyst")
    dialog.get_by_role("combobox", name="Analyst").fill("view")
    page.get_by_role("option").filter(has_text="viewer@example.com").click()
    page.get_by_role("listbox").wait_for(state="hidden")
    dialog.get_by_role("textbox", name="Reason").fill("Editor is away")
    assert_clean(page)
    dialog.get_by_role("button", name="Reassign").click()
    page.get_by_role("alertdialog", name="Reassigned 1 of 1").wait_for()
    saved = type(made.entity)._default_manager.get(pk=made.entity.pk)
    assert str(entities.current_values(saved)["analyst"]) == str(world.users["viewer"].pk)
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_reassignment_of_a_changed_selection_assigns_nothing_until_refreshed(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """Reassigning sends each picked record's revision as the grid showed it. When one
    changed meanwhile, the server refuses the whole batch (412): the dialog names it, keeps
    what was chosen, and reassigns all of them once the selection is refreshed."""
    from iki_server.entities import services as entities
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    grant = world.project.memberships.get(user=world.users["viewer"])
    LabRoleGrant.objects.create(
        membership=grant, role="acme/protein.analyst", granted_by=world.users["admin"]
    )
    permissions.forget(world.users["viewer"])
    admin = world.users["admin"]
    made = [
        cast(
            "Entity",
            entities.create(admin, "acme/protein.Sample", world.project, {"label": label}).entity,
        )
        for label in ("Lot 11", "Lot 12")
    ]
    page = open_as(
        "admin", "/projects/kinase-atlas/entities/?type=acme/protein.Sample", color_scheme
    )
    grid = page.get_by_role("table", name=re.compile(r"^2 samples$"))
    for record in made:
        grid.get_by_role("checkbox", name=f"Select {record.hrid}").check()
    page.get_by_role("button", name="Reassign").click()
    dialog = page.get_by_role("alertdialog", name="Reassign 2 samples")
    dialog.get_by_role("combobox", name="Field").select_option("Analyst")
    dialog.get_by_role("combobox", name="Analyst").fill("view")
    page.get_by_role("option").filter(has_text="viewer@example.com").click()
    page.get_by_role("listbox").wait_for(state="hidden")
    dialog.get_by_role("textbox", name="Reason").fill("Editor is away")

    changed = made[1]
    entities.update(admin, changed, {"label": "Lot 12b"}, expected_revision=1, reason="Relabelled")
    with page.expect_response(lambda r: r.url.endswith("/entities/assign")) as refused:
        dialog.get_by_role("button", name="Reassign").click()
    assert refused.value.status == 412, refused.value.text()
    assert refused.value.request.post_data_json["expected_revisions"] == {
        str(made[0].pk): 1,
        str(changed.pk): 1,
    }
    alert = dialog.get_by_role("alert")
    expect(alert).to_contain_text(
        "Nothing was reassigned: one of these records changed after you selected it."
    )
    expect(
        alert.get_by_role("list", name="Changed since you selected them").get_by_role("listitem")
    ).to_have_text(f"{changed.hrid}: revision 1 when you selected it, revision 2 now.")
    expect(dialog.get_by_role("button", name="Reassign")).to_have_attribute("aria-disabled", "true")
    for record in made:
        saved = type(record)._default_manager.get(pk=record.pk)
        assert entities.current_values(saved).get("analyst") is None
    # Chromium and WebKit log the refusal's 412 as a failed load: that, and nothing else.
    PAGE_ERRORS[page][:] = [
        e for e in PAGE_ERRORS[page] if "status of 412 (Precondition Failed)" not in e
    ]
    assert_clean(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth <= 1")
    assert_clean(page)
    page.set_viewport_size({"width": 1440, "height": 900})

    alert.get_by_role("button", name="Refresh the selection").click()
    refreshed = dialog.get_by_role("status")
    expect(refreshed).to_have_text(
        "The selection is up to date. Check the record that changed before you reassign: "
        f"{changed.hrid}."
    )
    expect(refreshed).to_be_focused()
    expect(dialog.get_by_role("textbox", name="Reason")).to_have_value("Editor is away")
    assert_clean(page)
    with page.expect_response(lambda r: r.url.endswith("/entities/assign")) as accepted:
        dialog.get_by_role("button", name="Reassign").click()
    assert accepted.value.status == 200, accepted.value.text()
    assert accepted.value.request.post_data_json["expected_revisions"] == {
        str(made[0].pk): 1,
        str(changed.pk): 2,
    }
    page.get_by_role("alertdialog", name="Reassigned 2 of 2").wait_for()
    for record in made:
        saved = type(record)._default_manager.get(pk=record.pk)
        assert str(entities.current_values(saved)["analyst"]) == str(world.users["viewer"].pk)


def test_signing_a_record_signs_in_again_and_shows_the_signature(
    open_as: Any, world: World, fake_idp: Any
) -> None:
    """An electronic signature (ADR 0126): the person agrees before their first, signs in
    again at the identity provider (ADR 0124), and comes back to the record, which shows
    the signature as people read it and checks it again."""
    from allauth.socialaccount.models import SocialAccount
    from test_signatures import LOT, provider, ready

    from iki_server.accounts.models import IdentityProvider
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    IdentityProvider.objects.all().delete()
    SocialAccount.objects.all().delete()
    found = provider(
        fake_idp.issuer,
        client_id=fake_idp.client_id,
        authorization_endpoint=fake_idp.discovery()["authorization_endpoint"],
    )
    found.client_secret = fake_idp.client_secret
    found.save()
    ready(world, found)
    editor = world.users["editor"]
    grant = world.project.memberships.get(user=editor)
    LabRoleGrant.objects.create(
        membership=grant, role="acme/protein.analyst", granted_by=world.users["admin"]
    )
    permissions.forget(editor)
    made = entities.create(editor, LOT, world.project, {"label": "LOT A"})
    hrid = Entity.objects.get(pk=made.entity.pk).hrid

    page: Page = open_as("editor", f"/entities/{hrid}/")
    # At the provider's sign-in page, the editor signs in again.
    fake_idp.browser_claims = {"sub": "uid-editor", "amr": ["pwd", "otp"]}
    heading = page.get_by_role("heading", name="Signatures", exact=True)
    heading.wait_for()
    section = page.locator("section", has=heading)
    section.get_by_role("button", name="Sign: Authorship").click()
    dialog = page.get_by_role("alertdialog", name=f"Sign {hrid}: Authorship")
    dialog.get_by_text("Next, Test SSO asks you to sign in again").wait_for()
    assert_clean(page)
    # Without agreeing first, it says so, before asking Iki.
    dialog.get_by_role("button", name="Sign in again to sign").click()
    dialog.get_by_text("Agree to this before your first signature.").wait_for()
    dialog.get_by_role("checkbox", name="I agree").check()
    dialog.get_by_role("button", name="Sign in again to sign").click()
    page.wait_for_url(re.compile(r"\?signing="))
    page.get_by_text(f"You signed {hrid}. Its Status is In review now.").wait_for()
    states = page.locator("section", has=page.get_by_role("heading", name="States"))
    states.get_by_text("Status: In review").wait_for()
    page.get_by_text(re.compile(r"^Authored by Editor on \d{4}-\d\d-\d\d at ")).wait_for()
    assert_clean(page)
    section.get_by_role("button", name="Check signatures").click()
    section.get_by_text(
        "Its key, what it signed and the record’s history all check out."
    ).wait_for()
    section.get_by_role("button", name="Dismiss").click()
    page.wait_for_url(re.compile(rf"/entities/{hrid}/$"))
    assert fake_idp.authorizations[-1]["prompt"] == "login"
    # Its audit trail shows the revision it set the state in, with the signature.
    page.get_by_role("link", name=re.compile("^Audit trail")).click()
    signed = page.get_by_role("list", name="Signatures of revision 2")
    signed.get_by_text(re.compile(r"^Authored by Editor on ")).wait_for()
    assert_clean(page)
    page.context.close()


def test_dialogs_hold_focus_and_give_it_back(open_as: Any) -> None:
    page = open_as("admin", "/projects/kinase-atlas/members/")
    row = page.get_by_role("listitem").filter(has_text="viewer@example.com")
    remove = row.get_by_role("button", name="Remove")
    remove.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("alertdialog", name="Remove Viewer?")
    dialog.wait_for()
    page.wait_for_function("() => document.activeElement.closest('[role=alertdialog]') !== null")
    assert_clean(page)
    page.keyboard.press("Escape")
    dialog.wait_for(state="detached")
    page.wait_for_function("() => document.activeElement.textContent === 'Remove'")
    assert remove.evaluate("button => button === document.activeElement")


def test_an_ended_session_signs_in_again_and_comes_back(open_as: Any, world: World) -> None:
    page = open_as("admin", "/projects/kinase-atlas/")
    # Once the overview has what it asked for: had a request of it still been going, its
    # answer would send the page to sign in, coming back to the overview.
    page.get_by_text("Your role").wait_for()
    # Its costs, and its plugins' sections (the test plugin's board has one, ADR 0117).
    page.wait_for_function(
        "() => [/\\/costs$/, /^\\/api\\/v1\\/components\\/.+\\/entities$/].every((path) =>"
        " performance.getEntriesByType('resource')"
        ".some((e) => path.test(new URL(e.name).pathname) && e.responseEnd > 0))"
    )
    quiet()
    # The session ends (signed out elsewhere, or deactivated) while the page is open.
    from iki_server.accounts.models import UserSession

    UserSession.objects.filter(user=world.users["admin"]).delete()
    page.get_by_role("link", name="Members").click()
    page.wait_for_url(re.compile(r"/login/\?next="))
    assert "next=%2Fprojects%2Fkinase-atlas%2Fmembers%2F" in page.url


def test_the_apps_files_are_cached_for_ever(open_as: Any) -> None:
    page = open_as("admin", "/projects/")
    script = page.locator('script[type=module][src^="/static/app/assets/main-"]').get_attribute(
        "src"
    )
    assert script and re.fullmatch(r"/static/app/assets/main-[\w-]+\.js", script)

    assets = [script]
    styles = set()
    for link in page.locator("link[rel=stylesheet]").all():
        href = link.get_attribute("href")
        assert href
        name = re.fullmatch(r"/static/iki/([\w-]+)\.[0-9a-f]{12}\.css", href)
        assert name, href
        styles.add(name[1])
        assets.append(href)
    assert {"iki", "react-aria", "tokens", "fonts", "button"} <= styles

    for url in assets:
        response = page.request.get(url)
        assert response.ok, url
        assert "immutable" in response.headers["cache-control"], url


@pytest.mark.wasm
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_an_app_whose_panels_read_the_ones_before(
    open_as: Any, world: World, color_scheme: str
) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    for name, sequence in (("ABL1", "MLEICLKLVG"), ("ABL2", "MLEVCLKLVGAKQ")):
        entities.create(
            admin, "acme/protein.Protein", world.project, {"sequence": sequence, "name": name}
        )
    spec = {
        "version": 1,
        "panels": [
            {
                "id": "kinases",
                "title": "Kinases",
                "width": 5,
                "component": "iki.entities",
                "props": {
                    "type": "acme/protein.Protein",
                    "where": {"all": [{"field": "name", "op": "starts_with", "value": "ABL"}]},
                    "sort": "name",
                    "columns": [
                        {"field": "name"},
                        {"label": "Length", "value": "{{ size(row.values.sequence) }} aa"},
                    ],
                },
            },
            {
                "id": "picked",
                "title": "Picked",
                "width": 7,
                "component": "iki.viewer",
                "props": {"entity": "{{ kinases.selection }}"},
            },
            {
                "id": "summary",
                "component": "iki.text",
                "props": {
                    "text": "{{ size(kinases.selection) }} of {{ size(kinases.rows) }} picked."
                },
            },
        ],
    }
    made = entities.create(admin, "iki/core.App", world.project, {"name": "Kinases", "spec": spec})
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/", color_scheme)
    grid = page.locator(".app-panel", has_text="Kinases")
    grid.get_by_text("13 aa", exact=True).wait_for()
    page.get_by_text("0 of 2 picked.").wait_for()
    # Its expressions ran in iki-wasm's worker, under a policy of its own.
    policy = page.evaluate(WORKER_POLICY)
    assert policy is not None and "script-src 'self' 'wasm-unsafe-eval'" in policy
    assert_clean(page)

    grid.get_by_role("radio", name="Pick ABL1").check()
    page.get_by_text("1 of 2 picked.").wait_for()
    picked = page.locator(".app-panel", has_text="Picked")
    picked.get_by_text("10 residues").wait_for()
    assert picked.get_by_role("link", name="ABL1").count() == 1


@pytest.mark.wasm
def test_an_app_about_an_entity_is_a_tab_on_its_page(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    made = {}
    for name, sequence, organism in (
        ("ABL1", "MLEICLKLVG", "Homo sapiens"),
        ("ABL2", "MLEVCLKLVGAKQ", "Homo sapiens"),
        ("Abl", "MGQQPGKVLG", "Mus musculus"),
    ):
        found = entities.create(
            admin,
            "acme/protein.Protein",
            world.project,
            {"sequence": sequence, "name": name, "host": organism},
        )
        made[name] = Entity.objects.get(pk=found.entity.pk).hrid
    spec = {
        "version": 1,
        "entity": {"type": "acme/protein.Protein"},
        "panels": [
            {
                "id": "length",
                "component": "iki.text",
                "props": {
                    "text": "{{ entity.values.name }} has {{ size(entity.values.sequence) }} "
                    "residues."
                },
            },
            {
                "id": "kin",
                "title": "From the same host",
                "component": "iki.entities",
                "props": {
                    "type": "acme/protein.Protein",
                    "select": "none",
                    "where": {
                        "all": [
                            {
                                "field": "host",
                                "op": "eq",
                                "value": "{{ entity.values.host }}",
                            },
                            {"field": "name", "op": "ne", "value": "{{ entity.values.name }}"},
                        ]
                    },
                    "columns": [{"field": "name"}],
                },
            },
        ],
    }
    entities.create(admin, "iki/core.App", world.project, {"name": "Relatives", "spec": spec})
    page = open_as("admin", f"/entities/{made['ABL1']}/")
    # By CSS and text, not role, until axe has run (Firefox; see above).
    page.locator(".tabs a", has_text="Relatives").click()
    page.get_by_text("ABL1 has 10 residues.").wait_for()
    grid = page.locator(".app-panel", has_text="From the same host")
    grid.get_by_text(made["ABL2"]).wait_for()
    assert grid.get_by_text(made["Abl"]).count() == 0
    assert_clean(page)


def test_a_projects_page_opens_with_its_dashboard(open_as: Any, world: World) -> None:
    from iki_server.entities import dashboards
    from iki_server.entities import services as entities

    admin = world.users["admin"]
    entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
    )
    spec = {
        "version": 1,
        "panels": [
            {
                "id": "proteins",
                "title": "Our proteins",
                "component": "iki.entities",
                "props": {
                    "type": "acme/protein.Protein",
                    "select": "none",
                    "columns": [{"field": "name"}],
                },
            },
            {
                "id": "count",
                "component": "iki.text",
                "props": {"text": "{{ size(proteins.rows) }} so far."},
            },
        ],
    }
    made = entities.create(admin, "iki/core.App", world.project, {"name": "Atlas", "spec": spec})
    dashboards.choose(world.project, str(made.entity.pk), by=admin)
    page = open_as("viewer", f"/projects/{world.project.slug}/")
    # By CSS and text, not role, until axe has run (Firefox; see above).
    shown = page.locator(".dashboard", has_text="Atlas")
    shown.get_by_text("1 so far.").wait_for()
    shown.locator(".app-panel", has_text="Our proteins").get_by_text("ABL1").wait_for()
    assert_clean(page)


def test_a_draft_app_opens_for_its_person_and_previews_as_its_written(
    open_as: Any, world: World
) -> None:
    from iki_server.entities import app_drafts
    from iki_server.entities import services as entities

    editor = world.users["editor"]
    text = {"id": "hi", "component": "iki.text", "props": {"text": "{{ 1 + 1 }} panels."}}
    spec = {"version": 1, "panels": [text]}
    draft = app_drafts.create(editor, world.project, "Greeting", spec)
    page = open_as("editor", f"/projects/{world.project.slug}/apps/drafts/{draft.pk}/")
    page.get_by_text("2 panels.").wait_for()
    page.get_by_text("A draft, not saved.").wait_for()
    assert_clean(page)

    made = entities.create(
        editor, "iki/core.App", world.project, {"name": "Greeting", "spec": spec}
    )
    page.goto(page.url.split("/projects/")[0] + f"/entities/{made.entity.pk}/edit/")
    # By CSS and text, not role, until axe has run (Firefox; see above).
    page.locator(".app-preview button", has_text="Preview").click()
    page.locator(".app-preview").get_by_text("2 panels.").wait_for()
    assert_clean(page)

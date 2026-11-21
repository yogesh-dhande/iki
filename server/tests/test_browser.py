"""The web app in real browsers: Chromium, Firefox and WebKit against the real
server (production settings, collected static files, uvicorn), with the
production Content Security Policy and Trusted Types enforced.

Every page is checked with axe against WCAG 2.2 AA, in light and dark mode,
and must log no errors and break no CSP rule. Needs the built app (``make web``)
and browsers (``uv run playwright install``); without them these tests skip
locally and fail in CI.
"""

import contextlib
import itertools
import os
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import S3_ACCESS_KEY, S3_SECRET_KEY
from django.conf import settings
from django.test import Client
from iki_test_plugins import ensure_built
from org_world import World, build_world
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    sync_playwright,
)
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


def _require(what: bool, message: str) -> None:
    if what:
        return
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="module", autouse=True)
def _orm_beside_playwright() -> Iterator[None]:
    # Sync Playwright keeps an event loop in this thread, so Django assumes the
    # ORM is called from async code. Nothing runs concurrently: allow it here.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
        yield


@pytest.fixture(scope="module")
def server(
    django_db_setup: None,
    django_db_blocker: Any,
    tmp_path_factory: pytest.TempPathFactory,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> Iterator[Server]:
    _require(frontend._manifest_path() is not None, "Build the web app first: make web")
    _require(AXE.is_file(), "Install the web dependencies first: pnpm install")
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
    except Exception as error:
        _require(False, f"{request.param}: {error}. Run: uv run playwright install")
        raise
    yield launched
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


def watch(page: Page) -> Page:
    """Records what goes wrong on the page, from now on: its errors in `PAGE_ERRORS`,
    which `assert_clean` checks, its failed requests in `FAILED_REQUESTS`, and what it's
    still waiting for in `IN_FLIGHT`. `what_happened` says it all when a test fails."""
    errors: list[str] = []
    failed: list[str] = []
    waiting: dict[Any, tuple[float, str]] = {}

    def done(request: Any) -> None:
        waiting.pop(request, None)

    def fail(request: Any) -> None:
        done(request)
        failed.append(f"{request.method} {request.url}: {request.failure}")

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: waiting.__setitem__(r, (time.monotonic(), f"{r.method} {r.url}")))
    page.on("requestfinished", done)
    page.on("requestfailed", fail)

    # A console error says a request failed, but not which: this does.
    def answered(response: Any) -> None:
        if response.status >= 500:
            failed.append(f"{response.request.method} {response.url}: {response.status}")

    page.on("response", answered)
    visited: list[str] = []
    page.on("framenavigated", lambda f: visited.append(f.url) if f == page.main_frame else None)
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
    browser: Browser, server: Server, world: World, request: pytest.FixtureRequest
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


def assert_clean(page: Page, *, axe_in_firefox: bool = True) -> None:
    """No CSP violations, no errors, and nothing axe finds against WCAG 2.2 AA.

    `axe_in_firefox=False` leaves axe to Chromium and WebKit on that page: Playwright's
    Firefox crashes its content process when axe works out names and roles on a page that
    has both a plugin's component frame and an element named by `aria-labelledby`
    (aria-allowed-attr, aria-conditional-attr, aria-prohibited-attr, nested-interactive,
    label-content-name-mismatch and more each do). axe checks the DOM, which is the same
    in every browser.
    """
    assert page.evaluate("window.__cspViolations") == [], page.url
    assert PAGE_ERRORS[page] == [], (page.url, FAILED_REQUESTS.get(page))
    browser = page.context.browser
    if not axe_in_firefox and browser and browser.browser_type.name == "firefox":
        return
    # Mid-fade text has less contrast than it ends up with: check the end state, once it's
    # drawn, and still settled then.
    page.wait_for_function(FADES_DONE)
    page.evaluate(NEXT_FRAMES)
    page.wait_for_function(FADES_DONE)
    page.evaluate(AXE.read_text())
    violations = page.evaluate(RUN_AXE, WCAG_22_AA)
    assert violations == [], (page.url, violations)


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
    ("org_admin", "/settings/email/"),
    ("admin", "/projects/kinase-atlas/webhooks/"),
    ("org_admin", "/settings/webhooks/"),
    ("compute_admin", "/compute/"),
    ("compute_admin", "/compute/workers/"),
    ("compute_admin", "/compute/slurm/"),
    ("compute_admin", "/compute/quotas/"),
    ("org_admin", "/settings/registries/"),
    ("org_admin", "/settings/signatures/"),
]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_pages_are_accessible_and_keep_the_policy(open_as: Any, color_scheme: str) -> None:
    for principal, path in PAGES:
        page = open_as(principal, path, color_scheme)
        assert_clean(page)
        page.context.close()


def test_a_compute_admin_registers_and_changes_self_hosted_machines(
    open_as: Any, world: World
) -> None:
    """A team's machine, its labels and what it's for (ADR 0112). By role, but each
    section by its heading: in Firefox, finding a region by name before axe runs crashes
    the page (see the other tests' notes)."""
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
    page = open_as("compute_admin", "/compute/workers/")
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


def test_set_a_quota_stays_put_as_the_quotas_arrive(open_as: Any) -> None:
    """The button that opens the dialog doesn't move when the list above it is answered.
    It did, a line down, and a press begun while the list loaded ended on the card instead:
    in Firefox and WebKit, Playwright's own click did, and no dialog opened."""
    page = open_as("compute_admin", "/compute/quotas/")
    held: list[Any] = []

    def listing(url: str) -> bool:
        return url.endswith("/api/v1/compute/quotas")

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

    page = open_as("compute_admin", "/compute/quotas/")
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
    page.get_by_role("link", name="Notifications, 1 unread").wait_for()
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
    page.get_by_role("link", name="Notifications, 2 unread").wait_for()
    try:
        page.get_by_role("link", name=title).wait_for()
    except PlaywrightTimeoutError:
        pytest.fail(f"the inbox never showed it; its requests answered {answered}")
    assert_clean(page)

    page.get_by_role("link", name=title).click()
    page.wait_for_url("**/projects/kinase-atlas/")
    page.get_by_role("link", name="Notifications, 1 unread").wait_for()
    assert_clean(page)


def test_webhooks_show_their_deliveries_and_where_they_send(open_as: Any, world: World) -> None:
    webhooks.create(
        project=world.project,
        url="https://eln.example.com/hooks/iki?token=s3cret",
        description="Our ELN",
        chosen=["project.updated"],
        by=world.users["admin"],
    )
    page = open_as("admin", "/projects/kinase-atlas/webhooks/")
    page.get_by_role("link", name="eln.example.com").click()
    page.get_by_role("button", name="Details of ping").click()
    page.get_by_text("Message ID").wait_for()
    assert_clean(page)
    page.context.close()

    # Org Admins see where it sends, not the address (which may carry a token).
    page = open_as("org_admin", "/settings/webhooks/")
    page.get_by_text("eln.example.com").wait_for()
    assert "s3cret" not in page.content()
    assert_clean(page)


def test_entities_from_the_grid_to_their_history(open_as: Any, world: World) -> None:
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
    page.get_by_role("heading", level=1, name=f"ABL1 kinase {entity.hrid}").wait_for()
    page.get_by_text("Changed by").first.wait_for()
    assert_clean(page)
    page.get_by_role("link", name="History").click()
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
    # Playwright’s Firefox crashes in axe on this page (see assert_clean).
    assert_clean(page, axe_in_firefox=False)


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


def test_the_grids_search_box_is_a_field_like_its_neighbours(open_as: Any) -> None:
    page = open_as("admin", "/projects/kinase-atlas/entities/?type=acme/dna.DNASequence")
    search = page.get_by_role("searchbox", name="Search")
    search.wait_for()
    box = search.bounding_box()
    assert box is not None and box["height"] >= 44, box
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
    page.get_by_role("table", name="1 protein match").wait_for()
    page.get_by_role("searchbox", name="Search").fill("")
    page.get_by_role("table", name="3 proteins").wait_for()

    page.get_by_role("button", name="Filters").click()
    panel = page.get_by_role("region", name="Filters")
    panel.get_by_role("button", name="Add a filter").click()
    panel.get_by_label("Filter 1: field").select_option(label="Host")
    panel.get_by_label("Filter 1: condition").select_option(label="is")
    panel.get_by_label("Filter 1: value").fill("Homo sapiens")
    panel.get_by_role("button", name="Add a filter").click()
    panel.get_by_label("Filter 2: field").select_option(label="Temperature optimum")
    panel.get_by_label("Filter 2: condition").select_option(label="is more than")
    panel.get_by_role("textbox", name="Filter 2: value").fill("90")
    panel.get_by_label("Unit for Filter 2: value").select_option("[degF]")
    grid = page.get_by_role("table", name="1 protein match")
    grid.get_by_role("cell", name="ABL1", exact=True).wait_for()
    assert_clean(page)

    # The address keeps it: a reload, or a shared link, shows the same.
    page.reload()
    page.get_by_role("table", name="1 protein match").wait_for()
    assert page.get_by_role("textbox", name="Filter 2: value").input_value() == "90"
    page.get_by_role("button", name="Remove filter 2").click()
    grid = page.get_by_role("table", name="2 proteins match")
    grid.wait_for()

    grid.get_by_role("button", name="Name").click()
    page.wait_for_url(re.compile(r"sort=name"))
    grid.get_by_role("button", name="Name").click()
    page.wait_for_url(re.compile(r"desc=true"))
    first = grid.get_by_role("row").nth(1)
    first.get_by_role("cell", name="SRC", exact=True).wait_for()
    assert grid.get_by_role("columnheader", name="Name").get_attribute("aria-sort") == "descending"
    assert_clean(page)


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
    page.get_by_role("table", name="1 protein match").wait_for()
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
    other.get_by_role("table", name="1 protein match").wait_for()
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
    page.get_by_role("table", name="2 proteins match").wait_for()
    page.get_by_role("button", name="Save as collection").click()
    dialog = page.get_by_role("alertdialog", name="Save as a collection")
    dialog.get_by_label("Name").fill("ABL kinases")
    assert_clean(page)
    dialog.get_by_role("button", name="Save collection").click()
    page.get_by_text(re.compile(r"Saved the collection COL-\d+\.")).wait_for()
    # By its heading: in Firefox, asking for a region by name before axe runs crashes the page.
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

    page.get_by_role("button", name="Archive").click()
    dialog = page.get_by_role("alertdialog", name=f"Archive {made.hrid}?")
    dialog.get_by_role("textbox", name="Reason (optional)").fill("Duplicate of another")
    assert_clean(page)
    dialog.get_by_role("button", name="Archive").click()
    page.get_by_text(f"Archived {made.hrid}.").wait_for()
    assert Entity.objects.get(pk=made.pk).archived_at is not None


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
    existing = structure.get_by_role("combobox", name="Or one already in the project")
    page.wait_for_function("(el) => el.value !== ''", arg=existing.element_handle(), timeout=60_000)
    stored = File.objects.get(pk=existing.input_value())
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

    page.get_by_role("link", name="Kinase Atlas").click()
    row = page.get_by_role("listitem").filter(has_text=run.hrid)
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
    assert page.evaluate("window.__cspViolations") == [] and PAGE_ERRORS[page] == []
    if page.context.browser and page.context.browser.browser_type.name == "firefox":
        # Firefox's content process crashes when axe's aria-allowed-attr check runs on
        # this page straight after the app navigates to it; loaded, it's checked in full.
        page.reload()
        runs.first.wait_for()
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
    # By its heading: in Firefox, Playwright finding a region by name and then axe
    # running crashes the page (Playwright's role engine, not the app).
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
    page.get_by_role("link", name="Lineage").click()
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

    # Its first 24 bases, by pointer: from before the first to after the 24th.
    page.mouse.click(*_place(page, 0, 0))
    page.keyboard.down("Shift")
    page.mouse.click(*_place(page, 0, 24))
    page.keyboard.up("Shift")
    first = bases[:24]
    gc, tm = sequences.gc_content(first), sequences.melting_temperature(first)
    assert (gc, tm) == (62.5, 64.2)
    readout = page.get_by_text(re.compile("^Bases 1–24 selected"))
    readout.wait_for()
    assert readout.text_content() == f"Bases 1–24 selected: 24 bp · GC {gc:g}% · Tm {tm} °C"
    assert_clean(page)
    page.get_by_role("button", name="Copy", exact=True).click()
    page.get_by_role("status").filter(has_text="Copied 24 bases.").wait_for()
    page.get_by_role("button", name="Copy reverse complement").click()
    page.get_by_role("status").filter(has_text="reverse complement").wait_for()
    assert page.evaluate("window.__copied") == [first, sequences.reverse_complement(first)]

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


def _stored_file(world: World, s3_endpoint: str, bucket: str, path: Path, media_type: str) -> Any:
    """A file in the project, its bytes put straight into the store the server reads."""
    import hashlib

    import boto3

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
    blob, _ = Blob.objects.get_or_create(
        sha256=sha256, defaults={"size": len(data), "storage_uri": f"s3://{bucket}/{key}"}
    )
    return File.objects.create(
        blob=blob,
        name=path.name,
        media_type=media_type,
        project=world.project,
        created_by=world.users["admin"],
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
    said = page.locator(".messages").get_by_text(re.compile(r"^moved"))
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
    page.get_by_role("navigation", name="Entity").get_by_role("link", name="Alignment").click()
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
    # By role, not label: Playwright's Firefox crashes finding a label once axe has run.
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
    assert_clean(page, axe_in_firefox=False)

    # A person's home: what they added. The viewer added nothing, so has no section.
    page.context.close()
    page = open_as("editor", "/")
    home = page.get_by_role("region", name="Proteins you added")
    home.frame_locator("iframe").get_by_text("Proteins you added: 3 shown").wait_for()
    assert_clean(page, axe_in_firefox=False)
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
    assert_clean(page, axe_in_firefox=False)


def test_the_audit_log_reads_in_the_viewers_time_zone_under_the_apps_header(
    open_as: Any, world: World
) -> None:
    """An auditor in California opens the log from the web app: its header offers what the
    app's did, its times are in their zone and say so, and the date the app showed a
    project's creation on finds it."""
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

    def header() -> list[str]:
        nav = page.get_by_role("navigation", name="Main")
        links: list[str] = nav.get_by_role("link").all_inner_texts()
        return links

    in_the_app = header()
    assert in_the_app == ["My work", "Projects", "Types", "Teams", "Costs", "Audit"]
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Audit").click()
    page.get_by_role("heading", name="Audit log", level=1).wait_for()
    assert header() == in_the_app
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


def test_org_admins_who_arent_members_see_how_to_join(open_as: Any) -> None:
    page = open_as("org_admin", "/projects/kinase-atlas/")
    page.get_by_text("You’re not a member of this project").wait_for()
    assert_clean(page)


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
    page.get_by_role("link", name="Antigens", exact=True).click()
    page.wait_for_url(re.compile(rf"/entities/{hrid}/\?project=antigens$"))
    page.get_by_role("heading", name=f"Registry entry {hrid}").wait_for()
    page.get_by_role("heading", name="Antigens’s values").wait_for()
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
    viewer.get_by_role("heading", name="Kinase Atlas’s values").wait_for()
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
    from iki_server.org.models import Registry

    admin = world.users["admin"]
    antigens = Project.objects.create(team=world.team, name="Antigens")
    ProjectMembership.objects.create(project=antigens, user=admin, role=ProjectRole.EDITOR)
    wrong: Any = entities.create(
        admin, "acme/protein.Protein", world.project, {"sequence": "MKVLAAGG"}
    )
    right: Any = entities.create(admin, "acme/protein.Protein", antigens, {"sequence": "MKVLAAGH"})
    main = Registry.default()
    registries.appoint(main, world.users["team_member"], by=world.users["org_admin"])
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


def test_an_org_admin_adds_a_registry_and_a_new_project_goes_in_it(
    open_as: Any, world: World
) -> None:
    """A registry of a client's own, its registrar, and a project made in it (ADR 0115)."""
    page = open_as("org_admin", "/settings/registries/")
    page.get_by_role("textbox", name="Name").fill("Client A")
    page.get_by_role("checkbox", name="Every team's projects may be in it").check()
    page.get_by_role("button", name="Add registry").click()
    page.get_by_role("heading", level=1, name="Client A").wait_for()
    assert_clean(page)
    page.get_by_role("textbox", name="Email address").fill(world.users["team_member"].email)
    page.get_by_role("button", name="Appoint").click()
    remove = page.get_by_role("button", name="Remove Team Member as a registrar")
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
        "or Disposed on samples."
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
    puts on it. Its card by its heading: in Firefox, finding a region by name and then
    running axe crashes the page (see above)."""
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
    page.get_by_role("link", name="History").click()
    page.get_by_text("Status set to Reviewed, from In testing by Editor").wait_for()
    page.get_by_text("“Bench 4”").wait_for()
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
    card.get_by_role("checkbox", name="Assay request").check()
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
    tab = tabs.get_by_role("link", name="Requests", exact=True)
    tab.wait_for()
    assert tab.get_attribute("aria-current") == "page"
    assert tabs.get_by_role("link", name="Entities").get_attribute("aria-current") is None
    grid = page.get_by_role("table", name="1 assay request", exact=True)
    grid.get_by_role("link", name=hrid).wait_for()
    assert_clean(page)
    # By the project that asked: the core can't open it, but it's named.
    page.get_by_role("button", name=re.compile("^Filters")).click()
    filters = page.get_by_role("region", name="Filters")
    filters.get_by_role("button", name="Add a filter").click()
    filters.get_by_role("combobox", name="Filter 1: field").select_option(label="Asked by")
    filters.get_by_role("combobox", name="Filter 1: value").select_option(label="Kinase Atlas")
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
    tabs.get_by_role("link", name="Requests", exact=True).click()
    tab = tabs.get_by_role("link", name="Requests", exact=True)
    page.wait_for_url(re.compile(r"/requests/"))
    assert tab.get_attribute("aria-current") == "page"
    # Only what it sent: not the request it hasn't sent yet.
    grid = page.get_by_role("table", name="1 assay request", exact=True)
    row = grid.get_by_role("row").filter(has=page.get_by_role("link", name=request.hrid))
    row.get_by_role("cell", name="Protein Core", exact=True).wait_for()
    row.get_by_role("cell", name="Submitted", exact=True).wait_for()
    assert grid.get_by_role("link", name=draft_hrid).count() == 0
    page.get_by_text(f"The requests {world.project.name} sent to services.").wait_for()
    assert_clean(page)

    page.get_by_role("button", name=re.compile("^Filters")).click()
    filters = page.get_by_role("region", name="Filters")
    filters.get_by_role("button", name="Add a filter").click()
    filters.get_by_role("combobox", name="Filter 1: field").select_option(label="Sent to")
    filters.get_by_role("combobox", name="Filter 1: value").select_option(label="Protein Core")
    grid = page.get_by_role("table", name="1 assay request match")
    grid.get_by_role("link", name=request.hrid).wait_for()
    assert_clean(page)
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
    page.wait_for_url(re.compile(r"/entities/BAT-\d+/"))
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
    grid = page.get_by_role("table", name="1 batch match")
    grid.get_by_role("columnheader", name="Rules").wait_for()
    grid.get_by_role(
        "cell", name=re.compile("^Held for review: Purity in specification")
    ).wait_for()
    assert_clean(page)

    page.get_by_role("searchbox", name="Search").fill("")
    page.get_by_role("table", name="3 batches").wait_for()
    page.get_by_role("button", name="Filters").click()
    panel = page.get_by_role("region", name="Filters")
    panel.get_by_role("button", name="Add a filter").click()
    panel.get_by_role("combobox", name="Filter 1: field").select_option(label="Breaks")
    panel.get_by_role("combobox", name="Filter 1: condition").select_option(label="the rule")
    panel.get_by_role("combobox", name="Filter 1: value").select_option(
        label="Expires after it's made"
    )
    grid = page.get_by_role("table", name="1 batch match")
    grid.get_by_role("cell", name=re.compile("^Breaks: Expires after it's made")).wait_for()
    assert_clean(page)
    # The address keeps it, and the condition reads back.
    page.reload()
    page.get_by_role("table", name="1 batch match").wait_for()
    condition = page.get_by_role("combobox", name="Filter 1: condition")
    assert condition.locator("option:checked").inner_text() == "the rule"
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
    grid = page.get_by_role("table", name="1 sample match")
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
    # Its history shows the revision it set the state in, with the signature.
    page.get_by_role("link", name="History").click()
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
    script = page.locator("script[type=module]").first.get_attribute("src")
    stylesheet, react_aria = (
        link.get_attribute("href") for link in page.locator("link[rel=stylesheet]").all()
    )
    assert script and re.fullmatch(r"/static/app/assets/main-[\w-]+\.js", script)
    assert stylesheet and re.fullmatch(r"/static/iki/iki\.[0-9a-f]{12}\.css", stylesheet)
    assert react_aria and re.fullmatch(r"/static/iki/react-aria\.[0-9a-f]{12}\.css", react_aria)
    for url in (script, stylesheet, react_aria):
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
    # By text, not role, until axe has run: Playwright's Firefox crashes running axe after
    # finding a region by its role.
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

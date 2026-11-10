"""The web app in real browsers: Chromium, Firefox and WebKit against the real
server (production settings, collected static files, uvicorn), with the
production Content Security Policy and Trusted Types enforced.

Every page is checked with axe against WCAG 2.2 AA, in light and dark mode,
and must log no errors and break no CSP rule. Needs the built app (``make web``)
and browsers (``uv run playwright install``); without them these tests skip
locally and fail in CI.
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from iki_test_plugins import ensure_built
from org_world import World, build_world
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from real_server import Server, app_role, manage, serve, server_env

from iki_server.accounts.models import User
from iki_server.notifications import kinds
from iki_server.notifications.services import notify
from iki_server.org.models import Project, ProjectRole
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

# Finite animations (fades) have finished; spinners run for ever.
FADES_DONE = """
() => document.getAnimations().every(
  (a) => a.playState !== "running" || a.effect.getComputedTiming().endTime === Infinity,
)
"""

RUN_AXE = """
async (tags) => {
  const result = await axe.run(document, { runOnly: { type: "tag", values: tags } });
  return result.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    nodes: v.nodes.map((n) => n.target.join(" ")),
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
    django_db_setup: None, django_db_blocker: Any, tmp_path_factory: pytest.TempPathFactory
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
        )
        collected = manage("collectstatic", "--noinput", env=env)
        assert collected.returncode == 0, collected.stdout + collected.stderr
        with serve(env) as running:
            yield running


@pytest.fixture(scope="module")
def playwright() -> Iterator[Playwright]:
    with sync_playwright() as running:
        yield running


@pytest.fixture(scope="module", params=BROWSERS)
def browser(request: pytest.FixtureRequest, playwright: Playwright) -> Iterator[Browser]:
    try:
        launched = getattr(playwright, request.param).launch()
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


def session_cookie(user: User) -> str:
    client = Client()
    client.force_login(user)
    return client.cookies[settings.SESSION_COOKIE_NAME].value


@pytest.fixture
def open_as(browser: Browser, server: Server, world: World) -> Iterator[Any]:
    contexts: list[BrowserContext] = []

    def open_as(principal: str, path: str, color_scheme: str = "light") -> Page:
        context = browser.new_context(base_url=server.url, color_scheme=color_scheme)  # type: ignore[arg-type]
        contexts.append(context)
        user = world.users[principal]
        # The server runs without HTTPS, so its cookie has no __Host- prefix.
        context.add_cookies(
            [{"name": "iki_session", "value": session_cookie(user), "url": server.url}]
        )
        context.add_init_script(WATCH_CSP)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        PAGE_ERRORS[page] = errors
        page.goto(path)
        page.locator("h1").first.wait_for()
        return page

    yield open_as
    for context in contexts:
        context.close()


def assert_clean(page: Page) -> None:
    """No CSP violations, no errors, and nothing axe finds against WCAG 2.2 AA."""
    assert page.evaluate("window.__cspViolations") == [], page.url
    assert PAGE_ERRORS[page] == [], page.url
    # Mid-fade text has less contrast than it ends up with: check the end state.
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
    ("admin", "/account/apps/"),
    ("admin", "/account/notifications/"),
    ("org_admin", "/settings/email/"),
    ("admin", "/projects/kinase-atlas/webhooks/"),
    ("org_admin", "/settings/webhooks/"),
]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_pages_are_accessible_and_keep_the_policy(open_as: Any, color_scheme: str) -> None:
    for principal, path in PAGES:
        page = open_as(principal, path, color_scheme)
        assert_clean(page)
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
    page.get_by_role("link", name=title).wait_for()
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
        admin, "protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
    )
    entity = Entity.objects.get(pk=made.entity.pk)
    entities.update(admin, entity, {"name": "ABL1 kinase"}, expected_revision=1, reason="Full name")
    for path in ("/types/", "/types/protein.Protein/"):
        page = open_as("admin", path)
        assert_clean(page)
        page.context.close()

    page = open_as("admin", "/projects/kinase-atlas/entities/?type=protein.Protein")
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


def test_org_admins_who_arent_members_see_how_to_join(open_as: Any) -> None:
    page = open_as("org_admin", "/projects/kinase-atlas/")
    page.get_by_text("You’re not a member of this project").wait_for()
    assert_clean(page)


def test_creating_a_project(open_as: Any, world: World) -> None:
    page = open_as("team_member", "/")
    page.get_by_role("link", name="New project").first.click()
    page.get_by_role("heading", name="New project", level=1).wait_for()
    # Navigating moves focus to the new page's heading, as a page load would.
    assert page.evaluate("document.activeElement.hasAttribute('data-page-title')")

    page.get_by_role("button", name="Create project").click()
    name = page.get_by_role("textbox", name="Project name")
    assert name.get_attribute("aria-invalid") == "true"
    assert page.evaluate("document.activeElement.id") == name.get_attribute("id")
    assert_clean(page)

    name.fill("Fragment Screen")
    page.get_by_role("button", name="Create project").click()
    page.wait_for_url("**/projects/fragment-screen/")
    page.get_by_text("Created Fragment Screen. You’re its Admin.").wait_for()
    project = Project.objects.get(slug="fragment-screen")
    assert project.memberships.get(user=world.users["team_member"]).role == ProjectRole.ADMIN
    assert_clean(page)


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
    # The session ends (signed out elsewhere, or deactivated) while the page is open.
    from iki_server.accounts.models import UserSession

    UserSession.objects.filter(user=world.users["admin"]).delete()
    page.get_by_role("link", name="Members").click()
    page.wait_for_url(re.compile(r"/login/\?next="))
    assert "next=%2Fprojects%2Fkinase-atlas%2Fmembers%2F" in page.url


def test_the_apps_files_are_cached_for_ever(open_as: Any) -> None:
    page = open_as("admin", "/projects/")
    script = page.locator("script[type=module]").first.get_attribute("src")
    stylesheet = page.locator("link[rel=stylesheet]").get_attribute("href")
    assert script and re.fullmatch(r"/static/app/assets/main-[\w-]+\.js", script)
    assert stylesheet and re.fullmatch(r"/static/iki/iki\.[0-9a-f]{12}\.css", stylesheet)
    for url in (script, stylesheet):
        response = page.request.get(url)
        assert response.ok, url
        assert "immutable" in response.headers["cache-control"], url

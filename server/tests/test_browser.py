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
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import S3_ACCESS_KEY, S3_SECRET_KEY
from django.conf import settings
from django.test import Client
from iki_test_plugins import ensure_built
from org_world import World, build_world
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from real_server import Server, app_role, manage, run_tasks, serve, server_env

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
    django_db_setup: None,
    django_db_blocker: Any,
    tmp_path_factory: pytest.TempPathFactory,
    _s3_endpoint: str,
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
            IKI_FILES_BUCKET="iki",
            IKI_FILES_PREFIX=f"browser-{uuid.uuid4().hex}",
            IKI_FILES_ENDPOINT_URL=_s3_endpoint,
            AWS_ACCESS_KEY_ID=S3_ACCESS_KEY,
            AWS_SECRET_ACCESS_KEY=S3_SECRET_KEY,
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
            "protein.Protein",
            world.project,
            {
                "sequence": sequence,
                "name": name,
                "organism": organism,
                "temperature_optimum": temperature,
            },
        )
    page = open_as("admin", "/projects/kinase-atlas/entities/?type=protein.Protein")
    page.get_by_role("table", name="3 proteins").wait_for()

    page.get_by_role("searchbox", name="Search").fill("ab")
    page.get_by_role("table", name="1 protein match").wait_for()
    page.get_by_role("searchbox", name="Search").fill("")
    page.get_by_role("table", name="3 proteins").wait_for()

    page.get_by_role("button", name="Filters").click()
    panel = page.get_by_role("region", name="Filters")
    panel.get_by_role("button", name="Add a filter").click()
    panel.get_by_label("Filter 1: field").select_option(label="Organism")
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
            "protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    page = open_as("editor", "/projects/kinase-atlas/entities/?type=protein.Protein&q=abl")
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


def test_creating_editing_and_archiving_an_entity(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    gene = Entity.objects.get(
        pk=entities.create(
            world.users["admin"], "dna.DNASequence", world.project, {"bases": "ATG", "name": "abl1"}
        ).entity.pk
    )
    page = open_as("admin", "/projects/kinase-atlas/entities/?type=protein.Protein")
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
    picker.fill("abl")
    page.get_by_role("option", name=re.compile("abl1")).click()
    assert picker.input_value() == f"abl1 ({gene.hrid})"
    page.get_by_role("button", name="Add Uniprot ids").click()
    page.get_by_role("textbox", name="Uniprot ids, item 1").fill("P00519")
    assert_clean(page)
    page.get_by_role("button", name="Create protein").click()
    page.wait_for_url(re.compile(r"/entities/PRT-\d+/$"))
    made = Entity.objects.get(type="protein.Protein")
    page.get_by_text(f"Created {made.hrid}.").wait_for()
    values = entities.current_values_of([made])[made.pk]
    assert values["temperature_optimum"] == pytest.approx(37.0)
    assert values["encoded_by"] == gene.pk and values["uniprot_ids"] == ["P00519"]
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


@pytest.fixture
def tasks(server: Server) -> Iterator[Server]:
    """A task runner beside the server, for work done after the request (checking uploads)."""
    with run_tasks(server.env) as runner:
        yield runner


def test_uploading_a_file_into_a_file_field(open_as: Any, world: World, tasks: Server) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.files.models import File

    protein = entities.create(
        world.users["admin"], "protein.Protein", world.project, {"sequence": "MKVL", "name": "abl1"}
    ).entity
    page = open_as("admin", "/projects/kinase-atlas/entities/new/?type=protein.ProteinStructure")
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
    made = Entity.objects.get(type="protein.ProteinStructure")
    values = entities.current_values_of([made])[made.pk]
    assert values["structure"] == stored.pk and values["chains"] == [protein.pk]
    page.get_by_role("link", name="1abc.pdb").wait_for()
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

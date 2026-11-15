"""The web app in real browsers: Chromium, Firefox and WebKit against the real
server (production settings, collected static files, uvicorn), with the
production Content Security Policy and Trusted Types enforced.

Every page is checked with axe against WCAG 2.2 AA, in light and dark mode,
and must log no errors and break no CSP rule. Needs the built app (``make web``)
and browsers (``uv run playwright install``); without them these tests skip
locally and fail in CI.
"""

import itertools
import os
import re
import time
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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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
def _flush_past_late_requests() -> Iterator[None]:
    """Retries the flush after each test when it deadlocks with a request the server took
    after the pages closed (see `quiet`). Postgres rolls back one side, so trying again
    is safe; without it, the next test starts on this one's rows."""
    from django.db import OperationalError
    from django.test.testcases import TransactionTestCase

    flush = vars(TransactionTestCase)["_fixture_teardown"]

    def retrying(self: TransactionTestCase) -> None:
        for attempt in range(5):
            try:
                flush(self)
            except Exception as error:
                cause = error.__cause__ or error
                if not isinstance(cause, OperationalError) or "deadlock" not in str(cause):
                    raise
                if attempt == 4:
                    raise
                quiet()
            else:
                return

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TransactionTestCase, "_fixture_teardown", retrying)
        yield


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
    # Headless Firefox on Linux turns WebGL off without a GPU; its software renderer
    # draws Mol* well enough to test (Chromium and WebKit use theirs anyway).
    options: dict[str, Any] = (
        {"firefox_user_prefs": {"webgl.force-enabled": True}} if request.param == "firefox" else {}
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
    quiet()


def quiet(timeout: float = 10) -> None:
    """Waits for the server to finish what the pages asked of it. Closing a page doesn't
    stop its requests on the server, and one still in a transaction when the test's
    database is flushed deadlocks with it."""
    from django.db import connection

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
                " AND state <> 'idle'"
            )
            (busy,) = cursor.fetchone()
        if not busy:
            return
        time.sleep(0.05)


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


def test_saving_a_search_as_a_collection(open_as: Any, world: World) -> None:
    from iki_server.entities import services as entities

    for name, sequence in (("ABL1", "MKVL"), ("ABL2", "MKVI"), ("SRC", "MSTL")):
        entities.create(
            world.users["admin"],
            "protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    page = open_as("editor", "/projects/kinase-atlas/entities/?type=protein.Protein&q=abl")
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
        "protein.Protein",
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
            "protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    from iki_server.entities.models import Entity

    made = Entity.objects.get(
        pk=entities.create(
            world.users["admin"],
            "core.Collection",
            world.project,
            {"name": "ABL kinases", "search": {"type": "protein.Protein", "q": "abl"}},
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
def browser_name(browser: Browser) -> str:
    return browser.browser_type.name


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


def test_starting_a_run_and_following_it_live(open_as: Any, world: World) -> None:
    from test_workers import LOCAL, ROOMY

    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.runs import queue, workers
    from iki_server.runs.models import ActionRun

    made = entities.create(
        world.users["admin"], "protein.Protein", world.project, {"sequence": "MKV", "name": "ABL1"}
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
            "protein.Protein",
            world.project,
            {"sequence": sequence, "name": name},
        )
    page = open_as("admin", "/projects/kinase-atlas/entities/?type=protein.Protein")
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
    action = catalog.action("protein.compute_properties")
    assert action is not None
    made = [
        entities.create(admin, "protein.Protein", world.project, {"sequence": s, "name": n})
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
    action = catalog.action("protein.compute_properties")
    assert action is not None
    made = []
    for name, sequence in [("ABL1 precursor", "MKV"), ("ABL1", "MKVL"), ("ABL1 fragment", "KVL")]:
        created = entities.create(
            admin, "protein.Protein", world.project, {"sequence": sequence, "name": name}
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
    graph = page.get_by_role("group", name=re.compile("^3 entities and 2 runs"))
    graph.wait_for()
    assert_clean(page)
    graph.get_by_role("link", name=re.compile("^ABL1 precursor")).hover()
    assert_clean(page)

    page.get_by_role("link", name="List").click()
    page.get_by_role("heading", level=2, name=re.compile("· Compute properties$")).first.wait_for()
    assert_clean(page)

    page.get_by_role("link", name=re.compile("^ABL1 fragment")).click()
    page.get_by_role("heading", level=1, name="ABL1 fragment").wait_for()
    page.get_by_role("heading", level=2, name=re.compile("· Compute properties$")).first.wait_for()
    assert page.get_by_role("heading", level=2).count() == 2


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_proteins_sequence_in_its_viewer(open_as: Any, world: World, color_scheme: str) -> None:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    admin = world.users["admin"]
    sequence = "MLEICLKLVGCKSKKGLSSSSSCYLEEALQRPVASDFEPQGLSEAARWNSKENLLAGPSENDPNLFVALYDFVASG"
    made = entities.create(
        admin, "protein.Protein", world.project, {"sequence": sequence, "name": "ABL1"}
    )
    from iki_plugin_protein.models import Protein  # type: ignore[import-not-found]

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
        admin, "protein.Protein", world.project, {"sequence": "TTCCPSIVAR", "name": "Crambin"}
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
        "protein.ProteinStructure",
        world.project,
        {"structure": file.pk, "method": "xray", "chains": [crambin.pk]},
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/", color_scheme)
    page.get_by_role("link", name="Structure", exact=True).click()
    page.get_by_role("link", name="Download crambin.pdb").wait_for()
    frame_element = page.locator("iframe.structure-frame")
    frame_element.wait_for()
    # Mol* has drawn it once the page stops saying it's drawing.
    page.get_by_text("Drawing crambin.pdb…").wait_for(state="detached", timeout=60_000)
    if page.get_by_text("This browser can’t draw 3D structures").count():
        # Some browsers can't draw 3D at all (no GPU, WebGL off): the page says so, in place
        # of the frame, and offers the file to open elsewhere.
        assert browser_name == "firefox", f"{browser_name} has no WebGL"
        assert not page.locator("iframe.structure-frame").count()
        assert_clean(page)
        return
    (frame,) = [f for f in page.frames if f.url.endswith("/viewers/structure/") and f.parent_frame]
    assert frame.evaluate(WEBGL)
    shown = page.frame_locator("iframe.structure-frame")
    shown.locator("canvas").first.wait_for()
    shown.get_by_text("Sequence of").first.wait_for()
    assert_clean(page)

    # A saved view keeps where the camera is, and opens on its revision.
    page.get_by_role("button", name="Save view").click()
    dialog = page.get_by_role("alertdialog", name="Save this view")
    dialog.get_by_role("textbox", name="Name").fill("Crambin, N-terminus")
    dialog.get_by_role("button", name="Save view").click()
    page.wait_for_url(re.compile(r"/entities/VIEW-\d+/$"))
    page.get_by_text(f"Revision 1 of {hrid}, as the view saved it.").wait_for()
    page.get_by_text("Drawing crambin.pdb…").wait_for(state="detached", timeout=60_000)
    page.frame_locator("iframe.structure-frame").locator("canvas").first.wait_for()
    saved = entities.current_values_of([Entity.objects.get(type="core.View")])
    (state,) = [v["state"] for v in saved.values()]
    assert set(state) == {"version", "camera"} and len(state["camera"]["position"]) == 3
    assert_clean(page)
    page.goto(page.url.split("/entities/")[0] + f"/entities/{hrid}/viewers/structure/")
    page.get_by_role("link", name="Crambin, N-terminus").wait_for()

    # Mol* can't read Iki's cookies, storage or page, or call the API as the person.
    page.get_by_text("Drawing crambin.pdb…").wait_for(state="detached", timeout=60_000)
    # The frame's element, then its page: under load, WebKit can list it before it's loaded.
    handle = page.locator("iframe.structure-frame").element_handle()
    frame = handle.content_frame() if handle else None
    assert frame is not None
    frame.wait_for_url("**/viewers/structure/")
    assert frame.evaluate(SANDBOX_PROBES) == ISOLATED
    frame.evaluate("() => { location.href = 'about:blank'; }")
    page.get_by_role("alert").get_by_text(
        "The structure viewer tried to open another page, so Iki closed it."
    ).wait_for()
    assert page.locator("iframe").count() == 0


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
        admin, "protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
    ).entity
    abl2 = entities.create(
        admin, "protein.Protein", world.project, {"sequence": "MLEVCLKLVGA", "name": "ABL2"}
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
        "core.Alignment",
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
    saved = entities.current_values_of([Entity.objects.get(type="core.View")])
    (state,) = [v["state"] for v in saved.values()]
    assert state == {"version": 1, "position": {"column": 311, "row": 2}}
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
        admin, "protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
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
    (frame,) = [f for f in page.frames if "/plugin-components/protein.residues/" in f.url]
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
    # Once the overview has what it asked for: had a request of it still been going, its
    # answer would send the page to sign in, coming back to the overview.
    page.get_by_text("Your role").wait_for()
    page.wait_for_function(
        "() => performance.getEntriesByType('resource')"
        ".some((e) => new URL(e.name).pathname.endsWith('/costs') && e.responseEnd > 0)"
    )
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
            admin, "protein.Protein", world.project, {"sequence": sequence, "name": name}
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
                    "type": "protein.Protein",
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
    made = entities.create(admin, "core.App", world.project, {"name": "Kinases", "spec": spec})
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
            "protein.Protein",
            world.project,
            {"sequence": sequence, "name": name, "organism": organism},
        )
        made[name] = Entity.objects.get(pk=found.entity.pk).hrid
    spec = {
        "version": 1,
        "entity": {"type": "protein.Protein"},
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
                "title": "From the same organism",
                "component": "iki.entities",
                "props": {
                    "type": "protein.Protein",
                    "select": "none",
                    "where": {
                        "all": [
                            {
                                "field": "organism",
                                "op": "eq",
                                "value": "{{ entity.values.organism }}",
                            },
                            {"field": "name", "op": "ne", "value": "{{ entity.values.name }}"},
                        ]
                    },
                    "columns": [{"field": "name"}],
                },
            },
        ],
    }
    entities.create(admin, "core.App", world.project, {"name": "Relatives", "spec": spec})
    page = open_as("admin", f"/entities/{made['ABL1']}/")
    # By CSS and text, not role, until axe has run (Firefox; see above).
    page.locator(".tabs a", has_text="Relatives").click()
    page.get_by_text("ABL1 has 10 residues.").wait_for()
    grid = page.locator(".app-panel", has_text="From the same organism")
    grid.get_by_text(made["ABL2"]).wait_for()
    assert grid.get_by_text(made["Abl"]).count() == 0
    assert_clean(page)


def test_a_projects_page_opens_with_its_dashboard(open_as: Any, world: World) -> None:
    from iki_server.entities import dashboards
    from iki_server.entities import services as entities

    admin = world.users["admin"]
    entities.create(
        admin, "protein.Protein", world.project, {"sequence": "MLEICLKLVG", "name": "ABL1"}
    )
    spec = {
        "version": 1,
        "panels": [
            {
                "id": "proteins",
                "title": "Our proteins",
                "component": "iki.entities",
                "props": {
                    "type": "protein.Protein",
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
    made = entities.create(admin, "core.App", world.project, {"name": "Atlas", "spec": spec})
    dashboards.choose(world.project, str(made.entity.pk), by=admin)
    page = open_as("viewer", f"/projects/{world.project.slug}/")
    # By CSS and text, not role, until axe has run (Firefox; see above).
    shown = page.locator(".dashboard", has_text="Atlas")
    shown.get_by_text("1 so far.").wait_for()
    shown.locator(".app-panel", has_text="Our proteins").get_by_text("ABL1").wait_for()
    assert_clean(page)

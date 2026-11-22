"""The web app's pages: the page that loads the app, and how its files are served."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from browser import Browser
from conftest import FAKE_WEB_APP_MANIFEST
from django.utils.csp import CSP
from org_world import World, build_world, post
from test_web_security import csp_directives

from iki_server.setup.models import Installation
from iki_server.web import frontend

pytestmark = pytest.mark.django_db

APP_PAGES = [
    "/",
    "/projects/",
    "/projects/new/",
    "/projects/a-b/",
    "/projects/a-b/members/",
    "/projects/a-b/entities/",
    "/projects/a-b/entities/new/",
    "/types/",
    "/types/acme/dna.DNASequence/",
    "/entities/",
    "/entities/types/acme/dna.DNASequence/",
    "/requests/",
    "/runs/",
    "/entities/DNA-000001/",
    "/entities/DNA-000001/edit/",
    "/entities/DNA-000001/audit/",
    "/entities/DNA-000001/lineage/",
    "/entities/PRT-000001/components/acme/protein.residues/",
    "/entities/PRT-000001/viewers/sequence/",
    "/projects/a-b/runs/",
    "/projects/a-b/runs/new/",
    "/runs/RUN-000001/",
    "/batches/BAT-000001/",
    "/entities/",
    "/entities/types/acme/dna.DNASequence/",
    "/requests/",
    "/runs/",
    "/saved-searches/01a1c452-a14b-7195-acd0-4b6ba266a656/",
]


@pytest.fixture
def world() -> World:
    return build_world()


ROUTER = Path(__file__).parents[2] / "web" / "app" / "src" / "router.tsx"
#: A value for each of the router's path parameters.
SAMPLES = {
    "slug": "a-b",
    "ref": "DNA-000001",
    "name": "acme/dna.DNASequence",
    "viewer": "sequence",
    "app": "APP-000001",
    "webhookId": "01a1c452-a14b-7195-acd0-4b6ba266a652",
    "draft": "01a1c452-a14b-7195-acd0-4b6ba266a653",
    "importId": "01a1c452-a14b-7195-acd0-4b6ba266a654",
    "registryId": "01a1c452-a14b-7195-acd0-4b6ba266a655",
    "savedId": "01a1c452-a14b-7195-acd0-4b6ba266a656",
    "id": "01a1c452-a14b-7195-acd0-4b6ba266a657",
    # A splat, `$`, takes the rest of the path: a name with its `/`, as a type's or a
    # component's has (ADR 0113).
    "": "acme/protein.Protein",
}


def routed_pages() -> list[str]:
    """Every page the web app's router has, with a sample for each parameter, so a page
    added there can't be missing from the server's (a link to it, opened, would 404)."""
    source = ROUTER.read_text()
    found = re.finditer(
        r'const (\w+) = createRoute\(\{\s*getParentRoute: \(\) => (\w+),\s*path: "([^"]*)"',
        source,
    )
    routes: dict[str, tuple[str, str, bool]] = {}
    for match in found:
        body = source[match.end() : source.index("});", match.end())]
        routes[match[1]] = (match[2], match[3], "component" in body)
    assert len(routes) == source.count("createRoute({"), "router.tsx changed shape"

    def full(name: str) -> str:
        if name not in routes:
            return ""
        parent, path, _ = routes[name]
        return f"{full(parent)}/{path.strip('/')}".rstrip("/")

    # A route without a page of its own only lays out its children's.
    pages = {(full(name) or "") + "/" for name, route in routes.items() if route[2]}
    return sorted(re.sub(r"\$(\w*)", lambda m: SAMPLES[m[1]], p.replace("//", "/")) for p in pages)


def test_every_page_the_app_routes_loads_it(world: World) -> None:
    viewer = world.browser("viewer")
    statuses = {p: viewer.get(p).status_code for p in routed_pages()}
    missing = [f"{p}: {status}" for p, status in statuses.items() if status != 200]
    assert missing == [], "\n".join(missing)


@pytest.mark.parametrize("path", APP_PAGES)
def test_app_pages_load_the_app_with_the_session_and_csrf_token(world: World, path: str) -> None:
    page = world.browser("viewer").get(path)
    assert page.status_code == 200
    html = page.content.decode()
    assert '<div id="root">' in html
    assert re.search(r'<meta name="csrf-token" content="[^"]{20,}">', html)
    assert '<meta name="iki-organization" content="Example Bio">' in html
    assert '<script type="module" src="/static/app/assets/main-test.js"></script>' in html
    assert '<link rel="modulepreload" href="/static/app/assets/vendor-test.js">' in html
    assert '<link rel="stylesheet" href="/static/iki/iki.css">' in html


def test_app_pages_say_whether_the_identity_provider_provisions_accounts(world: World) -> None:
    """So the forms that add people say how someone new gets an account (ADR 0026)."""
    from iki_server.scim import tokens

    meta = '<meta name="iki-provisioning" content="{}">'
    assert meta.format("") in world.browser("admin").get("/projects/").content.decode()
    tokens.create(by=world.users["org_admin"])
    assert meta.format("scim") in world.browser("admin").get("/projects/").content.decode()
    tokens.revoke(by=world.users["org_admin"])
    assert meta.format("") in world.browser("admin").get("/projects/").content.decode()


def test_app_pages_carry_no_data(world: World) -> None:
    # Everything comes from the API, which decides who sees what; the page is
    # the same for everyone, so it can't leak which projects exist.
    outsider = world.browser("outsider").get(f"/projects/{world.project.slug}/").content
    member = world.browser("admin").get(f"/projects/{world.project.slug}/").content
    strip = re.compile(rb'content="[^"]+"')  # the CSRF token differs per session
    assert strip.sub(b"", outsider) == strip.sub(b"", member)
    assert world.project.name.encode() not in member


def test_app_pages_keep_the_strict_policy(world: World) -> None:
    csp = csp_directives(world.browser("viewer").get("/projects/")[CSP.HEADER_ENFORCE])
    assert csp["script-src"] == ["'self'"]
    assert csp["style-src"] == ["'self'"]
    assert csp["connect-src"] == ["'self'"]
    assert csp["trusted-types"] == ["iki-workers"]
    assert csp["require-trusted-types-for"] == ["'script'"]
    # Plugins' components: only in Iki's own frames, which sandbox them (ADR 0084).
    assert csp["frame-src"] == ["'self'"]
    # Its workers: its own scripts, which carry policies of their own (ADR 0088).
    assert csp["worker-src"] == ["'self'"]


def test_app_pages_need_sign_in_and_come_back_after(world: World) -> None:
    response = Browser().get("/projects/kinase-atlas/members/")
    assert response.status_code == 302
    assert response["Location"] == "/login/?next=/projects/kinase-atlas/members/"


def test_app_pages_answer_only_get(world: World) -> None:
    assert post(world.browser("admin"), "/projects/").status_code == 405


def test_home_sends_a_new_installation_to_setup() -> None:
    Installation.objects.update_or_create(pk=1, defaults={"setup_completed_at": None})
    response = Browser().get("/")
    assert response.status_code == 302
    assert response["Location"] == "/setup/"


def test_a_missing_build_says_so_instead_of_a_blank_page(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> dict[str, Any]:
        raise frontend.AppNotBuilt

    monkeypatch.setattr(frontend, "manifest", missing)
    page = world.browser("viewer").get("/projects/")
    assert page.status_code == 503
    assert "Iki isn’t fully installed" in page.content.decode()
    assert b"<script" not in page.content


def test_the_dev_server_is_used_only_when_on_and_asked_for(world: World, settings: Any) -> None:
    browser = world.browser("viewer")
    via_vite = {frontend.DEV_HEADER: "1"}
    assert b"@vite/client" not in browser.get("/projects/", headers=via_vite).content
    settings.IKI_WEB_DEV_SERVER = True
    assert b"@vite/client" not in browser.get("/projects/").content
    html = browser.get("/projects/", headers=via_vite).content.decode()
    scripts = re.findall(r'<script type="module" src="([^"]+)"', html)
    assert scripts == [
        "/static/app/@vite/client",
        "/static/app/dev-preamble.js",
        "/static/app/src/main.tsx",
    ]


def test_chunks_imported_by_the_entry_are_preloaded_once() -> None:
    chunks = {
        **FAKE_WEB_APP_MANIFEST,
        "_vendor-test.js": {"file": "assets/vendor-test.js", "imports": ["_shared.js"]},
        "_shared.js": {"file": "assets/shared.js", "imports": ["_vendor-test.js"]},
    }
    assert frontend._imports(chunks, frontend.ENTRY, set()) == [
        "assets/vendor-test.js",
        "assets/shared.js",
    ]


# --- The sidebar (ADR 0176) ------------------------------------------------------


class _Links(HTMLParser):
    """Each link in an element with the id or class asked for: its text as a screen reader
    reads it (without what's aria-hidden), where it goes, and whether it's the current page."""

    def __init__(self, within: str) -> None:
        super().__init__()
        self.within = within
        self.depth = 0
        #: The depth an aria-hidden element opened at, while in one.
        self.hidden: int | None = None
        self.links: list[dict[str, Any]] = []
        self.link: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if self.depth:
            self.depth += tag not in VOID
            if self.hidden is None and found.get("aria-hidden") == "true":
                self.hidden = self.depth
        elif self.within in (found.get("class") or "").split() or found.get("id") == self.within:
            self.depth = 1
        if self.depth and tag == "a":
            self.link = {"href": found["href"], "current": "aria-current" in found, "text": ""}
            self.link["label"] = found.get("aria-label")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """``<path ... />``: opened and closed at once."""

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        if self.hidden == self.depth:
            self.hidden = None
        if tag == "a" and self.link is not None:
            self.link["text"] = " ".join(self.link["text"].split())
            self.links.append(self.link)
            self.link = None
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.link is not None and self.hidden is None:
            self.link["text"] += data


VOID = {"input", "br", "img", "meta", "link", "hr"}


def links_in(html: str, within: str = "sidebar-nav") -> list[dict[str, Any]]:
    parser = _Links(within)
    parser.feed(html)
    return parser.links


def sidebar_links(html: str) -> list[str]:
    return [link["text"] for link in links_in(html)]


def current_links(html: str) -> list[str]:
    return [link["text"] for link in links_in(html) if link["current"]]


@pytest.mark.parametrize(
    ("principal", "admin"),
    [
        ("viewer", None),
        ("org_admin", "/admin/"),
        ("compute_admin", "/admin/"),
        ("auditor", "/admin/"),
    ],
)
def test_server_pages_have_the_web_apps_sidebar(
    world: World, principal: str, admin: str | None
) -> None:
    """The People, Teams, Settings and Audit pages offer what the web app's sidebar does,
    under the same conditions: Admin, to the admin area, for those who may open any page of
    it, and the projects they're in."""
    from iki_server.web.navigation import spec

    session = world.browser(principal)
    can = session.get("/api/v1/me").json()["can"]
    html = session.get("/teams/").content.decode()
    projects = [
        p["name"]
        for p in session.get("/api/v1/projects?member=true&archived=false&sort=name").json()[
            "items"
        ]
    ]
    expected = [
        "My work",
        "Inbox",
        "All entities",
        "Requests",
        "Runs",
        "Files",
        *(["New project"] if can["create_projects"] else []),
        *projects,
        "All projects",
        "Types",
        "People and teams",
        *(["Admin"] if admin else []),
    ]
    assert sidebar_links(html) == expected
    hrefs = {link["text"]: link["href"] for link in links_in(html)}
    assert hrefs.get("Admin") == admin
    assert hrefs["People and teams"] == "/teams/"
    assert [link["href"] for link in links_in(html, "account-menu")] == [
        i["href"] for i in spec()["account"]
    ]


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/teams/", "People and teams"),
        ("/teams/structural-biology/", "People and teams"),
        # The admin area's pages mark theirs in its own sidebar (test_admin_area.py).
    ],
)
def test_the_sidebar_marks_the_section_a_page_is_in(world: World, path: str, section: str) -> None:
    page = world.browser("org_admin").get(path)
    assert page.status_code == 200
    assert current_links(page.content.decode()) == [section]


def test_the_admin_areas_pages_draw_its_frame_not_the_sidebar(world: World) -> None:
    """Admin is one link, to the admin area, whose pages have a sidebar of their own."""
    html = world.browser("org_admin").get("/admin/sign-in/").content.decode()
    assert 'aria-label="Admin"' in html
    assert 'class="sidebar-nav"' not in html


def test_the_most_specific_link_is_the_current_one() -> None:
    """A project's page marks the project, not All projects; a type's, the type."""
    from iki_server.web.navigation import Link, current_of

    links = [
        Link("All projects", "/projects/", prefixes=("/projects/",)),
        Link("Kinase Atlas", "/projects/kinase-atlas/", prefixes=("/projects/kinase-atlas/",)),
        Link("Types", "/types/", prefixes=("/types/",)),
    ]
    assert current_of("/projects/kinase-atlas/entities/", links) is links[1]
    assert current_of("/projects/", links) is links[0]
    assert current_of("/projects/new/", links) is links[0]
    assert current_of("/types/acme/", links) is links[2]
    assert current_of("/work/", links) is None


def test_the_sidebar_lists_the_readers_active_projects_and_saved_searches(world: World) -> None:
    """Their own saved searches, in the projects they can still read: not another's, nor one
    in a project they've left. Each opens from its own address."""
    from iki_server.org.models import Project, ProjectMembership, ProjectRole
    from iki_server.searches.models import SavedSearch

    viewer = world.users["viewer"]
    left = Project.objects.create(team=world.team, name="Left Behind")
    mine = SavedSearch.objects.create(
        project=world.project, name="Kinases", query={"q": "kinase"}, created_by=viewer
    )
    SavedSearch.objects.create(
        project=world.project,
        name="Theirs",
        query={},
        created_by=world.users["editor"],
        shared=True,
    )
    SavedSearch.objects.create(project=left, name="Gone", query={}, created_by=viewer)
    ProjectMembership.objects.create(
        project=left, user=world.users["editor"], role=ProjectRole.ADMIN
    )

    html = world.browser("viewer").get("/teams/").content.decode()
    links = {link["text"]: link["href"] for link in links_in(html)}
    assert links["Kinases"] == f"/saved-searches/{mine.pk}/"
    assert "Theirs" not in links and "Gone" not in links
    # Their active project, not the archived one, nor one they're not in.
    assert links["Kinase Atlas"] == "/projects/kinase-atlas/"
    assert "Old Screens" not in links and "Left Behind" not in links


def test_a_projects_square_is_its_color_as_the_app_draws_it(world: World) -> None:
    """One of the stylesheet's eight swatches, from its ID, as project-color.ts works it out
    (its test takes the same IDs to the same swatches): the same color on the server's pages
    as in the app."""
    from iki_server.web.navigation import swatch_of

    assert [swatch_of(pk) for pk in ("00000000-0000-7000-8000-000000000001", "p2")] == [4, 2]
    assert swatch_of("iki/dna.Plasmid") == 2
    assert {swatch_of(f"project-{n}") for n in range(64)} == set(range(1, 9))
    html = world.browser("viewer").get("/teams/").content.decode()
    swatch = swatch_of(str(world.project.pk))
    assert (
        f'<span class="swatch swatch-large" data-swatch="{swatch}" aria-hidden="true"></span>'
        '<span class="nav-label">Kinase Atlas</span>'
    ) in html


def test_the_account_is_marked_on_the_swatch_its_id_picks(world: World) -> None:
    """A person's mark is on one of the eight swatches, from their ID, as the app's sidebar
    draws it: not the accent, the same for everyone."""
    from iki_server.web.navigation import swatch_of

    viewer = world.users["viewer"]
    html = world.browser("viewer").get("/teams/").content.decode()
    assert (
        f'<span class="avatar" data-swatch="{swatch_of(str(viewer.pk))}" aria-hidden="true">'
        in html
    )


def test_the_sidebar_shows_the_project_a_page_is_in_beyond_the_first_few(world: World) -> None:
    from iki_server.org.models import Project, ProjectMembership, ProjectRole
    from iki_server.web.navigation import sidebar, spec

    shown = spec()["projects"]["shown"]
    for n in range(shown + 1):
        p = Project.objects.create(team=world.team, name=f"A{n:02d} Screen")
        ProjectMembership.objects.create(
            project=p, user=world.users["viewer"], role=ProjectRole.VIEWER
        )
    session = world.browser("viewer")
    names = [t for t in sidebar_links(session.get("/teams/").content.decode()) if "Screen" in t]
    assert len(names) == shown
    # A project's pages are the app's, whose sidebar is the server's own.
    deep = sidebar(world.users["viewer"], "/projects/kinase-atlas/members/", {})
    assert [p.label for p in deep.projects][-1] == "Kinase Atlas"
    assert [p.label for p in deep.projects if p.current] == ["Kinase Atlas"]


def test_the_inbox_says_how_many_are_unread(world: World) -> None:
    from iki_server.notifications import kinds
    from iki_server.notifications.services import notify

    admin = world.users["admin"]
    notify([admin], kinds.TEAM_ADDED, title="Added", path="/teams/", actor=world.users["viewer"])
    html = world.browser("admin").get("/teams/").content.decode()
    (inbox,) = [link for link in links_in(html) if link["href"] == "/notifications/"]
    # Named by what it shows, then how many, as a screen reader reads it.
    assert (inbox["text"], inbox["label"]) == ("Inbox 1 unread", None)
    assert '<span class="nav-badge">1</span>' in html


def test_the_entities_section_opens_as_the_reader_left_it(world: World) -> None:
    """Closed away from the entities' pages, where its types would push the rest of the
    sidebar out of sight, until the reader opens it; then as they left it, everywhere."""
    from iki_server.web.navigation import ENTITIES_COOKIE, entities_open

    session = world.browser("viewer")
    html = session.get("/teams/").content.decode()
    assert 'aria-expanded="false" aria-controls="nav-entities"' in html
    assert 'id="nav-entities" hidden data-entity-types' in html
    session.cookies[ENTITIES_COOKIE] = "open"
    html = session.get("/teams/").content.decode()
    assert 'aria-expanded="true" aria-controls="nav-entities"' in html
    assert 'id="nav-entities" data-entity-types' in html
    # As the app's sidebar works it out (sidebar.test.tsx).
    assert entities_open("/entities/PLS-000001/", None, "/entities/")
    assert not entities_open("/requests/", None, "/entities/")
    assert not entities_open("/entities/", "closed", "/entities/")
    assert entities_open("/teams/", "open", "/entities/")


def test_signed_out_pages_have_no_sidebar(world: World) -> None:
    html = world.browser("anonymous").get("/login/").content.decode()
    assert 'class="sidebar"' not in html
    assert 'class="site-header"' in html


def test_server_pages_load_the_search_palette_for_those_signed_in(
    world: World, settings: Any
) -> None:
    """⌘K opens search everything (ADR 0176) on the server's own pages too: they load the
    palette's entry, with the chunks it shares with the app, and React Aria's stylesheet,
    which the CSP would refuse inline."""
    html = world.browser("viewer").get("/teams/").content.decode()
    scripts = re.findall(r'<script type="module" src="([^"]+)"', html)
    assert scripts == ["/static/app/assets/search-entry-test.js"]
    assert '<link rel="modulepreload" href="/static/app/assets/vendor-test.js">' in html
    assert 'id="react-aria-pressable-style"' in html
    # Not before signing in: there's nothing to search.
    signed_out = Browser().get("/login/").content.decode()
    assert "search-entry" not in signed_out
    # Through the dev server, from its modules.
    settings.IKI_WEB_DEV_SERVER = True
    dev = world.browser("viewer").get("/teams/", headers={frontend.DEV_HEADER: "1"})
    assert re.findall(r'<script type="module" src="([^"]+)"', dev.content.decode()) == [
        "/static/app/@vite/client",
        "/static/app/dev-preamble.js",
        "/static/app/src/search-entry.tsx",
    ]


def test_a_server_page_works_without_the_palette_when_it_isnt_built(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = {k: v for k, v in FAKE_WEB_APP_MANIFEST.items() if k != frontend.SEARCH_ENTRY}
    monkeypatch.setattr(frontend, "manifest", lambda: without)
    page = world.browser("viewer").get("/teams/")
    assert page.status_code == 200
    assert "search-entry" not in page.content.decode()


def test_the_sidebars_items_are_pages_and_the_app_routes_those_it_says() -> None:
    from django.urls import resolve

    from iki_server.api.schemas import MeCan
    from iki_server.web.navigation import items, spec

    routed = set(routed_pages())
    for item in items():
        resolve(item.href)
        assert item.app == (item.href in routed), item
        assert item.requires is None or item.requires in MeCan.model_fields, item
    saved = spec()["saved"]
    assert saved["app"] == (f"{saved['href']}{SAMPLES['savedId']}/" in routed)


# --- Serving the build ---------------------------------------------------------


def test_the_build_manifest_is_where_collectstatic_finds_it() -> None:
    # collectstatic skips dot-directories, so Vite's default .vite/ wouldn't ship.
    config = (Path(__file__).parents[2] / "web/app/vite.config.ts").read_text()
    assert 'manifest: "manifest.json"' in config
    assert frontend.MANIFEST == "app/manifest.json"


def test_vite_named_files_keep_their_names_and_others_get_hashed(tmp_path: Path) -> None:
    storage = frontend.StaticFilesStorage(location=tmp_path)
    assert storage.hashed_name("app/assets/main-B5l83iiN.js") == "app/assets/main-B5l83iiN.js"
    (tmp_path / "iki").mkdir()
    (tmp_path / "iki/iki.css").write_text("body {}")
    assert re.fullmatch(r"iki/iki\.[0-9a-f]{12}\.css", storage.hashed_name("iki/iki.css"))


@pytest.mark.parametrize(
    ("url", "immutable"),
    [
        ("/static/app/assets/main-B5l83iiN.js", True),
        ("/static/app/assets/main-B5l83iiN.js.map", True),
        ("/static/app/manifest.json", False),
        ("/static/iki/iki.css", False),
    ],
)
def test_the_apps_files_are_cached_for_ever(url: str, immutable: bool) -> None:
    # Django's hashed names keep servestatic's own test; the browser tests
    # check both against the real server.
    async def get_response(request: object) -> None:
        return None

    middleware = frontend.ServeStaticMiddleware(get_response)
    assert middleware.immutable_file_test("", url) is immutable


def test_the_published_openapi_document_is_current() -> None:
    from iki_server.api.v1 import api

    committed = Path(__file__).parents[2] / "web/packages/sdk/openapi.json"
    current = api.get_openapi_schema()
    assert json.loads(committed.read_text()) == json.loads(json.dumps(current)), (
        "The API changed. Run `make client` and commit web/packages/sdk."
    )


def test_the_worker_protocol_document_is_current() -> None:
    from iki_server.api.worker import api

    committed = Path(__file__).parents[2] / "worker/crates/iki-protocol/worker-openapi.json"
    current = api.get_openapi_schema()
    assert json.loads(committed.read_text()) == json.loads(json.dumps(current)), (
        "The worker API changed. Run `make protocol` and commit worker/."
    )

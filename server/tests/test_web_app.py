"""The web app's pages: the page that loads the app, and how its files are served."""

import json
import re
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
    "/entities/DNA-000001/history/",
    "/entities/DNA-000001/lineage/",
    "/entities/PRT-000001/components/acme/protein.residues/",
    "/entities/PRT-000001/viewers/sequence/",
    "/projects/a-b/runs/",
    "/projects/a-b/runs/new/",
    "/runs/RUN-000001/",
    "/batches/BAT-000001/",
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


# --- The header ------------------------------------------------------------------


def header_links(html: str) -> list[tuple[str, bool]]:
    """The main navigation's links: each one's label, and whether it's the current page."""
    nav = re.search(r'<nav class="site-nav" aria-label="Main">(.*?)</nav>', html, re.S)
    assert nav, "no main navigation"
    return [
        (label, bool(current))
        for current, label in re.findall(
            r'<a href="[^"]*"( aria-current="page")?>([^<]+)</a>', nav[1]
        )
    ]


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        ("viewer", ["My work", "Projects", "Types", "Teams"]),
        (
            "org_admin",
            ["My work", "Projects", "Types", "Teams", "People", "Settings", "Costs"],
        ),
        ("compute_admin", ["My work", "Projects", "Types", "Teams", "Costs", "Compute"]),
        ("auditor", ["My work", "Projects", "Types", "Teams", "Costs", "Audit"]),
    ],
)
def test_server_pages_have_the_web_apps_header(
    world: World, principal: str, expected: list[str]
) -> None:
    """The People, Teams, Settings and Audit pages offer what the web app's header does,
    under the same conditions: the ones /me's `can` says they may open."""
    from iki_server.web.navigation import items

    session = world.browser(principal)
    can = session.get("/api/v1/me").json()["can"]
    offered = [i.label for i in items() if i.requires is None or can[i.requires]]
    links = [label for label, _ in header_links(session.get("/teams/").content.decode())]
    assert links == offered == expected


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/teams/", "Teams"),
        ("/teams/structural-biology/", "Teams"),
        ("/people/", "People"),
        ("/service-accounts/", "People"),
        ("/settings/sign-in/", "Settings"),
        ("/settings/signatures/", "Settings"),
        ("/settings/registries/", "Settings"),
        ("/settings/email/", "Settings"),
        ("/audit/", "Audit"),
    ],
)
def test_the_header_marks_the_section_a_page_is_in(world: World, path: str, section: str) -> None:
    from iki_server.org.models import OrgRole, OrgRoleAssignment

    OrgRoleAssignment.objects.create(user=world.users["org_admin"], role=OrgRole.AUDITOR)
    page = world.browser("org_admin").get(path)
    assert page.status_code == 200
    assert [label for label, current in header_links(page.content.decode()) if current] == [section]


def test_the_headers_items_are_pages_and_the_app_routes_those_it_says() -> None:
    from django.urls import resolve

    from iki_server.api.schemas import MeCan
    from iki_server.web.navigation import items

    routed = set(routed_pages())
    for item in items():
        resolve(item.href)
        assert item.app == (item.href in routed), item
        assert item.requires is None or item.requires in MeCan.model_fields, item


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

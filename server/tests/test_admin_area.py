"""The admin area (ADR 0176): its frame on the server's pages, what its sidebar offers whom,
and /admin/, which opens the first page of it someone may open."""

import re

import pytest
from org_world import World, build_world
from test_web_app import routed_pages

from iki_server.web import admin_area

pytestmark = pytest.mark.django_db

ORGANIZATION = [
    "Sign-in and provisioning",
    "People",
    "Apps",
    "Email",
]
DATA = ["Registries", "Plugins", "Outside APIs", "Webhooks"]
COMPLIANCE = ["Audit log", "Electronic signatures", "Legal holds"]


@pytest.fixture
def world() -> World:
    return build_world()


def sidebar(html: str) -> list[tuple[str, list[str]]]:
    """The admin sidebar's groups, each with its links' labels."""
    nav = re.search(r'<nav class="admin-nav" aria-label="Admin"[^>]*>(.*?)</nav>', html, re.S)
    assert nav, "no admin sidebar"
    groups = re.findall(
        r'<p class="admin-group" id="([^"]+)">([^<]+)</p>\s*'
        r'<ul class="admin-links" aria-labelledby="\1">(.*?)</ul>',
        nav[1],
        re.S,
    )
    return [
        (label, re.findall(r'<a class="admin-link"[^>]*><svg.*?</svg>([^<]+)</a>', links, re.S))
        for _, label, links in groups
    ]


def current(html: str) -> list[tuple[str, str]]:
    """The sidebar's current items: each one's label, and whether it's this page or under it."""
    link = r'<a class="admin-link"[^>]* aria-current="(\w+)"><svg.*?</svg>([^<]+)</a>'
    return re.findall(link, html)


def crumbs(html: str) -> list[str]:
    nav = re.search(r'<nav class="admin-crumbs" aria-label="Breadcrumb">(.*?)</nav>', html, re.S)
    assert nav, "no breadcrumb"
    return [re.sub(r"<[^>]+>", "", item) for item in re.findall(r"<li>(.*?)</li>", nav[1], re.S)]


@pytest.mark.parametrize(
    ("principal", "path", "expected"),
    [
        (
            "org_admin",
            "/admin/sign-in/",
            [
                ("Organization", ORGANIZATION),
                ("Data", DATA),
                ("Compute and cost", ["Costs and budgets"]),
                ("Compliance", ["Electronic signatures", "Legal holds"]),
            ],
        ),
        (
            "auditor",
            "/admin/audit/",
            [("Compute and cost", ["Costs and budgets"]), ("Compliance", COMPLIANCE)],
        ),
    ],
)
def test_the_sidebar_offers_only_what_each_person_may_open(
    world: World, principal: str, path: str, expected: list[tuple[str, list[str]]]
) -> None:
    """Grouped as the ADR says, with a group none of whose pages someone may open left out:
    and the same as the web app works out from /me's `can`."""
    session = world.browser(principal)
    page = session.get(path)
    assert page.status_code == 200
    assert sidebar(page.content.decode()) == expected
    can = session.get("/api/v1/me").json()["can"]
    offered = [
        (group.label, [i.label for i in group.items if any(can[need] for need in i.requires)])
        for group in admin_area.groups()
    ]
    assert [g for g in offered if g[1]] == expected


PAGES = [item.href for group in admin_area.groups() for item in group.items if not item.app]


@pytest.mark.parametrize("principal", ["viewer", "org_admin", "compute_admin", "auditor"])
def test_each_server_page_in_the_sidebar_opens_for_whom_it_offers_it(
    world: World, principal: str
) -> None:
    """Each page keeps its own check, and the sidebar agrees with it: it offers a page to
    exactly those who may open it."""
    session = world.browser(principal)
    can = session.get("/api/v1/me").json()["can"]
    for group in admin_area.groups():
        for item in group.items:
            if item.app:
                continue
            offered = any(can[need] for need in item.requires)
            status = session.get(item.href).status_code
            assert status == (200 if offered else 403), (principal, item.href, status)


def test_the_sidebars_items_are_pages_and_the_app_routes_those_it_says() -> None:
    from django.urls import resolve

    from iki_server.api.schemas import MeCan

    routed = set(routed_pages())
    for group in admin_area.groups():
        for item in group.items:
            resolve(item.href)
            assert item.app == (item.href in routed), item
            assert item.requires, item
            assert all(need in MeCan.model_fields for need in item.requires), item


@pytest.mark.parametrize(
    ("principal", "first"),
    [
        ("org_admin", "/admin/sign-in/"),
        ("compute_admin", "/admin/compute/"),
        ("auditor", "/admin/costs/"),
    ],
)
def test_admin_opens_the_first_page_someone_may_open(
    world: World, principal: str, first: str
) -> None:
    response = world.browser(principal).get("/admin/")
    assert response.status_code == 302
    assert response["Location"] == first


def test_admin_is_not_for_someone_who_may_open_none_of_it(world: World) -> None:
    """It says so, and who it's for, without an error: /admin/ itself holds nothing, and
    each page under it still refuses them."""
    response = world.browser("viewer").get("/admin/")
    assert response.status_code == 200
    page = response.content.decode()
    assert "The admin area isn’t open to you" in page
    assert "Org Admins, Compute Admins and Auditors" in page
    assert 'aria-label="Admin"' not in page
    assert 'aria-label="Main"' in page
    assert world.browser("viewer").get("/admin/sign-in/").status_code == 403
    anonymous = world.browser("anonymous").get("/admin/")
    assert anonymous.status_code == 302
    assert anonymous["Location"].startswith("/login/")


def test_every_page_of_the_area_is_under_admin() -> None:
    """The server's and the app's alike: one prefix says which frame a page is in."""
    hrefs = [item.href for group in admin_area.groups() for item in group.items]
    assert [h for h in hrefs if not h.startswith("/admin/")] == []
    app = {item.href for group in admin_area.groups() for item in group.items if item.app}
    routed = {page for page in routed_pages() if page.startswith("/admin/")}
    assert app <= routed
    areas = {page.split("/")[2] for page in routed}
    assert areas == {"compute", "costs", "outside-apis", "webhooks"}


OLD_ADDRESSES = [
    "/settings/sign-in/",
    "/settings/apps/",
    "/settings/email/",
    "/settings/registries/",
    "/settings/plugins/",
    "/settings/outside-apis/",
    "/settings/webhooks/",
    "/settings/signatures/",
    "/settings/legal-holds/",
    "/people/",
    "/service-accounts/",
    "/compute/",
    "/compute/workers/",
    "/costs/",
    "/audit/",
]


@pytest.mark.parametrize("path", OLD_ADDRESSES)
def test_the_old_addresses_are_gone(world: World, path: str) -> None:
    """Iki isn't deployed before M1, so nothing links to them: no redirects, no second
    address for a page."""
    from iki_server.org.models import OrgRole, OrgRoleAssignment

    for role in (OrgRole.AUDITOR, OrgRole.COMPUTE_ADMIN):
        OrgRoleAssignment.objects.create(user=world.users["org_admin"], role=role)
    assert world.browser("org_admin").get(path).status_code == 404


@pytest.mark.parametrize("path", PAGES)
def test_server_pages_are_in_the_admin_frame(world: World, path: str) -> None:
    """Each page is in the admin area's frame, without the main header, and says where it
    is."""
    from iki_server.org.models import OrgRole, OrgRoleAssignment

    OrgRoleAssignment.objects.create(user=world.users["org_admin"], role=OrgRole.AUDITOR)
    page = world.browser("org_admin").get(path)
    assert page.status_code == 200
    html = page.content.decode()
    assert 'class="site-nav"' not in html
    label = next(i.label for g in admin_area.groups() for i in g.items if i.href == path)
    assert current(html) == [("page", label)]
    assert crumbs(html) == ["Admin", label]
    assert '<a class="admin-back" href="/">' in html
    assert '<main id="main" class="admin-main" tabindex="-1">' in html


def test_a_page_under_an_item_marks_it_and_links_back_to_it(world: World) -> None:
    person = world.users["viewer"]
    html = world.browser("org_admin").get(f"/admin/people/{person.pk}/").content.decode()
    assert current(html) == [("true", "People")]
    assert crumbs(html) == ["Admin", "People"]
    assert '<li><a href="/admin/people/">People</a></li>' in html
    service_accounts = world.browser("org_admin").get("/admin/service-accounts/").content.decode()
    assert current(service_accounts) == [("true", "People")]


def test_admin_pages_share_the_main_pages_head_and_its_search(world: World) -> None:
    """The admin frame's <head> is the main pages': ⌘K opens search everything there too,
    and a link added to one can't be missing from the other."""
    html = world.browser("org_admin").get("/admin/plugins/").content.decode()
    scripts = re.findall(r'<script type="module" src="([^"]+)"', html)
    assert scripts == ["/static/app/assets/search-entry-test.js"]
    assert 'id="react-aria-pressable-style"' in html
    main = world.browser("org_admin").get("/teams/").content.decode()
    head = re.compile(r'<link rel="icon".*?(?=</head>)', re.S)
    assert head.search(html)[0] == head.search(main)[0]  # type: ignore[index]


def test_the_frame_has_the_account_and_the_narrow_screens_menu(world: World) -> None:
    html = world.browser("org_admin").get("/admin/plugins/").content.decode()
    assert re.search(
        r'<a class="admin-user" href="/account/"[^>]*><span class="admin-avatar" '
        r'aria-hidden="true">OA</span><span class="admin-user-name">Org Admin</span></a>',
        html,
    )
    assert re.search(r'<form class="admin-account" method="post" action="/logout/">', html)
    # On a narrow screen the sidebar is a drawer, as the main one is: iki.js's, from the
    # menu button in the bar above the page, with a close button and a backdrop.
    assert re.search(
        r'<button class="topbar-menu" type="button" aria-controls="admin-sidebar" '
        r'aria-expanded="false" data-drawer-open>\s*<svg class="icon"[^>]*>.*?</svg>\s*'
        r'<span class="visually-hidden">Menu</span>',
        html,
        re.S,
    )
    assert '<div class="admin-side" id="admin-sidebar" data-sidebar>' in html
    assert re.search(
        r'<button class="sidebar-close" type="button" data-drawer-close>\s*<svg[^>]*>.*?</svg>\s*'
        r'<span class="visually-hidden">Close menu</span>',
        html,
        re.S,
    )
    assert '<div class="sidebar-backdrop" data-drawer-close hidden></div>' in html

"""The server's pages have the web app's header (ADR 0176): a bar across the top of the page
with its breadcrumb and actions, then its title. The same markup as page-top.tsx and ui.tsx's
PageHeader draw, so one stylesheet serves both."""

import re

import pytest
from org_world import World, build_world

from iki_server.org.models import Team, TeamMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def html_of(world: World, principal: str, path: str, status: int = 200) -> str:
    page = world.browser(principal).get(path)
    assert page.status_code == status, (path, page.status_code)
    return page.content.decode()


def bar(html: str) -> str:
    found = re.search(r'<header class="page-top">(.*?)</header>', html, re.S)
    assert found, "no bar"
    return found[1]


def crumbs(part: str) -> list[str]:
    nav = re.search(r'<nav class="crumbs" aria-label="Breadcrumb"><ol>(.*?)</ol></nav>', part)
    assert nav, "no breadcrumb"
    return re.findall(r"<li>(.*?)</li>", nav[1])


def heading(html: str) -> str:
    found = re.search(r'<div class="page-heading">(.*?)\n</div>', html, re.S)
    assert found, "no heading"
    return found[1]


def test_a_page_of_the_main_frame_has_the_bar_then_its_title(world: World) -> None:
    """The bar's in the main region, above the page's body, as layout.tsx draws it; a page
    with no breadcrumb of its own has its title as one, and one without actions an empty
    slot for them, which the stylesheet hides."""
    html = html_of(world, "team_member", "/teams/")
    main = re.search(
        r'<main id="main" class="app-main" tabindex="-1">\s*<header class="page-top">.*?</header>'
        r'\s*<div class="page-body page-wide">(?P<body>.*?)</main>',
        html,
        re.S,
    )
    assert main, "the bar isn't first in the main region"
    top = bar(html)
    assert crumbs(top) == ['<span aria-current="page">Teams</span>']
    assert '<div class="page-top-actions"></div>' in top
    assert re.search(
        r'<div class="page-title-row">\s*<h1>Teams</h1>\s*</div>', heading(main["body"])
    )
    assert html.count("<h1") == 1
    # The old header wrapper is gone. Its name also appears in the shared stylesheet URL.
    class_values = re.findall(r'\sclass="([^"]*)"', html)
    assert all("page-header" not in value.split() for value in class_values)


def test_a_pages_crumbs_actions_and_description(world: World) -> None:
    """A team's page: where it is, its actions in the bar, primary last, and again under its
    title for a phone; what it is beside its title, with an (i) iki.js shows if it's cut
    short."""
    team = world.team
    team.description = "Crystallography and cryo-EM of kinases.\nSee the wiki for the rota."
    team.save()
    # An Org Admin manages it; as a member, they may start a project in it too.
    TeamMembership.objects.create(team=team, user=world.users["org_admin"])
    html = html_of(world, "org_admin", f"/teams/{team.slug}/")
    top = bar(html)
    assert crumbs(top) == [
        '<a href="/teams/">Teams</a>',
        '<span aria-current="page">Structural Biology</span>',
    ]
    actions = re.search(
        r'<div class="page-top-actions"><div class="page-actions" data-twin>(.*?)</div></div>',
        top,
        re.S,
    )
    assert actions, "no actions in the bar"
    buttons = re.findall(r'<a class="button[^"]*" href="([^"]+)">([^<]+)</a>', actions[1])
    assert buttons == [
        (f"/teams/{team.slug}/settings/", "Settings"),
        (f"/projects/new/?team={team.slug}", "New project"),
    ]
    head = heading(html)
    below = re.search(
        r'<div class="page-actions page-actions-below" data-twin>(.*?)</div>', head, re.S
    )
    assert below and below[1] == actions[1]
    # Actions drawn twice mustn't have IDs, which would then be on the page twice.
    assert " id=" not in actions[1]
    assert re.search(
        r'<div class="page-description" data-description>\s*'
        r'<p class="page-description-text">Crystallography and cryo-EM of kinases.\n'
        r"See the wiki for the rota.</p>\s*"
        r'<button class="page-about" type="button" aria-label="About Structural Biology" '
        r'aria-haspopup="dialog" aria-expanded="false" aria-controls="page-about" hidden>',
        head,
    )
    assert re.search(
        r'<div class="page-about-popover" id="page-about" hidden><div class="page-about-dialog" '
        r'role="dialog" aria-label="About Structural Biology" tabindex="-1"><p>Crystallography',
        head,
    )
    # Said once, beside the title: not again in the page.
    assert html.count("Crystallography") == 2


def test_someone_who_can_do_nothing_there_gets_no_actions(world: World) -> None:
    html = html_of(world, "viewer", "/teams/")
    assert "page-actions" not in html
    team = Team.objects.create(name="Proteomics")
    html = html_of(world, "org_admin", f"/teams/{team.slug}/")
    assert "New project" not in html
    assert "page-description" not in html  # It has none.


def test_a_settings_page_leads_back_through_its_team(world: World) -> None:
    team = world.team
    html = html_of(world, "org_admin", f"/teams/{team.slug}/settings/")
    assert crumbs(bar(html)) == [
        '<a href="/teams/">Teams</a>',
        f'<a href="/teams/{team.slug}/">Structural Biology</a>',
        '<span aria-current="page">Settings</span>',
    ]
    assert "<h1>Team settings</h1>" in heading(html)


def test_what_a_page_says_of_itself_is_escaped(world: World) -> None:
    """A name is text wherever the header puts it: its crumb, its title, the (i)'s label."""
    team = Team.objects.create(name='<img src=x onerror="alert(1)">', description="<b>x</b>")
    html = html_of(world, "org_admin", f"/teams/{team.slug}/")
    assert "<img src=x" not in html
    assert "<b>x</b>" not in html
    name = "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
    assert f'<span aria-current="page">{name}</span>' in bar(html)
    assert f"<h1>{name}</h1>" in heading(html)
    assert f'aria-label="About {name}"' in heading(html)


def test_an_admin_page_puts_its_actions_in_the_frames_bar(world: World) -> None:
    """The admin frame's breadcrumb is its own, "Admin / page"; the page gives its actions
    and title."""
    html = html_of(world, "org_admin", "/admin/people/")
    top = re.search(r'<header class="admin-top">(.*?)</header>', html, re.S)
    assert top
    assert re.search(
        r'<div class="admin-actions"><div class="page-actions" data-twin>'
        r'<a class="button" href="/admin/service-accounts/">Service accounts</a></div></div>',
        top[1],
    )
    main = re.search(r'<main id="main" class="admin-main" tabindex="-1">(.*?)</main>', html, re.S)
    assert main
    assert re.search(r'<div class="page-title-row">\s*<h1>People</h1>', main[1])
    assert 'class="page-actions page-actions-below" data-twin>' in main[1]
    assert html.count("<h1") == 1


def test_an_admin_page_under_another_leads_back_through_it(world: World) -> None:
    """Its breadcrumb goes on from the frame's "Admin / item" to the page, as a record's
    does in the app; its title has its badges after it."""
    person = world.users["inactive"]
    html = html_of(world, "org_admin", f"/admin/people/{person.pk}/")
    nav = re.search(r'<nav class="admin-crumbs" aria-label="Breadcrumb">(.*?)</nav>', html, re.S)
    assert nav
    assert re.findall(r"<li>(.*?)</li>", nav[1]) == [
        '<a href="/admin/">Admin</a>',
        '<a href="/admin/people/">People</a>',
        '<span aria-current="page">Inactive</span>',
    ]
    assert "eyebrow" not in html
    assert '<h1>Inactive <span class="badge badge-muted">Deactivated</span></h1>' in heading(html)
    assert '<div class="admin-actions"></div>' in html


def test_a_service_account_leads_back_through_the_list_of_them(world: World) -> None:
    from iki_server.accounts import service_accounts

    account = service_accounts.create(name="LIMS sync", description="", by=world.users["org_admin"])
    html = html_of(world, "org_admin", f"/admin/service-accounts/{account.pk}/")
    nav = re.search(r'<nav class="admin-crumbs" aria-label="Breadcrumb">(.*?)</nav>', html, re.S)
    assert nav
    assert re.findall(r"<li>(.*?)</li>", nav[1]) == [
        '<a href="/admin/">Admin</a>',
        '<a href="/admin/people/">People</a>',
        '<a href="/admin/service-accounts/">Service accounts</a>',
        '<span aria-current="page">LIMS sync</span>',
    ]


def test_a_page_that_isnt_there_says_so_under_the_bar(world: World) -> None:
    """As the app's errors.tsx: its title in the bar and the heading, what to do in a card."""
    html = html_of(world, "viewer", "/teams/no-such-team/", 404)
    assert crumbs(bar(html)) == ['<span aria-current="page">Page not found</span>']
    assert "<h1>Page not found</h1>" in heading(html)
    assert html.count("<h1") == 1


def test_signed_out_a_page_has_its_title_and_no_bar(world: World) -> None:
    """Signed out there's no sidebar, and no bar: nothing above the page to lead back to."""
    html = html_of(world, "anonymous", "/no-such-page/", 404)
    assert '<header class="page-top">' not in html
    assert "<h1>Page not found</h1>" in heading(html)


def test_signing_in_is_one_card_with_its_own_title(world: World) -> None:
    html = html_of(world, "anonymous", "/signed-out/")
    assert '<header class="page-top">' not in html
    assert 'class="page-heading"' not in html
    assert re.search(r'<section class="card card-centered">\s*<h1>', html)
    assert html.count("<h1") == 1

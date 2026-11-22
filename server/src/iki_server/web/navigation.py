"""The sidebar (ADR 0176), the same on server-rendered pages and in the web app.

``navigation.json`` lists its sections, their items and their icons, and both render that
one file: the server's ``base.html`` through :func:`sidebar`, and the web app's
``sidebar.tsx``, which imports it. So the two sidebars can't drift apart. An item has:

- ``label`` and ``href``, and an ``icon``: one of ``icons``, each the paths of a 24-unit
  square drawn in strokes;
- ``requires``: the one of ``/me``'s ``can`` (:func:`iki_server.org.permissions.capabilities`)
  it needs, if any. Both read the same rules;
- ``current``: the paths it's the current item for, by prefix (its ``href`` if left out).
  Of the items a page shows, the one with the longest prefix of its path is current, so a
  project's page marks the project, not All projects;
- ``app``: the web app routes it, so its links there don't load a page.

The rest is the reader's own: their projects, their saved searches, and, fetched by
``iki.js`` as by the app, the types of entity they can read with their counts.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any

from iki_server.entities import access
from iki_server.org import permissions
from iki_server.searches import services as saved_searches

SOURCE = Path(__file__).with_name("navigation.json")
#: Whether the sidebar's Entities section is open, as the reader left it: "open" or
#: "closed". A cookie, so a server-rendered page draws it that way from the start.
ENTITIES_COOKIE = "iki_entities"


@cache
def spec() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(SOURCE.read_text(encoding="utf-8"))
    return spec


@dataclass(frozen=True)
class Item:
    label: str
    href: str
    requires: str | None
    current: tuple[str, ...]
    app: bool
    icon: tuple[str, ...]


def _item(raw: dict[str, Any]) -> Item:
    return Item(
        label=raw["label"],
        href=raw["href"],
        requires=raw.get("requires"),
        current=tuple(raw.get("current", [raw["href"]])),
        app=bool(raw.get("app", False)),
        icon=icon(raw["icon"]) if "icon" in raw else (),
    )


@cache
def entity_counts_url() -> str:
    """Where the API counts the records of each type a reader can read (ADR 0176), or ""
    while it doesn't: the sidebar then lists the types without counts, rather than asking
    for what isn't there."""
    from iki_server.api.v1 import api

    url: str = spec()["entities"]["counts"]
    routes = {str(p.pattern) for p in api.urls[0]}
    return url if url.removeprefix("/api/v1/") in routes else ""


def icon(name: str) -> tuple[str, ...]:
    return tuple(spec()["icons"][name])


def items() -> tuple[Item, ...]:
    """Every fixed item, in the sidebar's order, the admin pages last: what tests check
    are pages, and routed by the app where they say."""
    raw = spec()
    found = [
        *raw["top"],
        raw["entities"]["all"],
        *raw["work"],
        raw["projects"]["new"],
        raw["projects"]["all"],
        *raw["organization"]["items"],
        *raw["account"],
        *raw["organization"]["admin"]["pages"],
    ]
    return tuple(_item(i) for i in found)


@dataclass(frozen=True)
class Link:
    label: str
    href: str
    icon: tuple[str, ...] = ()
    #: The paths it's current for, by prefix.
    prefixes: tuple[str, ...] = ()
    current: bool = False
    #: The inbox's unread count, if it's the inbox.
    badge: int | None = None

    @property
    def initial(self) -> str:
        """A project's mark, as the app draws it: the first letter of its name."""
        return self.label[:1].upper()

    @property
    def badge_text(self) -> str:
        return "99+" if self.badge is not None and self.badge > 99 else str(self.badge)


def initials(name: str) -> str:
    """A person's mark, as the app draws it: the first letters of their first two names."""
    return "".join(word[:1] for word in name.split()[:2]).upper()


@dataclass(frozen=True)
class Sidebar:
    top: list[Link]
    unread: int
    entities_open: bool
    all_entities: Link
    work: list[Link]
    saved: list[Link]
    projects: list[Link]
    new_project: Link | None
    all_projects: Link
    organization: list[Link]
    #: On an administrative page, the ones its reader may open, beneath Admin: until Admin
    #: has a page of its own, the way from one to the next.
    admin_pages: list[Link]
    account: list[Link]
    #: Who's signed in, as the account menu shows them.
    name: str = ""
    email: str = ""
    #: Where the counts of each type of entity come from, if the API has them.
    counts_url: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    icons: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def initials(self) -> str:
        return initials(self.name)


def _link(item: Item) -> Link:
    return Link(item.label, item.href, item.icon, item.current)


def _section(path: str, admin: Link | None, can: dict[str, bool]) -> list[Link]:
    """The admin pages beneath Admin, the one the page is in current, if it's in one."""
    if admin is None or not any(path.startswith(p) for p in admin.prefixes):
        return []
    pages = admin_pages(can)
    return _mark(pages, current_of(path, pages))


def admin_pages(can: dict[str, bool]) -> list[Link]:
    """The administrative pages someone may open, in the sidebar's order."""
    pages = [_item(p) for p in spec()["organization"]["admin"]["pages"]]
    return [_link(p) for p in pages if p.requires is None or can[p.requires]]


def admin_link(can: dict[str, bool]) -> Link | None:
    """Admin, for those who may open any administrative page: to the first of them, current
    on all of them."""
    raw = spec()["organization"]["admin"]
    allowed = admin_pages(can)
    if not allowed:
        return None
    prefixes = tuple(dict.fromkeys(p for page in allowed for p in page.prefixes))
    return Link(raw["label"], allowed[0].href, icon(raw["icon"]), prefixes)


def current_of(path: str, links: Iterable[Link]) -> Link | None:
    """The link a page is in: the one with the longest prefix of its path, the first of
    those as long."""
    best: tuple[int, Link] | None = None
    for link in links:
        for prefix in link.prefixes:
            if path.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), link)
    return best[1] if best else None


def _mark(links: Sequence[Link], current: Link | None) -> list[Link]:
    return [replace(link, current=link is current) for link in links]


def sidebar(user: Any, path: str, cookies: dict[str, str], unread: int = 0) -> Sidebar:  # noqa: ANN401
    """The sidebar of the person signed in, on the page at ``path``."""
    raw = spec()
    can = permissions.capabilities(user)
    top = [
        replace(_link(_item(i)), badge=unread if i.get("badge") == "unread" else None)
        for i in raw["top"]
    ]
    all_entities = _link(_item(raw["entities"]["all"]))
    work = [_link(_item(i)) for i in raw["work"]]
    saved_icon = icon(raw["saved"]["icon"])
    saved = [
        Link(s.name, f"{raw['saved']['href']}{s.pk}/", saved_icon)
        for s in saved_searches.mine(user).order_by("name", "pk")[: raw["projects"]["shown"]]
    ]
    projects = _projects(user, path, raw["projects"])
    new = _item(raw["projects"]["new"])
    new_project = _link(new) if new.requires is None or can[new.requires] else None
    all_projects = _link(_item(raw["projects"]["all"]))
    organization = [_link(_item(i)) for i in raw["organization"]["items"]]
    admin = admin_link(can)
    if admin is not None:
        organization.append(admin)
    account = [_link(_item(i)) for i in raw["account"]]

    shown = [*top, all_entities, *work, *projects, all_projects, *organization, *account]
    if new_project is not None:
        shown.append(new_project)
    current = current_of(path, shown)
    section = _section(path, admin, can) if current is admin else []
    return Sidebar(
        top=_mark(top, current),
        unread=unread,
        entities_open=cookies.get(ENTITIES_COOKIE) != "closed",
        all_entities=_mark([all_entities], current)[0],
        work=_mark(work, current),
        saved=saved,
        projects=_mark(projects, current),
        new_project=_mark([new_project], current)[0] if new_project else None,
        all_projects=_mark([all_projects], current)[0],
        # On an admin page, it's the page beneath Admin that's current.
        organization=_mark(organization, None if section else current),
        admin_pages=section,
        account=_mark(account, current),
        name=user.get_full_name() or user.email,
        email=user.email,
        counts_url=entity_counts_url(),
        labels={
            "entities": raw["entities"]["label"],
            "saved": raw["saved"]["label"],
            "projects": raw["projects"]["label"],
            "organization": raw["organization"]["label"],
        },
        icons={name: icon(name) for name in raw["icons"]},
    )


def _projects(user: Any, path: str, raw: dict[str, Any]) -> list[Link]:  # noqa: ANN401
    """The active projects the person has a role in, in the order people browse them (by
    team, then name), the first few of them: and the one they're on, if it's further down."""
    found = (
        access.readable_projects(user)
        .filter(archived_at__isnull=True)
        .order_by("team__name", "name", "pk")
        .values_list("name", "slug")
    )
    shown = list(found[: raw["shown"]])
    here = path.removeprefix(raw["href"]).split("/", 1)[0] if path.startswith(raw["href"]) else ""
    if here and here not in {slug for _, slug in shown}:
        shown.extend(found.filter(slug=here))
    return [
        Link(name, f"{raw['href']}{slug}/", prefixes=(f"{raw['href']}{slug}/",))
        for name, slug in shown
    ]

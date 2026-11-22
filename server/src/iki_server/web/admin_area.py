"""The admin area (ADR 0176): a frame of its own, with a sidebar of the organization's
settings, its compute and costs, and what auditors read.

``navigation.json``'s ``admin`` lists the sidebar's groups, and both the server's
``web/admin_base.html`` and the web app's ``AdminLayout`` (``web/app/src/admin.tsx``)
render that one list, so the two can't drift apart. Each group has a ``label`` and its
``items``; each item has:

- ``label`` and ``href``;
- ``requires``: the ones of ``/me``'s ``can`` (:func:`iki_server.org.permissions.capabilities`)
  it's offered for: having any one is enough;
- ``current``: the paths it's the current item for, by prefix (its ``href`` if left out);
- ``app``: the web app routes it, so its links there don't load a page;
- ``icon``: its icon's SVG paths, on a 24-unit grid.

Every page of the area is under ``/admin/``, and ``/admin/`` itself opens the first one
the person may open. Each page still checks its own permission; the sidebar only leaves
out what the person can't open.
"""

import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET

from iki_server.org import permissions
from iki_server.web.navigation import SOURCE


@dataclass(frozen=True)
class Item:
    label: str
    href: str
    requires: tuple[str, ...]
    """It's offered to whoever has any one of these."""
    current: tuple[str, ...]
    app: bool
    icon: tuple[str, ...]


@dataclass(frozen=True)
class Group:
    label: str
    items: tuple[Item, ...]


@cache
def groups() -> tuple[Group, ...]:
    """Every group of the sidebar, with every item, whoever is looking."""
    raw: list[dict[str, Any]] = json.loads(SOURCE.read_text(encoding="utf-8"))["admin"]["groups"]
    return tuple(
        Group(
            label=group["label"],
            items=tuple(
                Item(
                    label=item["label"],
                    href=item["href"],
                    requires=tuple(item["requires"]),
                    current=tuple(item.get("current", [item["href"]])),
                    app=bool(item.get("app", False)),
                    icon=tuple(item["icon"]),
                )
                for item in group["items"]
            ),
        )
        for group in raw
    )


@dataclass(frozen=True)
class Link:
    label: str
    href: str
    current: bool
    icon: tuple[str, ...]


@dataclass(frozen=True)
class Section:
    label: str
    links: tuple[Link, ...]


def navigation(user: Any, path: str) -> list[Section]:  # noqa: ANN401
    """The groups, with the items this person may open; a group with none is left out.
    ``current`` marks the item ``path`` is in."""
    can = permissions.capabilities(user)
    sections = []
    for group in groups():
        links = tuple(
            Link(item.label, item.href, path.startswith(item.current), item.icon)
            for item in group.items
            if any(can[need] for need in item.requires)
        )
        if links:
            sections.append(Section(group.label, links))
    return sections


@require_GET
@login_required
def home(request: HttpRequest) -> HttpResponse:
    """``/admin/``: the first page of the area this person may open."""
    for section in navigation(request.user, request.path):
        return redirect(section.links[0].href)
    raise PermissionDenied

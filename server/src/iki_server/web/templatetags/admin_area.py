"""``{% admin_area as admin %}``: the admin area's sidebar, for ``web/admin_base.html``."""

from dataclasses import dataclass
from typing import Any

from django import template

from iki_server.web import admin_area as area
from iki_server.web.navigation import icon

register = template.Library()


@dataclass(frozen=True)
class AdminArea:
    sections: list[area.Section]
    current: area.Link | None
    """The item the page is in: the breadcrumb's last part."""
    icons: dict[str, tuple[str, ...]]
    """The drawer's menu and close buttons' (``navigation.json``'s ``icons``)."""


@register.simple_tag(takes_context=True)
def admin_area(context: dict[str, Any]) -> AdminArea:
    request = context["request"]
    sections = area.navigation(request.user, request.path)
    current = next((link for s in sections for link in s.links if link.current), None)
    return AdminArea(sections, current, {name: icon(name) for name in ("menu", "close")})


@register.filter
def initials(name: str) -> str:
    """The account's badge at the foot of the sidebar: "Rosalind Franklin" is "RF"."""
    words = [w for w in str(name).split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:1] + words[1:][-1:]).upper()

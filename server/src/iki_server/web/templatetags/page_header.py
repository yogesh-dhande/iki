"""``{% page_header as page_header %}``: a server-rendered page's header (ADR 0176), as the
web app's ``PageHeader`` draws it: its breadcrumb and actions in the bar across the top of
the page, then its title, with what it is in one line beside it.

A page gives its parts as blocks, and its frame (``base.html``, ``web/admin_base.html``)
reads them with this tag and draws them in ``web/_page_top.html`` and
``web/_page_heading.html``:

- ``title``: its name, in the browser's tab, its breadcrumb and its ``<h1>``.
- ``heading``: its ``<h1>``, where that isn't just its title, as with a badge after it.
- ``crumbs``: its breadcrumb's items, ``<li>``s, the page itself last; its title alone if
  left out. In the admin area they follow the frame's own "Admin / item", for a page under
  the item: "Admin / People / Ada Lovelace".
- ``actions``: its buttons, primary last.
- ``description``: one line beside its title. Cut short where it doesn't fit, it's whole in
  a toggletip (``iki.js``).

A block is read rather than drawn where it stands because a page's actions are in two
places: the bar, and under its title on a phone. The web app moves them there when the
screen is a phone's; a page drawn once by the server has both, and the stylesheet shows the
one for the screen.
"""

from dataclasses import dataclass
from typing import Any

from django import template
from django.template.loader_tags import BLOCK_CONTEXT_KEY
from django.utils.safestring import SafeString, mark_safe

register = template.Library()


@dataclass(frozen=True)
class PageHeader:
    title: SafeString
    heading: SafeString
    crumbs: SafeString
    actions: SafeString
    description: SafeString


@register.simple_tag(takes_context=True)
def page_header(context: template.Context) -> PageHeader:
    title = _block(context, "title")
    return PageHeader(
        title=title,
        heading=_block(context, "heading") or title,
        crumbs=_block(context, "crumbs"),
        actions=_block(context, "actions"),
        description=_block(context, "description"),
    )


def _block(context: template.Context, name: str) -> SafeString:
    """The page's block ``name``, drawn, or nothing if it has none or it's empty. The page's
    own, where it gives one, else its frame's."""
    blocks: Any = context.render_context.get(BLOCK_CONTEXT_KEY)
    block = blocks.get_block(name) if blocks is not None else None
    if block is None:
        return SafeString()
    with context.push():
        drawn: str = block.nodelist.render(context)
    # The template's own output, escaped as it rendered: only its ends are trimmed.
    return mark_safe(drawn.strip())  # noqa: S308

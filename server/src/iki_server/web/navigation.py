"""The header's main navigation, the same on server-rendered pages and in the web app.

``navigation.json`` lists its items, and both render that one list: the server's
``base.html`` through :func:`main_navigation`, and the web app's layout, which imports
the file (``web/app/src/layout.tsx``). So the two headers can't drift apart. Each item
has:

- ``label`` and ``href``;
- ``requires``: the one of ``/me``'s ``can`` (:func:`iki_server.org.permissions.capabilities`)
  it needs, if any. Both read the same rules;
- ``current``: the paths it's the current section for, by prefix (its ``href`` if left
  out);
- ``app``: the web app routes it, so its links there don't load a page.
"""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from iki_server.org import permissions

SOURCE = Path(__file__).with_name("navigation.json")


@dataclass(frozen=True)
class Item:
    label: str
    href: str
    requires: str | None
    current: tuple[str, ...]
    app: bool


@cache
def items() -> tuple[Item, ...]:
    raw: list[dict[str, Any]] = json.loads(SOURCE.read_text(encoding="utf-8"))["main"]
    return tuple(
        Item(
            label=item["label"],
            href=item["href"],
            requires=item.get("requires"),
            current=tuple(item.get("current", [item["href"]])),
            app=bool(item.get("app", False)),
        )
        for item in raw
    )


@dataclass(frozen=True)
class Link:
    label: str
    href: str
    current: bool


def main_navigation(user: Any, path: str) -> list[Link]:  # noqa: ANN401
    """The items this person may open, and which one ``path`` is in."""
    can = permissions.capabilities(user)
    return [
        Link(item.label, item.href, current=path.startswith(item.current))
        for item in items()
        if item.requires is None or can[item.requires]
    ]

"""The trees of Iki's own plugins, as Iki's repository has had them (ADR 0114).

A deployment that can't reach Iki's repository fetches Iki's plugins from a
mirror. A commit's hash proves what it holds, but not that Iki made it: a
mirror could serve a commit of its own. So ``iki deploy build`` accepts a
plugin from a mirror only if its directory's git tree is one listed here,
as Go checks a module from a proxy against its checksum database and
Terraform checks a provider from a mirror against its lockfile's hashes.

The list travels in this package, so it reaches an air-gapped site the way
the rest of Iki does. ``make first-party`` adds each plugin's current tree,
and a test fails while one is missing, so every commit of Iki's repository
lists the trees its plugins have. Trees are only ever added.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files

__all__ = ["FILENAME", "trees"]

#: The list, beside this module.
FILENAME = "first_party_trees.json"


@cache
def _all() -> dict[str, tuple[str, ...]]:
    data = json.loads(files(__package__).joinpath(FILENAME).read_text("utf-8"))
    return {name: tuple(listed) for name, listed in data["trees"].items()}


def trees(plugin: str) -> frozenset[str]:
    """Every git tree Iki's repository has had for ``plugin`` (``iki/dna``), by hash."""
    return frozenset(_all().get(plugin, ()))

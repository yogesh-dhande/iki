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
from pathlib import Path

from iki_spec import naming
from iki_spec.yamlsource import read_documents

__all__ = ["FILENAME", "catalog", "trees"]

#: The list, beside this module.
FILENAME = "first_party_trees.json"


@cache
def _all() -> dict[str, tuple[str, ...]]:
    data = json.loads(files(__package__).joinpath(FILENAME).read_text("utf-8"))
    return {name: tuple(listed) for name, listed in data["trees"].items()}


def trees(plugin: str) -> frozenset[str]:
    """Every git tree Iki's repository has had for ``plugin`` (``iki/dna``), by hash."""
    return frozenset(_all().get(plugin, ()))


def catalog(plugins_dir: Path) -> dict[str, Path]:
    """The installable plugins in an Iki checkout. The server supplies core separately."""
    directories = {
        name: plugins_dir / naming.short_name(name)
        for name in sorted(naming.FIRST_PARTY - {naming.CORE})
    }
    expected = {directory / "plugin.yaml" for directory in directories.values()}
    found = set(plugins_dir.rglob("plugin.yaml"))
    if missing := expected - found:
        raise ValueError(f"Missing first-party manifest: {min(missing)}")
    if extra := found - expected:
        raise ValueError(f"Unexpected plugin manifest in the first-party catalog: {min(extra)}")
    for name, directory in directories.items():
        manifest = directory / "plugin.yaml"
        if directory.is_symlink() or manifest.is_symlink():
            raise ValueError(f"The first-party manifest {manifest} must be in its own directory.")
        docs, problems = read_documents(manifest, str(manifest))
        data = docs[0].data if len(docs) == 1 else None
        if problems or not isinstance(data, dict) or data.get("name") != name:
            raise ValueError(f"{manifest} must declare the first-party plugin {name}.")
    return directories

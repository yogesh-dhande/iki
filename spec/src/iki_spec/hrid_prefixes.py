"""HRID prefixes across a deployment (ADR 0033): its overrides, and the prefixes Iki keeps.

A plugin's prefix for a type is a default. A deployment's ``plugins.lock.yaml`` may give
a type another, ``hrids: { iki/reagents.Buffer: IBUF }``, and the build uses it in place
of the plugin's everywhere: the check that no two types share one, the compiled
manifest, the server's HRID sequences. So when two plugins that don't know each other
take one prefix, or a release of Iki's plugins takes a lab's, the lab settles it in its
lockfile, without changing either plugin.

Iki publishes the prefixes it uses, and those it reserves for first-party types on its
roadmap, in ``reserved_hrid_prefixes.json`` (``scripts/reserved_hrid_prefixes.py``
writes it). Core's are Iki's alone: no plugin may take one, and a deployment can't move
them. A lab's plugin that takes one of Iki's plugins', or a reserved one, is warned.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from iki_spec import naming
from iki_spec.model import EntityType, Plugin

__all__ = [
    "FILENAME",
    "LOCKFILE_KEY",
    "PATTERN",
    "Reserved",
    "effective",
    "reserved",
    "settle",
    "suggest",
    "with_prefixes",
]

#: The list, beside this module.
FILENAME = "reserved_hrid_prefixes.json"
#: The lockfile's key for a deployment's overrides.
LOCKFILE_KEY = "hrids"
#: What a prefix is: 2 to 8 capital letters or digits, starting with a letter.
PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")


@dataclass(frozen=True)
class Reserved:
    """The prefixes Iki keeps, each with what uses it."""

    #: Core's types and the records the server numbers itself: Iki's alone.
    core: Mapping[str, str]
    #: The types of Iki's fetched plugins.
    plugins: Mapping[str, str]
    #: First-party types the roadmap names that aren't built yet.
    planned: Mapping[str, str]

    def kept_for(self, prefix: str) -> str | None:
        """What a lab plugin's type with ``prefix`` would collide with, if Iki's plugins
        use or reserve it: such as ``iki/reagents.Buffer``, or ``iki/antibody.FcFusion
        (planned)``."""
        if prefix in self.plugins:
            return self.plugins[prefix]
        if prefix in self.planned:
            return f"{self.planned[prefix]}, which is planned"
        return None

    def all(self) -> frozenset[str]:
        return frozenset({*self.core, *self.plugins, *self.planned})


@cache
def reserved() -> Reserved:
    data = json.loads(files(__package__).joinpath(FILENAME).read_text("utf-8"))
    return Reserved(core=data["core"], plugins=data["plugins"], planned=data["planned"])


def effective(entity_type: EntityType, hrids: Mapping[str, str] | None) -> str | None:
    """The prefix a type's HRIDs get in a deployment: its override, or its plugin's.
    None for a type without HRIDs (an abstract one)."""
    if entity_type.abstract or not entity_type.hrid_prefix:
        return None
    return (hrids or {}).get(entity_type.qualified_name, entity_type.hrid_prefix)


def with_prefixes(plugin: Plugin, hrids: Mapping[str, str] | None) -> Plugin:
    """``plugin`` with the deployment's prefixes in place of its own."""
    changed = {
        name: dataclasses.replace(t, hrid_prefix=hrids[name])
        for name, t in plugin.types.items()
        if hrids and name in hrids and not t.abstract and hrids[name] != t.hrid_prefix
    }
    if not changed:
        return plugin
    return dataclasses.replace(plugin, types={**plugin.types, **changed})


def suggest(qualified: str, prefix: str, taken: Collection[str]) -> str:
    """A prefix for ``qualified`` in place of ``prefix`` that nothing in ``taken`` (nor
    anything Iki keeps) has: its namespace's initial and the prefix, such as ``ABUF``
    for ``acme/reagents.Buffer``, or the prefix with a digit."""
    namespace = naming.split(qualified.partition(".")[0])[0]
    letters = re.sub(r"[^A-Z0-9]", "", namespace.upper())
    used = {*taken, *reserved().all()}
    candidates = [(letters[:n] + prefix)[:8] for n in (1, 2, 3) if letters[:n]]
    candidates += [f"{prefix[:7]}{digit}" for digit in range(2, 10)]
    for candidate in candidates:
        if PATTERN.match(candidate) and candidate not in used:
            return candidate
    return f"{prefix[:6]}X1"


def settle(first: str, second: str, prefix: str, taken: Collection[str]) -> str:
    """The hint for two types that share ``prefix``: the override, in a deployment's
    ``plugins.lock.yaml``, that gives one of them another. It moves a lab's type before
    one of Iki's, and never one of core's."""
    pick = min((first, second), key=lambda q: (_kept(q), q != second))
    return (
        f"Give one of them its own prefix in the deployment's `plugins.lock.yaml`: "
        f"`{LOCKFILE_KEY}: {{ {pick}: {suggest(pick, prefix, taken)} }}`. Or, in a plugin "
        "you own, change its `hrid.prefix` before it has records."
    )


def _kept(what: str) -> int:
    """How much a type is Iki's: 0 a lab's, 1 one of Iki's plugins', 2 core's (or a record
    the server numbers), which can't move."""
    plugin = what.partition(".")[0]
    if "." not in what or plugin == naming.CORE:
        return 2
    return 1 if plugin in naming.FIRST_PARTY else 0

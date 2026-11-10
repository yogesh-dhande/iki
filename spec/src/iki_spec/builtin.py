"""The core plugin, which every deployment has and which ships with Iki.

Its manifests are in ``iki_spec/core``, written and checked like any
plugin's. Deployments don't list it in ``plugins.lock.yaml``, and other
plugins use its types, such as ``core.Entity``, without declaring a
dependency. Its version is Iki's.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from pathlib import Path

from iki_spec.model import Plugin

__all__ = ["CORE_DIRECTORY", "core_plugin"]

CORE_DIRECTORY = Path(str(files("iki_spec").joinpath("core")))


@cache
def core_plugin() -> Plugin:
    from iki_spec.validate import load_plugin

    result = load_plugin(CORE_DIRECTORY, core=True)
    errors = [p for p in result.problems if p.is_error]
    if result.plugin is None or errors:
        detail = "; ".join(f"{p.file}: {p.message}" for p in errors)
        raise RuntimeError(f"Iki's core plugin is invalid: {detail}")
    return result.plugin

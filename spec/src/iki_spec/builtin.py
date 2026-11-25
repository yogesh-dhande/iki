"""The core plugin, which every deployment has and which ships with Iki.

Its manifests are in ``iki_spec/core``, written and checked like any
plugin's. Deployments don't list it in ``plugins.lock.yaml``, and other
plugins use its types, such as ``iki/core.Entity``, without declaring a
dependency. Its version is Iki's.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from importlib.resources import files
from pathlib import Path

from iki_spec.model import Plugin

__all__ = ["CORE_DIRECTORY", "core_plugin", "shipped_core", "using_core"]

CORE_DIRECTORY = Path(str(files("iki_spec").joinpath("core")))


_override: ContextVar[Plugin | None] = ContextVar("core_plugin_override", default=None)


def core_plugin() -> Plugin:
    return _override.get() or shipped_core()


@contextmanager
def using_core(directory: Path | None) -> Iterator[None]:
    """Use the core manifests from an earlier commit in an upgrade probe."""
    if directory is None:
        yield
        return
    token = _override.set(_load_core(directory))
    try:
        yield
    finally:
        _override.reset(token)


@cache
def shipped_core() -> Plugin:
    return _load_core(CORE_DIRECTORY)


def _load_core(directory: Path) -> Plugin:
    from iki_spec.validate import load_plugin

    result = load_plugin(directory, core=True)
    errors = [p for p in result.problems if p.is_error]
    if result.plugin is None or errors:
        detail = "; ".join(f"{p.file}: {p.message}" for p in errors)
        raise RuntimeError(f"Iki's core plugin is invalid: {detail}")
    return result.plugin

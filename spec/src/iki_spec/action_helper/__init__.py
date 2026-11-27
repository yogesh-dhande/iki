"""Iki's action helper, ``iki_action.py``, and the check of a plugin's copy (ADR 0287).

A plugin's scripts read their run and write their output with ``iki_action.py``, a copy
of :data:`SOURCE` vendored beside them. A copy runs in images Iki doesn't build, so it is
never fetched at run time: ``iki plugin vendor`` writes it, and ``iki plugin validate``
checks that every copy in a plugin is a published version, byte for byte. A published
version never changes; a change to the helper is a new version, added to
:data:`PUBLISHED` with its digest, and the old ones stay valid.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path as FilePath
from typing import TYPE_CHECKING

from iki_spec.problems import Severity

if TYPE_CHECKING:
    from iki_spec import validate as _validate

__all__ = [
    "CURRENT",
    "FILE_NAME",
    "PUBLISHED",
    "SOURCE",
    "check",
    "copies",
    "source",
    "version_of",
]

#: The helper's name, where a plugin keeps its copy: ``scripts/iki_action.py``.
FILE_NAME = "iki_action.py"
#: This Iki's version of the helper.
SOURCE = FilePath(__file__).with_name(FILE_NAME)
#: Every published version's SHA-256, by version.
PUBLISHED: dict[str, str] = {
    "1.0.0": "6a0f9a6f603863724a74071d3edefbb596dc04dd110887f4820aad036f075e52",
}
#: The version :data:`SOURCE` is.
CURRENT = "1.0.0"
#: Directories a plugin's copy is never looked for in: tools' and packages' own.
_SKIPPED = {"node_modules", "__pycache__", "web"}


def source() -> bytes:
    """This Iki's helper, as ``iki plugin vendor`` writes it."""
    return SOURCE.read_bytes()


def version_of(data: bytes) -> str | None:
    """The published version ``data`` is, if it is one."""
    digest = hashlib.sha256(data).hexdigest()
    return next((v for v, d in PUBLISHED.items() if d == digest), None)


def copies(root: FilePath) -> list[FilePath]:
    """Each copy of the helper in the plugin at ``root``."""
    found: list[FilePath] = []
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if not n.startswith(".") and n not in _SKIPPED)
        if FILE_NAME in files:
            found.append(FilePath(directory) / FILE_NAME)
    return found


def check(semantics: _validate._Semantics) -> None:
    """Every copy of the helper in the plugin is a published version: an edited copy is
    refused, and an earlier version is warned of."""
    root = semantics.loaded.directory
    for copy in copies(root):
        where = copy.relative_to(root).as_posix()
        if copy.is_symlink() or not copy.is_file():
            semantics.add(
                "action-helper-changed",
                f"`{where}` isn't a file: keep Iki's action helper as a copy of its own.",
                file=where,
                hint="Run `iki plugin vendor .` to write it.",
            )
            continue
        version = version_of(copy.read_bytes())
        if version is None:
            semantics.add(
                "action-helper-changed",
                f"`{where}` isn't a version of Iki's action helper that Iki published: "
                "it was changed after it was vendored.",
                file=where,
                hint="Keep your own code in other files, and run `iki plugin vendor .` "
                "to write the helper again.",
            )
        elif version != CURRENT:
            semantics.add(
                "action-helper-outdated",
                f"`{where}` is Iki's action helper {version}; this Iki's is {CURRENT}.",
                file=where,
                severity=Severity.WARNING,
                hint="Run `iki plugin vendor .` to update it.",
            )

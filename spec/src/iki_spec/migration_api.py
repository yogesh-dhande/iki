"""What a plugin's migrations may use of Iki, and the checks of them (ADR 0296).

A plugin migration imports one module of Iki's, :data:`OPERATIONS`, whose names
(:func:`names`) Iki keeps working across Django upgrades. It depends on Iki's own
migrations only through the release anchors in :data:`ANCHORS_APP` (:func:`anchors`),
which Iki never removes, renames or squashes. Both are listed, with the release each
came in, in ``migration_api.json``, which the manifest spec prints.

:func:`check` reads a plugin's ``migrations/`` without Django or a database, so
``iki plugin validate``, ``build`` and ``iki deploy build`` refuse a migration that
uses anything else, or that needs a newer Iki than the plugin's ``iki:`` admits.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

from iki_spec import naming, releases
from iki_spec.problems import Location, Problem

#: The one module of Iki's a plugin migration imports.
OPERATIONS = "iki_server.plugins.operations"
#: The app whose migrations are the release anchors.
ANCHORS_APP = "iki_anchors"
#: How ``iki plugin makemigrations`` imports :data:`OPERATIONS`.
ALIAS = "iki"

Release = tuple[int, int]

_ANCHOR_NAME = re.compile(r"[0-9]{4}_iki_([0-9]+)_([0-9]+)")


@dataclass(frozen=True)
class Anchor:
    """A minor release's anchor: after the last migration of each of Iki's apps in it."""

    release: Release
    migration: str
    #: Each of Iki's apps' last migration in the release, by app label.
    after: Mapping[str, str]

    @property
    def version(self) -> str:
        return ".".join(map(str, self.release))

    @property
    def key(self) -> tuple[str, str]:
        return (ANCHORS_APP, self.migration)


@cache
def _table() -> dict[str, object]:
    text = resources.files("iki_spec").joinpath("migration_api.json").read_text("utf-8")
    data: dict[str, object] = json.loads(text)
    return data


def _release(text: str) -> Release:
    major, minor, _ = releases.release(text)
    return major, minor


@cache
def anchors() -> tuple[Anchor, ...]:
    """Every release's anchor, oldest first."""
    rows = _table()["anchors"]
    assert isinstance(rows, list)  # noqa: S101 - our own table
    return tuple(
        Anchor(_release(row["release"]), row["migration"], dict(row["after"])) for row in rows
    )


@cache
def names() -> dict[str, Release]:
    """Each name of :data:`OPERATIONS` a migration may use, with the release it came in."""
    rows = _table()["names"]
    assert isinstance(rows, dict)  # noqa: S101 - our own table
    return {name: _release(since) for name, since in rows.items()}


def anchor_named(migration: str) -> Anchor | None:
    return next((a for a in anchors() if a.migration == migration), None)


def anchor_of(version: str) -> Anchor | None:
    """The anchor of the minor release ``version`` is in, if Iki has one."""
    wanted = _release(version)
    return next((a for a in anchors() if a.release == wanted), None)


def oldest(requirement: str | None) -> tuple[int, int, int] | None:
    """The oldest release a plugin's ``iki:`` admits; with no ``iki:``, this Iki's."""
    if requirement is None:
        return releases.release(releases.iki())
    try:
        return releases.lowest(requirement)
    except ValueError:
        return None


def anchor_for(requirement: str | None) -> Anchor | None:
    """The anchor a migration of a plugin with this ``iki:`` depends on: that of the oldest
    release it admits. None when it admits a release older than the first anchor."""
    first = oldest(requirement)
    if first is None:
        return None
    fitting = [a for a in anchors() if a.release <= first[:2]]
    return fitting[-1] if fitting else None


def anchor_dependencies(version: str) -> list[tuple[str, str]]:
    """What a release's anchor migration depends on: each app's last migration in that
    release, and the anchor before it."""
    anchor = anchor_of(version)
    if anchor is None:
        raise LookupError(f"Iki has no anchor for {version}.")
    before = [a for a in anchors() if a.release < anchor.release]
    return sorted(anchor.after.items()) + ([before[-1].key] if before else [])


def _is_stdlib(module: str) -> bool:
    return module.split(".")[0] in sys.stdlib_module_names


# --- Checking a plugin's migrations ------------------------------------------------------------


def check(directory: Path, plugin: str, requirement: str | None) -> list[Problem]:
    """The problems with the migrations in ``directory / "migrations"`` of ``plugin``, whose
    ``iki:`` is ``requirement``."""
    folder = directory / "migrations"
    if not folder.is_dir():
        return []
    files = sorted(
        p for p in folder.glob("*.py") if not p.name.startswith(("_", "~")) and p.is_file()
    )
    own = {p.stem for p in files}
    problems: list[Problem] = []
    for path in files:
        problems += _Migration(path, f"migrations/{path.name}", plugin, requirement, own).check()
    return problems


class _Migration:
    def __init__(
        self, path: Path, file: str, plugin: str, requirement: str | None, own: set[str]
    ) -> None:
        self.path = path
        self.file = file
        self.label = naming.app_label(plugin)
        self.requirement = requirement
        self.oldest = oldest(requirement)
        self.own = own
        self.problems: list[Problem] = []
        #: Names bound to the operations module, and to names imported from it.
        self.modules: set[str] = set()
        self.imported: dict[str, str] = {}
        #: Names bound to what the migration mustn't import (``models``: ``django.db.models``),
        #: and modules imported without a name (``import django.db.models.lookups``), each
        #: with the problem that refuses its import.
        self.bound: dict[str, tuple[str, int]] = {}
        self.plain: list[tuple[str, int]] = []

    def add(self, code: str, message: str, node: ast.AST | None, hint: str | None = None) -> int:
        line = getattr(node, "lineno", None)
        location = Location(line, getattr(node, "col_offset", 0) + 1) if line else None
        self.problems.append(
            Problem(code=code, message=message, file=self.file, location=location, hint=hint)
        )
        return len(self.problems) - 1

    def check(self) -> list[Problem]:
        try:
            tree = ast.parse(self.path.read_text("utf-8"), filename=self.file)
        except (SyntaxError, UnicodeDecodeError, OSError) as error:
            line = getattr(error, "lineno", None)
            column = (getattr(error, "offset", None) or 1) - 1
            self.add(
                "migration-syntax",
                f"{self.path.name} isn't a Python module Iki can read: {error}.",
                ast.Pass(lineno=line, col_offset=column) if line else None,
            )
            return self.problems
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                self._import(node)
        self._uses(tree)
        self._migration(tree)
        # In the file's order, as a reader meets them (a sort that keeps ties as they are).
        return sorted(
            self.problems,
            key=lambda p: (p.location.line, p.location.column) if p.location else (0, 0),
        )

    # Imports --------------------------------------------------------------------------------

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == OPERATIONS:
                    self.modules.add(alias.asname or alias.name)
                elif not _is_stdlib(alias.name):
                    refused = self._refuse(node, alias.name)
                    if alias.asname:
                        self.bound[alias.asname] = (alias.name, refused)
                    else:
                        self.plain.append((alias.name, refused))
            return
        module = node.module or ""
        if node.level:
            self.add(
                "migration-import",
                f"`from {'.' * node.level}{module} import …`: a plugin migration imports only "
                f"`{OPERATIONS}` and Python's standard library.",
                node,
                hint="Keep what a migration needs in the migration file itself, so it reads "
                "the same for as long as the plugin is installed.",
            )
            return
        if module == OPERATIONS:
            for alias in node.names:
                if alias.name == "*":
                    self.add(
                        "migration-shape",
                        f"`from {OPERATIONS} import *` hides which of Iki's operations the "
                        "migration uses.",
                        node,
                        hint=f"Write `from iki_server.plugins import operations as {ALIAS}`.",
                    )
                    continue
                self.imported[alias.asname or alias.name] = alias.name
                self._name(alias.name, node)
            return
        package, _, leaf = OPERATIONS.rpartition(".")
        if module == package and all(alias.name == leaf for alias in node.names):
            self.modules.update(alias.asname or alias.name for alias in node.names)
            return
        if _is_stdlib(module):
            return
        refused = self._refuse(node, module)
        for alias in node.names:
            self.bound[alias.asname or alias.name] = (f"{module}.{alias.name}", refused)

    def _refuse(self, node: ast.AST, module: str) -> int:
        return self.add(
            "migration-import",
            f"The migration imports `{module}`: a plugin migration imports only `{OPERATIONS}`, "
            "whose operations Iki keeps working across its releases and Django's, and "
            "Python's standard library.",
            node,
        )

    def _uses(self, tree: ast.AST) -> None:
        """Checks each name used from the operations module, and says, for each import it
        mustn't have, which of the module's names stand for what's used of it."""
        inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        used: dict[int, list[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute | ast.Name) or id(node) in inner:
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            dotted = _dotted(node)
            if dotted is None:
                continue
            head, _, rest = dotted.partition(".")
            if dotted.startswith(OPERATIONS + "."):
                self._name(dotted[len(OPERATIONS) + 1 :].split(".")[0], node)
            elif head in self.modules and rest:
                self._name(rest.split(".")[0], node)
            elif head in self.bound:
                module, refused = self.bound[head]
                used.setdefault(refused, []).append(f"{module}.{rest}" if rest else module)
            elif (refused_plain := self._plain(dotted)) is not None:
                used.setdefault(refused_plain, []).append(dotted)
            elif head == "iki_server":
                self.add(
                    "migration-import",
                    f"The migration uses `{dotted}`: of Iki's modules, a plugin migration uses "
                    f"only `{OPERATIONS}`.",
                    node,
                )
        for refused, names_used in used.items():
            hints = []
            for full in dict.fromkeys(names_used):
                wrapper = _wrapper_for(full)
                hints.append(
                    f"`{full}` is `{ALIAS}.{wrapper}`"
                    if wrapper is not None
                    else f"`{full}` has none: {_no_counterpart(full)}"
                )
            problem = self.problems[refused]
            self.problems[refused] = Problem(
                code=problem.code,
                message=problem.message,
                file=problem.file,
                location=problem.location,
                hint=f"Import `from iki_server.plugins import operations as {ALIAS}` instead. "
                + "; ".join(hints[:6])
                + ("; …" if len(hints) > 6 else "")
                + ".",
            )

    def _plain(self, dotted: str) -> int | None:
        """The import refused for ``import a.b`` that ``a.b.c`` is used through, if any."""
        found = [(m, i) for m, i in self.plain if dotted == m or dotted.startswith(m + ".")]
        return max(found, key=lambda pair: len(pair[0]))[1] if found else None

    def _name(self, name: str, node: ast.AST) -> None:
        since = names().get(name)
        if since is None:
            self.add(
                "migration-unknown-name",
                f"`{name}` isn't one of the operations Iki keeps for plugin migrations.",
                node,
                hint="The manifest spec lists them, and `iki plugin makemigrations` writes "
                "only those.",
            )
            return
        if self.requirement is not None and self.oldest is not None and self.oldest < (*since, 0):
            version = _version(since)
            self.add(
                "migration-iki-range",
                f"`{ALIAS}.{name}` needs Iki {version} or later, and `iki:` "
                f"({self.requirement.strip()}) admits {_version(self.oldest)}.",
                node,
                hint=f'Raise `iki:` to `">={version}"`.',
            )

    # The Migration class --------------------------------------------------------------------

    def _migration(self, tree: ast.Module) -> None:
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Migration"]
        if not classes:
            self.add(
                "migration-shape",
                f"{self.path.name} has no `Migration` class.",
                None,
                hint=f"A migration is `class Migration({ALIAS}.Migration)`, as "
                "`iki plugin makemigrations` writes it.",
            )
            return
        cls = classes[0]
        if len(cls.bases) != 1 or not self._is_operations_name(cls.bases[0], "Migration"):
            self.add(
                "migration-shape",
                f"`Migration` should be `class Migration({ALIAS}.Migration)`.",
                cls,
            )
        dependencies: ast.expr | None = None
        for statement in cls.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and statement.targets[0].id == "dependencies"
            ):
                dependencies = statement.value
        pairs = self._pairs(dependencies) if dependencies is not None else []
        if pairs is None:
            return
        anchored = False
        mine = False
        for (app, name), node in pairs:
            if app == self.label:
                mine = True
                if name not in self.own:
                    self.add(
                        "migration-shape",
                        f"It depends on {name}, which isn't in migrations/.",
                        node,
                    )
            elif app == ANCHORS_APP:
                anchored = True
                self._anchor(name, node)
            elif naming.plugin_of_label(app) is None or app == naming.app_label(naming.CORE):
                self._core(app, name, node)
        if not mine and not anchored:
            suggested = anchor_for(self.requirement)
            self.add(
                "migration-shape",
                f"{self.path.name} is the plugin's first migration and depends on none of "
                "Iki's anchors, so it may run before the tables it needs.",
                cls,
                hint=(
                    f"Add `{suggested.key!r}` to its dependencies: the anchor of the oldest "
                    "release `iki:` admits."
                    if suggested
                    else None
                ),
            )

    def _is_operations_name(self, node: ast.expr, name: str) -> bool:
        dotted = _dotted(node)
        if dotted is None:
            return False
        head, _, rest = dotted.partition(".")
        return (
            (head in self.modules and rest == name)
            or dotted == f"{OPERATIONS}.{name}"
            or self.imported.get(dotted) == name
        )

    def _pairs(self, node: ast.expr) -> list[tuple[tuple[str, str], ast.expr]] | None:
        items = node.elts if isinstance(node, ast.List | ast.Tuple) else None
        pairs: list[tuple[tuple[str, str], ast.expr]] = []
        for item in items or []:
            if (
                isinstance(item, ast.Tuple)
                and len(item.elts) == 2
                and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in item.elts)
            ):
                app, name = (str(e.value) for e in item.elts if isinstance(e, ast.Constant))
                pairs.append(((app, name), item))
            else:
                items = None
                break
        if items is None:
            self.add(
                "migration-shape",
                "List the migration's `dependencies` as `(app, migration)` pairs of strings, "
                "so they can be checked without running it.",
                node,
            )
            return None
        return pairs

    def _anchor(self, name: str, node: ast.expr) -> None:
        anchor = anchor_named(name)
        if anchor is None:
            match = _ANCHOR_NAME.fullmatch(name)
            which = f"Iki {match.group(1)}.{match.group(2)}'s anchor" if match else name
            self.add(
                "migration-newer-iki",
                f"It depends on {which} ({ANCHORS_APP} {name}), which this Iki "
                f"({releases.iki()}) doesn't have: the migration was made with a newer Iki.",
                node,
                hint="Use an Iki that has it, or a release of the plugin made for this one.",
            )
            return
        if self.requirement is None or self.oldest is None:
            return
        if self.oldest < (*anchor.release, 0):
            self.add(
                "migration-iki-range",
                f"It depends on Iki {anchor.version}'s anchor, and `iki:` "
                f"({self.requirement.strip()}) admits {_version(self.oldest)}, which hasn't "
                "got it.",
                node,
                hint=f'Raise `iki:` to `">={anchor.version}"`, or make the migration with the '
                "Iki its oldest release is.",
            )

    def _core(self, app: str, name: str, node: ast.expr) -> None:
        anchor = anchor_for(self.requirement)
        self.add(
            "migration-core-dependency",
            f"It depends on {app} {name}, one of Iki's own migrations, which a later Iki may "
            "squash or rename.",
            node,
            hint=(
                f"Depend on `{anchor.key!r}` instead: the anchor of the oldest release `iki:` "
                "admits, which `iki plugin makemigrations` writes."
                if anchor
                else f"Depend on one of Iki's anchors ({ANCHORS_APP}), and raise `iki:` to a "
                "release that has one."
            ),
        )


def _version(release: tuple[int, ...]) -> str:
    return ".".join(map(str, release))


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


#: What Django's names are as Iki's, where the last part of the name doesn't say.
_RENAMED = {
    "AUTH_USER_MODEL": "USER",
}
#: Why a name has no counterpart.
_NONE = {
    "swappable_dependency": "Iki's anchors cover the user model",
    "RunPython": "change stored values with MigrateValues, which gives each record a revision",
    "RunSQL": "change stored values with MigrateValues, which gives each record a revision",
}


def _wrapper_for(full: str) -> str | None:
    last = full.rsplit(".", 1)[-1]
    last = _RENAMED.get(last, last)
    return last if last in names() else None


def _no_counterpart(full: str) -> str:
    return _NONE.get(full.rsplit(".", 1)[-1], "it isn't part of Iki's migration API")

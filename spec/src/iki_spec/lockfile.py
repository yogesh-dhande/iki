"""A deployment's plugins: ``plugins.lock.yaml`` (spec ``iki/v1``).

Each deployment's config repository has a lockfile listing every plugin it
installs, each pinned to a git commit::

    apiVersion: iki/v1
    kind: PluginLock
    plugins:
      - name: dna
        git: https://github.com/acme/iki-plugins.git
        path: dna
        sha: 4f1c2b9e0d6a7c3b8e5f1a2d9c0b7e6f5a4d3c2b
        state: active
        settings: settings/dna.yaml

``iki deploy build`` fetches each plugin at its commit, checks it with the
others and with its settings, and compiles them for the server image. A
commit, unlike a branch or tag, can't change under a deployment, so the same
lockfile always builds the same plugins.
"""

from __future__ import annotations

import posixpath
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from iki_spec import API_VERSION, schemas
from iki_spec.problems import Path as DocPath
from iki_spec.problems import Problem, Severity
from iki_spec.yamlsource import Document, read_documents

__all__ = ["FILENAME", "KIND", "Lock", "LockResult", "LockedPlugin", "load_lock", "lock_to_dict"]

FILENAME = "plugins.lock.yaml"
KIND = "PluginLock"


@dataclass(frozen=True)
class LockedPlugin:
    name: str
    state: str
    git: str | None = None
    sha: str | None = None
    #: The plugin's directory in the repository, if not its root.
    path: str | None = None
    #: Development only: a directory, relative to the lockfile.
    local: str | None = None
    #: The settings file, relative to the lockfile.
    settings: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class Lock:
    #: The lockfile; ``local`` and ``settings`` paths are relative to its directory.
    file: Path
    plugins: tuple[LockedPlugin, ...]

    @property
    def directory(self) -> Path:
        return self.file.parent

    @property
    def has_local(self) -> bool:
        return any(p.local is not None for p in self.plugins)


@dataclass
class LockResult:
    lock: Lock | None
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.lock is not None and not any(p.is_error for p in self.problems)


class _Problems:
    def __init__(self, name: str) -> None:
        self.name = name
        self.problems: list[Problem] = []

    def add(
        self,
        code: str,
        message: str,
        document: Document | None,
        path: DocPath = (),
        *,
        hint: str | None = None,
        key: bool = False,
    ) -> None:
        self.problems.append(
            Problem(
                code=code,
                message=message,
                severity=Severity.ERROR,
                file=self.name,
                location=document.locate(path, key=key) if document else None,
                path=path,
                hint=hint,
            )
        )


def load_lock(file: Path, name: str | None = None) -> LockResult:
    """Read and check a lockfile. ``name`` is how problems refer to it (default: the path)."""
    name = name or str(file)
    problems = _Problems(name)
    if not file.is_file():
        problems.add(
            "missing-lockfile",
            f"There's no lockfile at `{file}`.",
            None,
            hint=f"A deployment's directory holds its `{FILENAME}`.",
        )
        return LockResult(None, problems.problems)
    documents, read_problems = read_documents(file, name)
    if read_problems:
        return LockResult(None, read_problems)
    documents = [d for d in documents if d.data is not None]
    if len(documents) != 1:
        problems.add(
            "lockfile-documents",
            "A lockfile holds one document.",
            documents[1] if documents else None,
        )
        return LockResult(None, problems.problems)
    document = documents[0]
    data = document.data
    if not isinstance(data, dict) or data.get("kind") != KIND:
        kind = data.get("kind") if isinstance(data, dict) else None
        problems.add(
            "not-a-lockfile",
            f"This isn't a plugin lockfile: its kind is `{kind}`."
            if kind
            else "This isn't a plugin lockfile.",
            document,
            ("kind",) if kind else (),
            hint=f"Start it with `apiVersion: {API_VERSION}` and `kind: {KIND}`.",
        )
        return LockResult(None, problems.problems)
    for issue in schemas.check("plugin-lock", data):
        hint = issue.hint
        if issue.path[-1:] == ("sha",):
            value = _at(data, issue.path)
            hint = (
                "YAML read this SHA as a number: put it in quotes."
                if isinstance(value, int | float)
                else "Give the full commit SHA. `git ls-remote <repository> <tag>` prints it."
            )
        problems.add("schema", issue.message, document, issue.path, hint=hint, key=issue.at_key)
    if problems.problems:
        return LockResult(None, problems.problems)
    plugins = [LockedPlugin(**entry) for entry in data["plugins"]]
    _check(plugins, document, file.parent, problems)
    lock = Lock(file=file, plugins=tuple(plugins))
    return LockResult(lock if not problems.problems else None, problems.problems)


def _check(
    plugins: list[LockedPlugin], document: Document, directory: Path, problems: _Problems
) -> None:
    seen: dict[str, int] = {}
    for index, plugin in enumerate(plugins):
        at: DocPath = ("plugins", index)
        if plugin.name in seen:
            problems.add(
                "duplicate-plugin",
                f"`{plugin.name}` is listed twice, here and as plugins[{seen[plugin.name]}].",
                document,
                (*at, "name"),
            )
        seen.setdefault(plugin.name, index)
        if plugin.name == "core":
            problems.add(
                "core-is-builtin",
                "The core plugin comes with Iki, at Iki's version. Don't list it.",
                document,
                (*at, "name"),
            )
        _check_source(plugin, document, at, problems)
        if plugin.settings is not None:
            _check_settings(plugin.settings, document, (*at, "settings"), directory, problems)


def _check_source(
    plugin: LockedPlugin, document: Document, at: DocPath, problems: _Problems
) -> None:
    if plugin.local is not None:
        for key in ("git", "sha", "path"):
            if getattr(plugin, key) is not None:
                problems.add(
                    "mixed-source",
                    f"A plugin comes from git or from a local directory, not both: "
                    f"`{key}` can't go with `local`.",
                    document,
                    (*at, key),
                    key=True,
                )
        return
    if plugin.git is None:
        problems.add(
            "missing-source",
            f"Say where {plugin.name} comes from: `git` and `sha`, or, in development, `local`.",
            document,
            at,
        )
        return
    if plugin.sha is None:
        problems.add(
            "missing-sha",
            f"Pin {plugin.name} to a commit with `sha`.",
            document,
            at,
            hint=f"`git ls-remote {plugin.git} <tag>` prints a tag's commit.",
        )
    if plugin.path is not None and not _inside(plugin.path):
        problems.add(
            "path-outside",
            f"`{plugin.path}` isn't a directory inside the repository.",
            document,
            (*at, "path"),
            hint="Give it relative to the repository's root, without `..`.",
        )


def _check_settings(
    settings: str, document: Document, at: DocPath, directory: Path, problems: _Problems
) -> None:
    if PurePosixPath(settings).is_absolute():
        problems.add(
            "absolute-path",
            "Give the settings file relative to the lockfile, so the config works anywhere.",
            document,
            at,
        )
    elif not (directory / settings).is_file():
        problems.add(
            "missing-settings-file",
            f"There's no settings file at `{settings}`.",
            document,
            at,
            hint="Settings files are relative to the lockfile.",
        )


def _at(data: Any, path: DocPath) -> Any:  # noqa: ANN401 - YAML data
    for key in path:
        data = data[key]
    return data


def _inside(path: str) -> bool:
    normal = posixpath.normpath(path)
    return not (posixpath.isabs(normal) or normal == ".." or normal.startswith("../"))


def lock_to_dict(lock: Lock) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "plugins": [p.to_dict() for p in lock.plugins],
    }

"""Fetching a deployment's plugins at the commits its lockfile pins.

Each repository is fetched once into a bare cache (``~/.cache/iki/git``, or
``$XDG_CACHE_HOME/iki/git``), and only the pinned commit, so a build fetches
nothing it doesn't need and a second build fetches nothing at all. Then the
commit's tree is exported into the build's own directory. A plugin is never
run from a working copy someone could have changed.

Git is run defensively:

- Only https and ssh URLs: git's ``ext::`` and ``file://`` transports could
  run commands or read this machine's files.
- Never prompting: a build in CI fails rather than hanging on a password.
  Credentials come from git's usual helpers, such as the one CI sets up.
  A URL with credentials in it is refused, and none reaches a message:
  every URL and line of git's output is shown without them.
- No hooks, submodules or LFS: the export is the commit's own files.
- Every object fetched is checked (``fetch.fsckObjects``), and the fetched
  commit is checked to be the one asked for.

Iki's own plugins (``iki/...``) come from Iki's repository, or, where a
deployment names them, from its mirrors of it, in order, and never from
Iki's repository then (ADR 0114). A plugin from a mirror is accepted only if
its tree is one Iki's repository has had (:mod:`iki_spec.first_party`), so a
mirror can't serve a commit of its own. Neither can git's own config:
an ``insteadOf`` that sends Iki's repository, or a mirror, somewhere else is
refused, since the build couldn't say where the plugin came from.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from iki_spec import first_party, naming
from iki_spec.lockfile import (
    LockedPlugin,
    redact_credentials,
    repository,
    url_credentials,
    without_credentials,
)

__all__ = ["FetchError", "Fetched", "cache_directory", "check_mirror", "fetch"]

PROTOCOLS = ("https", "ssh")


class FetchError(Exception):
    """A plugin couldn't be fetched; the message says why and what to try."""


@dataclass(frozen=True)
class Fetched:
    """A plugin's directory, and the repository the build asked git for its commit:
    the lockfile's, or a mirror of Iki's. ``None`` for a local directory. It never holds
    credentials."""

    directory: Path
    source: str | None = None
    #: Whether the source is one of the deployment's mirrors of Iki's repository.
    mirrored: bool = False


def cache_directory() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "iki" / "git"


def check_mirror(url: str, protocols: Sequence[str] | None = None) -> str | None:
    """Why ``url`` can't be a mirror of Iki's repository, or None if it can."""
    allowed = protocols or PROTOCOLS
    shown = without_credentials(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"`{shown}` isn't a URL git reads"
    remote = repository(url)
    scp = "://" not in url and remote is not None and remote.scheme == "ssh"
    if ("ssh" if scp else parts.scheme) not in allowed:
        return f"`{shown}` isn't an {' or '.join(allowed)} URL"
    what = url_credentials(url)
    if what == "a query or fragment":
        return f"`{shown}` has a query or fragment, which a git URL doesn't"
    if what is not None:
        return f"`{shown}` has {what} in it: give it to git's credential helper instead"
    if not scp and parts.scheme != "file" and not parts.hostname:
        return f"`{shown}` has no host"
    if remote is None:
        return f"`{shown}` isn't a valid Git repository address"
    return None


def fetch(
    plugin: LockedPlugin,
    lock_directory: Path,
    into: Path,
    *,
    cache: Path | None = None,
    protocols: Sequence[str] | None = None,
    mirrors: Sequence[str] = (),
) -> Fetched:
    """The plugin: ``local`` as is, or its commit exported under ``into``."""
    if plugin.local is not None:
        directory = (lock_directory / plugin.local).resolve()
        if not directory.is_dir():
            raise FetchError(f"{plugin.name}: there's no directory at {plugin.local}.")
        return Fetched(directory)
    assert plugin.git is not None and plugin.sha is not None  # noqa: S101 - the lockfile's rules
    protocols = protocols or PROTOCOLS
    ours = naming.split(plugin.name)[0] == naming.IKI_NAMESPACE
    mirrored = ours and bool(mirrors)
    sources = mirrors if mirrored else [plugin.git]
    failures = []
    for source in sources:
        git = _Git(_cache(cache, source), protocols)
        if ours:
            git.check_not_rewritten(plugin, source)
        try:
            git.ensure(plugin, source)
        except FetchError as error:
            failures.append(str(error))
            continue
        if mirrored:
            _check_iki_made(git, plugin, source)
        return Fetched(_export(git, plugin, into), without_credentials(source), mirrored)
    if len(failures) == 1:
        raise FetchError(failures[0])
    raise FetchError(
        f"{plugin.name}: no mirror of Iki's repository had commit {plugin.sha[:12]}:\n"
        + "".join(f"  - {failure}\n" for failure in failures)
    )


def _cache(cache: Path | None, source: str) -> Path:
    # One bare repository per source, so a commit's cache says where it came from.
    return (cache or cache_directory()) / hashlib.sha256(source.encode()).hexdigest()[:24]


def _check_iki_made(git: _Git, plugin: LockedPlugin, source: str) -> None:
    """Refuses a plugin from a mirror unless its tree is one Iki's repository has had."""
    assert plugin.sha is not None  # noqa: S101 - the lockfile's rules
    tree = git.run("rev-parse", "--verify", "--quiet", f"{plugin.sha}:{plugin.path}", check=False)
    if tree.strip() not in first_party.trees(plugin.name):
        raise FetchError(
            f"{plugin.name}: `{plugin.path}` at commit {plugin.sha[:12]} from the mirror "
            f"{without_credentials(source)} isn't one Iki's repository has had, as far as this "
            "iki-cli knows. A mirror serves Iki's own commits, so pin one of those. If it's a "
            "newer commit of Iki's, use the iki-cli of a release that has it. If you changed the "
            "plugin, it's "
            f"yours: give it your namespace, such as `acme/{naming.short_name(plugin.name)}`."
        )


def _export(git: _Git, plugin: LockedPlugin, into: Path) -> Path:
    assert plugin.sha is not None  # noqa: S101 - the lockfile's rules
    target = into / naming.app_label(plugin.name)
    target.mkdir(parents=True)
    tree = f"{plugin.sha}:{plugin.path}" if plugin.path else plugin.sha
    if git.run("cat-file", "-t", tree, check=False).strip() != "tree":
        raise FetchError(
            f"{plugin.name}: commit {plugin.sha[:12]} has no directory `{plugin.path}`."
        )
    archive = git.run_bytes("archive", "--format=tar", tree)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        # "data" refuses absolute paths, `..`, links out of the directory and devices.
        tar.extractall(target, filter="data")
    return target


def _format(sha: str) -> str:
    return "sha256" if len(sha) == 64 else "sha1"


class _Git:
    def __init__(self, repository: Path, protocols: Sequence[str]) -> None:
        self.repository = repository
        self.env = {
            **os.environ,
            "GIT_ALLOW_PROTOCOL": ":".join(protocols),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            # ssh: never ask whether to trust a new host key.
            "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"),
        }

    def _command(self, args: Sequence[str]) -> list[str]:
        return [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.fsckObjects=true",
            f"--git-dir={self.repository}",
            *args,
        ]

    def run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(  # noqa: S603 - git, with arguments we build
            self._command(args), env=self.env, capture_output=True, text=True, check=False
        )
        if check and result.returncode != 0:
            raise FetchError(f"git {args[0]} failed: {_last_line(result.stderr)}")
        return result.stdout

    def run_bytes(self, *args: str) -> bytes:
        result = subprocess.run(  # noqa: S603 - git, with arguments we build
            self._command(args), env=self.env, capture_output=True, check=False
        )
        if result.returncode != 0:
            raise FetchError(f"git {args[0]} failed: {_last_line(result.stderr.decode())}")
        return result.stdout

    def has_commit(self, sha: str) -> bool:
        found = self.run("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
        return found.strip() == sha

    def effective(self, url: str) -> str:
        """Where git's config (``url.<base>.insteadOf``) sends ``url``."""
        return self.run("ls-remote", "--get-url", "--", url).strip() or url

    def check_not_rewritten(self, plugin: LockedPlugin, url: str) -> None:
        self._init(plugin)
        effective = self.effective(url)
        expected = repository(url)
        if expected is None or repository(effective) != expected:
            raise FetchError(
                f"{plugin.name}: git's config sends {without_credentials(url)} to "
                f"{without_credentials(effective)} "
                "(`url.<base>.insteadOf`). Name a mirror of Iki's repository for the "
                "deployment instead, so the build checks what it fetches and records where it "
                "came from."
            )

    def _init(self, plugin: LockedPlugin) -> None:
        assert plugin.sha is not None  # noqa: S101 - the lockfile's rules
        if not (self.repository / "HEAD").is_file():
            self.repository.mkdir(parents=True, exist_ok=True)
            self.run("init", "--quiet", "--bare", f"--object-format={_format(plugin.sha)}")

    def ensure(self, plugin: LockedPlugin, url: str) -> None:
        """Has ``plugin``'s commit, from the cache or fetched from ``url``."""
        assert plugin.sha is not None  # noqa: S101 - the lockfile's rules
        self._init(plugin)
        if not self.has_commit(plugin.sha):
            self.fetch(plugin, url)
        if not self.has_commit(plugin.sha):
            raise FetchError(
                f"{plugin.name}: {without_credentials(url)} sent something other than {plugin.sha}."
            )

    def fetch(self, plugin: LockedPlugin, url: str) -> None:
        assert plugin.sha is not None  # noqa: S101 - the lockfile's rules
        result = subprocess.run(  # noqa: S603 - git, with arguments we build
            self._command(
                [
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--",
                    url,
                    plugin.sha,
                ]
            ),
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise FetchError(
                f"{plugin.name}: couldn't fetch commit {plugin.sha[:12]} from "
                f"{without_credentials(url)}: "
                f"{_last_line(result.stderr)}. Check that the commit is pushed and that this "
                "machine can read the repository."
            )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    last = (lines[-1] if lines else "no message").removeprefix("fatal: ").rstrip(".")
    return redact_credentials(last)

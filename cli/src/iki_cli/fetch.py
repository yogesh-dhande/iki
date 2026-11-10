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
- No hooks, submodules or LFS: the export is the commit's own files.
- The fetched commit is checked to be the one asked for.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path

from iki_spec.lockfile import LockedPlugin

__all__ = ["FetchError", "cache_directory", "fetch"]

PROTOCOLS = ("https", "ssh")


class FetchError(Exception):
    """A plugin couldn't be fetched; the message says why and what to try."""


def cache_directory() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "iki" / "git"


def fetch(
    plugin: LockedPlugin,
    lock_directory: Path,
    into: Path,
    *,
    cache: Path | None = None,
    protocols: Sequence[str] | None = None,
) -> Path:
    """The plugin's directory: ``local`` as is, or its commit exported under ``into``."""
    if plugin.local is not None:
        directory = (lock_directory / plugin.local).resolve()
        if not directory.is_dir():
            raise FetchError(f"{plugin.name}: there's no directory at {plugin.local}.")
        return directory
    assert plugin.git is not None and plugin.sha is not None  # noqa: S101 - the lockfile's rules
    repository = (cache or cache_directory()) / hashlib.sha256(plugin.git.encode()).hexdigest()[:24]
    git = _Git(repository, protocols or PROTOCOLS)
    if not (repository / "HEAD").is_file():
        repository.mkdir(parents=True, exist_ok=True)
        git.run("init", "--quiet", "--bare", f"--object-format={_format(plugin.sha)}")
    if not git.has_commit(plugin.sha):
        git.fetch(plugin)
    if not git.has_commit(plugin.sha):
        raise FetchError(f"{plugin.name}: {plugin.git} sent something other than {plugin.sha}.")
    target = into / plugin.name
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

    def fetch(self, plugin: LockedPlugin) -> None:
        assert plugin.git is not None and plugin.sha is not None  # noqa: S101 - the lockfile's rules
        result = subprocess.run(  # noqa: S603 - git, with arguments we build
            self._command(
                [
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--",
                    plugin.git,
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
                f"{plugin.name}: couldn't fetch commit {plugin.sha[:12]} from {plugin.git}: "
                f"{_last_line(result.stderr)}. Check that the commit is pushed and that this "
                "machine can read the repository."
            )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return (lines[-1] if lines else "no message").removeprefix("fatal: ").rstrip(".")

"""What a deployment's workers need, from the same lockfile as its server (ADR 0047).

``iki deploy build --workers DIRECTORY`` writes:

- ``plugins/<name>/``: each plugin's code, exported at the commit the lockfile
  pins (the server's image gets only its manifests);
- ``plugins/plugins.json``: what the worker tells the server it has. Each
  plugin's name, version and commit, and each environment it builds, with
  the build's digest and where it's built;
- ``build-environments.sh``: builds those environments, for the worker
  image's Dockerfile to run.

An environment's digest covers only what decides its contents: its Python
version and requirements file, or its conda lockfile. So an environment
that didn't change keeps its digest, and its layer stays cached, across
plugin commits.

Every ``iki deploy build`` also bundles each pinned plugin's code for the server
to give workers on people's computers (ADR 0059): a gzipped tar, the same bytes
for the same files, with what's needed to build each environment where it lands.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shlex
import shutil
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from iki_spec import API_VERSION
from iki_spec.model import Environment, Plugin

__all__ = [
    "ENVIRONMENTS",
    "SCRIPT",
    "WORKER_PLUGINS",
    "WorkerPlugin",
    "bundle",
    "environments",
    "write",
]

#: Where the worker image builds environments, unless told otherwise.
ENVIRONMENTS = "/opt/iki/envs"
#: What the worker reads, in ``IKI_PLUGINS_DIR``.
WORKER_PLUGINS = "plugins.json"
SCRIPT = "build-environments.sh"

#: Copied from a local plugin directory, for development, without these.
_IGNORED = shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules", ".DS_Store")


@dataclass(frozen=True)
class WorkerPlugin:
    plugin: Plugin
    directory: Path
    #: None for a local directory.
    sha: str | None


def _sha256(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def _built(env: Environment) -> bool:
    """Whether workers build it: a Python or conda environment for the backend or client
    tier. The container and vm tiers run images instead."""
    native = env.requirements is not None or env.conda_lockfile is not None
    return native and bool(set(env.tiers) & {"backend", "client"})


def _definition(env: Environment, directory: Path) -> dict[str, str]:
    if env.conda_lockfile is not None:
        return {"kind": "conda", "lockfile_sha256": _sha256(directory / env.conda_lockfile)}
    assert env.python_version is not None and env.requirements is not None  # noqa: S101 - the schema
    return {
        "kind": "python",
        "python": env.python_version,
        "requirements_sha256": _sha256(directory / env.requirements),
    }


def _digest(definition: dict[str, str]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def environments(plugin: Plugin, directory: Path) -> dict[str, dict[str, Any]]:
    """How a worker builds each of `plugin`'s environments from its bundle: the digest it
    reports, and the files (by path in the bundle) and Python version the build takes."""
    found: dict[str, dict[str, Any]] = {}
    for name, env in sorted(plugin.environments.items()):
        if not _built(env):
            continue
        definition = _definition(env, directory)
        entry: dict[str, Any] = {"digest": _digest(definition), "kind": definition["kind"]}
        if env.conda_lockfile is not None:
            entry["lockfile"] = str(PurePosixPath(env.conda_lockfile))
        else:
            assert env.python_version is not None and env.requirements is not None  # noqa: S101 - the schema
            entry["python"] = env.python_version
            entry["requirements"] = str(PurePosixPath(env.requirements))
            entry["packages"] = _has_requirements(directory / env.requirements)
        found[name] = entry
    return found


def _files(directory: Path) -> list[Path]:
    ignored = {".git", "__pycache__", ".venv", "node_modules", ".DS_Store"}
    found: list[Path] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in ignored)
        here = Path(root)
        found.extend(here / name for name in names if name not in ignored)
        found.extend(here / d for d in dirs if (here / d).is_symlink())
    return sorted(found)


def bundle(directory: Path, output: Path) -> dict[str, Any]:
    """Writes `directory`'s code to `output`, a gzipped tar workers fetch, and returns its
    ``sha256`` and ``size``. Only regular files: a symbolic link could point anywhere on
    the computer that unpacks it, so a plugin with one is refused. Times, owners and
    modes other than executable are left out, so the same files give the same bytes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for file in _files(directory):
            relative = file.relative_to(directory).as_posix()
            if file.is_symlink() or not file.is_file():
                raise ValueError(
                    f"{relative} is a symbolic link or special file; plugins hold only "
                    "regular files"
                )
            info = tarfile.TarInfo(relative)
            info.size = file.stat().st_size
            info.mode = 0o755 if os.access(file, os.X_OK) else 0o644
            info.mtime = 0
            with file.open("rb") as data:
                tar.addfile(info, data)
    packed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=packed, mtime=0) as zipped:
        zipped.write(raw.getvalue())
    output.write_bytes(packed.getvalue())
    return {"sha256": hashlib.sha256(packed.getvalue()).hexdigest(), "size": output.stat().st_size}


def _has_requirements(file: Path) -> bool:
    """Whether a requirements file names any package: an environment may need none."""
    for line in file.read_text("utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text and not text.startswith("--hash"):
            return True
    return False


def _build_lines(item: WorkerPlugin, env: Environment, prefix: str) -> list[str]:
    q = shlex.quote
    # Files in the bundle, beside this script, wherever it's copied.
    code = f"plugins/{item.plugin.name}"
    if env.conda_lockfile is not None:
        lockfile = f"{code}/{PurePosixPath(env.conda_lockfile)}"
        return [
            f'micromamba create --yes --quiet --prefix {q(prefix)} --file "$here"/{q(lockfile)}'
        ]
    assert env.python_version is not None and env.requirements is not None  # noqa: S101 - the schema
    # uv downloads the Python version when the image has none.
    lines = [f"uv venv --quiet --python {q(env.python_version)} {q(prefix)}"]
    if _has_requirements(item.directory / env.requirements):
        requirements = f"{code}/{PurePosixPath(env.requirements)}"
        # Every package pinned and hashed, so nothing else is installed.
        lines.append(
            f"uv pip install --quiet --python {q(prefix)} --require-hashes --no-deps "
            f'--requirement "$here"/{q(requirements)}'
        )
    return lines


def write(
    plugins: Sequence[WorkerPlugin], output: Path, env_root: str = ENVIRONMENTS
) -> dict[str, Any]:
    """Writes the workers' bundle to ``output`` (which mustn't exist), for environments
    built in ``env_root``, and returns what ``plugins.json`` holds."""
    code = output / "plugins"
    code.mkdir(parents=True)
    listed = []
    script = [
        "#!/bin/sh",
        "# Generated by `iki deploy build`: builds the environments workers run actions in.",
        "# The worker image's Dockerfile runs it, with uv and micromamba on PATH.",
        "set -eu",
        'here=$(cd "$(dirname "$0")" && pwd)',
    ]
    for item in sorted(plugins, key=lambda p: p.plugin.name):
        name = item.plugin.name
        shutil.copytree(item.directory, code / name, ignore=_IGNORED, symlinks=True)
        environments = {}
        for env_name, env in sorted(item.plugin.environments.items()):
            if not _built(env):
                continue
            prefix = f"{env_root}/{name}/{env_name}"
            definition = _definition(env, item.directory)
            environments[env_name] = {
                "digest": _digest(definition),
                "kind": definition["kind"],
                "prefix": prefix,
            }
            script.append(f"echo {shlex.quote(f'Building {name}.{env_name}')}")
            script.append(f"mkdir -p {shlex.quote(f'{env_root}/{name}')}")
            script.extend(_build_lines(item, env, prefix))
        listed.append(
            {
                "name": name,
                "version": item.plugin.version,
                "sha": item.sha,
                "environments": environments,
            }
        )
    manifest = {"apiVersion": API_VERSION, "kind": "WorkerPlugins", "plugins": listed}
    (code / WORKER_PLUGINS).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (output / SCRIPT).write_text("\n".join(script) + "\n")
    (output / SCRIPT).chmod(0o755)
    return manifest

"""`iki deploy …`: commands for a deployment's config repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from iki_cli import workers
from iki_cli.fetch import FetchError, fetch
from iki_cli.plugin import _missing_dependencies, write_problems
from iki_codegen import REGISTRY
from iki_codegen import build as build_apps
from iki_spec import compute as compute_spec
from iki_spec.lockfile import FILENAME, Lock, LockedPlugin, load_lock, lock_to_dict
from iki_spec.model import Plugin
from iki_spec.validate import load_plugin, read_settings
from iki_spec.yamlsource import read_documents

#: The deployment's compute, as the server reads it.
COMPUTE_JSON = "compute.json"


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    deploy = subparsers.add_parser(
        "deploy",
        help="Build a deployment's plugins.",
        description="Commands for a deployment's config repository.",
    )
    commands = deploy.add_subparsers(dest="deploy_command", metavar="COMMAND")
    build = commands.add_parser(
        "build",
        help="Fetch, check and compile the plugins a lockfile pins.",
        description=(
            f"Read a deployment's {FILENAME}, fetch each plugin at its commit, and check them "
            "together: their manifests, the deployment's settings for them, and that their "
            f"migrations are complete. Check its {compute_spec.FILENAME} against them, if it has "
            "one. Then compile them for the server image. The same lockfile always gives the "
            "same output."
        ),
    )
    build.add_argument(
        "deployment",
        type=Path,
        help=f"The deployment's directory, holding {FILENAME} (or the lockfile itself).",
    )
    build.add_argument(
        "--output",
        "-o",
        required=True,
        type=Path,
        metavar="DIRECTORY",
        help="Where to write the plugins, for the image's /opt/iki/plugins.",
    )
    build.add_argument(
        "--workers",
        type=Path,
        metavar="DIRECTORY",
        help=(
            "Also write what the deployment's workers need, for the worker image: each "
            "plugin's code at its commit, and a script that builds its environments."
        ),
    )
    build.add_argument(
        "--environments-dir",
        default=workers.ENVIRONMENTS,
        metavar="DIRECTORY",
        help=f"Where the worker image builds environments (default: {workers.ENVIRONMENTS}).",
    )
    build.add_argument(
        "--allow-local",
        action="store_true",
        help="Accept plugins from local directories (development only).",
    )
    build.add_argument(
        "--cache",
        type=Path,
        metavar="DIRECTORY",
        help="Where to keep fetched repositories (default: ~/.cache/iki/git).",
    )
    build.set_defaults(handler=run_build)
    deploy.set_defaults(handler=lambda _args: _help(deploy))


def _help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


@dataclass
class _Fetched:
    entry: LockedPlugin
    directory: Path
    plugin: Plugin | None = None

    @property
    def label(self) -> str:
        if self.entry.sha is not None:
            return f"{self.entry.name}@{self.entry.sha[:12]}"
        return str(self.entry.local)


def run_build(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    err = sys.stderr
    file: Path = args.deployment
    if file.is_dir():
        file = file / FILENAME
    result = load_lock(file)
    if result.lock is None or not result.ok:
        write_problems(err, result.problems, file.parent, {str(file): file})
        return 1
    lock = result.lock
    if lock.has_local and not args.allow_local:
        names = ", ".join(p.name for p in lock.plugins if p.local is not None)
        err.write(
            f"error: {names} come(s) from a local directory, which is only for development. "
            "Pin them with `git` and `sha`, or pass --allow-local.\n"
        )
        return 1
    with tempfile.TemporaryDirectory(prefix="iki-deploy-") as tmp:
        workspace = Path(tmp)
        fetched = _fetch_all(lock, workspace / "sources", args.cache, out, err)
        if fetched is None:
            return 1
        if not _load_all(fetched, err):
            return 1
        settings = _settings(lock, fetched, err)
        if settings is None:
            return 1
        compute = _compute(lock, fetched, err)
        if compute is None:
            return 1
        bundles = workspace / "bundles"
        try:
            made = _bundles(fetched, bundles)
        except ValueError as error:
            err.write(f"error: {error}.\n")
            return 1
        staging = workspace / "build"
        try:
            build_apps(
                [f.plugin for f in fetched if f.plugin is not None],
                staging,
                migrations={f.entry.name: f.directory / "migrations" for f in fetched},
                settings=settings,
                lock=_record(lock, fetched, made),
                local=lock.has_local,
            )
        except ValueError as error:
            err.write(f"error: {error}.\n")
            return 1
        if made:
            shutil.copytree(bundles, staging / "iki_installed_plugins" / BUNDLES)
        if fetched and not _migrations_complete(fetched, staging, err):
            return 1
        (staging / COMPUTE_JSON).write_text(
            json.dumps(compute_spec.compute_to_dict(compute), indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        if not _replace(staging, args.output, err):
            return 1
        if args.workers is not None:
            bundle = workspace / "workers"
            workers.write(
                [
                    workers.WorkerPlugin(f.plugin, f.directory, f.entry.sha)
                    for f in fetched
                    if f.plugin is not None
                ],
                bundle,
                args.environments_dir,
            )
            if not _replace_workers(bundle, args.workers, err):
                return 1
            out.write(f"Wrote what workers need to {args.workers}.\n")
    summary = ", ".join(f"{f.entry.name} {f.plugin.version}" for f in fetched if f.plugin)
    out.write(f"Built {args.output}: {summary or 'no plugins besides core'}.\n")
    return 0


def _fetch_all(
    lock: Lock, into: Path, cache: Path | None, out: TextIO, err: TextIO
) -> list[_Fetched] | None:
    fetched = []
    for entry in lock.plugins:
        try:
            directory = fetch(entry, lock.directory, into, cache=cache)
        except FetchError as error:
            err.write(f"error: {error}\n")
            return None
        if entry.sha is not None:
            out.write(f"Fetched {entry.name} at {entry.sha[:12]} from {entry.git}\n")
        fetched.append(_Fetched(entry, directory))
    return fetched


def _declared_name(directory: Path) -> str | None:
    documents, _ = read_documents(directory / "plugin.yaml", "plugin.yaml")
    data = documents[0].data if documents else None
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def _load_all(fetched: list[_Fetched], err: TextIO) -> bool:
    for item in fetched:
        declared = _declared_name(item.directory)
        if declared is not None and declared != item.entry.name:
            err.write(
                f"error: the lockfile's {item.entry.name} is the plugin `{declared}` "
                f"({item.label}). Check its `name` or `path`.\n"
            )
            return False
    directories = [f.directory for f in fetched]
    missing = _missing_dependencies(directories)
    if missing:
        for message in missing:
            err.write(
                f"error: {message.replace('give its directory too', 'add it to the lockfile')}\n"
            )
        return False
    ok = True
    for item in fetched:
        others = [d for d in directories if d != item.directory]
        result = load_plugin(item.directory, dependencies=others)
        if result.problems:
            sources = {p.file: item.directory / p.file for p in result.problems if p.file}
            write_problems(err, result.problems, item.directory, sources, label=item.label)
        if result.plugin is None or result.errors:
            ok = False
            continue
        item.plugin = result.plugin
    return ok


def _settings(lock: Lock, fetched: list[_Fetched], err: TextIO) -> dict[str, dict[str, Any]] | None:
    out: dict[str, dict[str, Any]] = {}
    ok = True
    for item in fetched:
        assert item.plugin is not None  # noqa: S101 - _load_all loaded every plugin
        relative = item.entry.settings
        file = lock.directory / relative if relative else None
        name = str(file) if file else str(lock.file)
        values, problems = read_settings(item.plugin, file, name)
        if problems:
            write_problems(err, problems, lock.directory, {name: file or lock.file})
        if values is None:
            ok = False
            if file is None:
                err.write(
                    f"  hint: give {item.entry.name}'s settings in a file, with `settings:` in "
                    "the lockfile.\n"
                )
            continue
        out[item.entry.name] = values
    return out if ok else None


def _compute(lock: Lock, fetched: list[_Fetched], err: TextIO) -> compute_spec.Compute | None:
    """The deployment's compute templates and bindings, checked against its plugins."""
    file = lock.directory / compute_spec.FILENAME
    plugins = [f.plugin for f in fetched if f.plugin is not None]
    result = compute_spec.load_compute(file, plugins, str(file))
    if result.problems:
        write_problems(err, result.problems, lock.directory, {str(file): file})
    return result.compute if result.ok else None


#: In ``iki_installed_plugins``: each pinned plugin's code, for workers to fetch (ADR 0059).
BUNDLES = "bundles"


def _bundles(fetched: list[_Fetched], output: Path) -> dict[str, dict[str, Any]]:
    """Each pinned plugin's bundle in `output`, as ``<name>-<sha>.tar.gz``, and what the
    server tells workers of it, by plugin."""
    made: dict[str, dict[str, Any]] = {}
    for item in fetched:
        if item.entry.sha is None or item.plugin is None:
            continue  # a local directory, for development: workers there have it
        output.mkdir(exist_ok=True)
        try:
            packed = workers.bundle(
                item.directory, output / f"{item.entry.name}-{item.entry.sha}.tar.gz"
            )
        except ValueError as error:
            raise ValueError(f"{item.label}: {error}") from None
        made[item.entry.name] = {
            "bundle": packed,
            "environments": workers.environments(item.plugin, item.directory),
        }
    return made


def _record(
    lock: Lock, fetched: list[_Fetched], made: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Where each plugin came from, for the server's plugins page and for audits, and the
    bundle workers fetch of each pinned one."""
    plugins = []
    for item in fetched:
        assert item.plugin is not None  # noqa: S101 - _load_all loaded every plugin
        entry = item.entry.to_dict()
        entry.pop("settings", None)
        entry["version"] = item.plugin.version
        entry.update((made or {}).get(item.entry.name, {}))
        if item.entry.settings:
            entry["settings_sha256"] = _sha256(lock.directory / item.entry.settings)
        plugins.append(entry)
    return {"lockfile_sha256": _sha256(lock.file), "lock": lock_to_dict(lock), "plugins": plugins}


def _sha256(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def _migrations_complete(fetched: Sequence[_Fetched], build: Path, err: TextIO) -> bool:
    """Whether every plugin's migrations make the tables its manifests describe."""
    names = [f.entry.name for f in fetched]
    command = [sys.executable, "-m", "iki_server.plugins.makemigrations", *names]
    command += ["--check", "--dry-run", "--no-input"]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(build), os.environ.get("PYTHONPATH")])),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(  # noqa: S603 - our own module, with this interpreter
        command, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return True
    err.write(result.stderr)
    err.write(result.stdout)
    err.write(
        "error: a plugin's manifests have changes its migrations don't. In the plugin's "
        "repository, run `iki plugin makemigrations`, commit the migration, and pin that commit.\n"
    )
    return False


def _replace_workers(bundle: Path, output: Path, err: TextIO) -> bool:
    """Puts the workers' bundle at ``output``, replacing an earlier one but nothing else."""
    if output.exists():
        unexpected = sorted(
            p.name for p in output.iterdir() if p.name not in ("plugins", workers.SCRIPT)
        )
        if unexpected:
            err.write(
                f"error: {output} holds files a build didn't write ({', '.join(unexpected)}). "
                "Give an empty directory, or a previous build's.\n"
            )
            return False
        shutil.rmtree(output)
    shutil.copytree(bundle, output, symlinks=True)
    return True


def _replace(staging: Path, output: Path, err: TextIO) -> bool:
    """Puts the build at ``output``, replacing an earlier build but nothing else."""
    if output.exists():
        unexpected = [
            p.name
            for p in output.iterdir()
            if p.name not in (REGISTRY, COMPUTE_JSON) and not p.name.startswith("iki_plugin_")
        ]
        if unexpected:
            err.write(
                f"error: {output} holds files a build didn't write "
                f"({', '.join(sorted(unexpected))}). Give an empty directory, or a previous "
                "build's.\n"
            )
            return False
        shutil.rmtree(output)
    shutil.copytree(staging, output)
    return True

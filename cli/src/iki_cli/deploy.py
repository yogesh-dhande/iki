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

from iki_cli import fetch as fetching
from iki_cli import workers
from iki_cli.fetch import FetchError, fetch
from iki_cli.plugin import _missing_dependencies, write_problems
from iki_codegen import REGISTRY
from iki_codegen import build as build_apps
from iki_codegen.build import with_hosts
from iki_spec import compute as compute_spec
from iki_spec import naming
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
        "--images",
        type=Path,
        metavar="FILE",
        help=(
            "Also write the images the deployment's runs use, one per line, pinned by digest: "
            "those of environments it binds to container-tier templates. Its build signs "
            "each, so the cluster admits them (ADR 0063)."
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
        "--mirror",
        action="append",
        default=[],
        metavar="URL",
        help=(
            "A mirror of Iki's repository to fetch Iki's own plugins (iki/...) from, rather "
            "than Iki's repository: the deployment's plugins.mirrors. Repeat for several, "
            "tried in order. Each plugin must be at the commit its lockfile pins, with a tree "
            "Iki's repository has had (ADR 0114)."
        ),
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
    #: The repository its commit came from, if not a local directory.
    source: str | None = None
    #: Whether that was a mirror of Iki's repository, rather than the lockfile's.
    mirrored: bool = False

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
    mirrors: list[str] = list(dict.fromkeys(args.mirror))
    for url in mirrors:
        why = fetching.check_mirror(url, fetching.PROTOCOLS)
        if why is not None:
            err.write(f"error: --mirror {why}.\n")
            return 1
    if lock.has_local and not args.allow_local:
        names = ", ".join(p.name for p in lock.plugins if p.local is not None)
        err.write(
            f"error: {names} come(s) from a local directory, which is only for development. "
            "Pin them with `git` and `sha`, or pass --allow-local.\n"
        )
        return 1
    with tempfile.TemporaryDirectory(prefix="iki-deploy-") as tmp:
        workspace = Path(tmp)
        fetched = _fetch_all(lock, workspace / "sources", args.cache, mirrors, out, err)
        if fetched is None:
            return 1
        purged = {p.name for p in lock.plugins if p.state == "purged"}
        if (
            not _nothing_needs(purged, fetched, err)
            or not _load_all(fetched, err)
            or not _detached_alone(fetched, err)
        ):
            return 1
        settings = _settings(lock, fetched, err)
        if settings is None or not _setups_call_no_apis(fetched, settings, err):
            return 1
        compute = _compute(lock, fetched, err)
        if compute is None:
            return 1
        bundles = workspace / "bundles"
        try:
            for item in fetched:
                item.directory = workers.prepared_source(
                    item.entry.name,
                    item.directory,
                    workspace / "worker-code" / naming.app_label(item.entry.name),
                    pinned=item.entry.sha is not None,
                )
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
                sources={f.entry.name: f.directory for f in fetched},
                settings=settings,
                lock=_record(lock, fetched, made),
                local=lock.has_local,
                mirrors=[f.source for f in fetched if f.source is not None and f.mirrored],
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
        if args.images is not None:
            listed = run_images([f.plugin for f in fetched if f.plugin is not None], compute)
            args.images.parent.mkdir(parents=True, exist_ok=True)
            args.images.write_text("".join(f"{image}\n" for image in listed), "utf-8")
            out.write(f"Wrote the {len(listed)} image(s) runs use to {args.images}.\n")
    summary = ", ".join(f"{f.entry.name} {f.plugin.version}" for f in fetched if f.plugin)
    out.write(f"Built {args.output}: {summary or 'no plugins besides core'}.\n")
    return 0


def _fetch_all(
    lock: Lock,
    into: Path,
    cache: Path | None,
    mirrors: Sequence[str],
    out: TextIO,
    err: TextIO,
) -> list[_Fetched] | None:
    fetched = []
    for entry in lock.plugins:
        if entry.state == "purged":
            continue  # not built: the deploy drops its tables (ADR 0092)
        try:
            got = fetch(entry, lock.directory, into, cache=cache, mirrors=mirrors)
        except FetchError as error:
            err.write(f"error: {error}\n")
            return None
        item = _Fetched(entry, got.directory, source=got.source, mirrored=got.mirrored)
        if entry.sha is not None:
            mirror = f" (a mirror of {entry.git})" if item.mirrored else ""
            out.write(f"Fetched {entry.name} at {entry.sha[:12]} from {got.source}{mirror}\n")
        fetched.append(item)
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


def _nothing_needs(purged: set[str], fetched: list[_Fetched], err: TextIO) -> bool:
    """A purged plugin's tables are dropped (ADR 0092), so nothing built may depend on it."""
    ok = True
    for item in fetched:
        documents, _ = read_documents(item.directory / "plugin.yaml", "plugin.yaml")
        data = documents[0].data if documents else None
        declared = data.get("dependencies") if isinstance(data, dict) else None
        needs = sorted(purged & set(declared if isinstance(declared, dict) else ()))
        if needs:
            names = " and ".join(needs)
            err.write(
                f"error: {item.entry.name} depends on {names}, which the lockfile purges. "
                f"Purge {item.entry.name} too, or keep {names} detached.\n"
            )
            ok = False
    return ok


def _detached_alone(fetched: list[_Fetched], err: TextIO) -> bool:
    """A detached plugin's tables are read-only (ADR 0091), so a plugin still active can't
    build on its types: what depends on it must be detached first, or with it."""
    detached = {f.entry.name for f in fetched if f.entry.state == "detached"}
    ok = True
    for item in fetched:
        assert item.plugin is not None  # noqa: S101 - _load_all loaded every plugin
        needs = sorted(detached & set(item.plugin.dependencies))
        if item.entry.state != "detached" and needs:
            names = " and ".join(needs)
            err.write(
                f"error: {item.entry.name} depends on {names}, which the lockfile detaches. "
                f"Detach {item.entry.name} too, or keep {names} active.\n"
            )
            ok = False
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


def _setups_call_no_apis(
    fetched: list[_Fetched], settings: dict[str, dict[str, Any]], err: TextIO
) -> bool:
    """That no setup build downloads from an outside API's host, as the deployment's settings
    give both (ADR 0141). A build belongs to the deployment, so there's no project to approve
    the API for, nobody to consent to what's sent and no project to bill; and through the
    egress proxy it would reach the API unrecorded, without its key. `iki plugin validate`
    catches a plugin's own hosts; only here are hosts a setting gives, and other plugins'
    APIs, known."""
    plugins = [with_hosts(f.plugin, settings.get(f.entry.name, {})) for f in fetched if f.plugin]
    apis = {api.host: api for p in plugins for api in p.apis.values() if api.host}
    ok = True
    for plugin in plugins:
        for environment in plugin.environments.values():
            for host in environment.setup_network:
                api = apis.get(host)
                if api is None:
                    continue
                ok = False
                err.write(
                    f"error: {plugin.name}'s {environment.name} setup downloads from {host}, "
                    f"which is {api.qualified_name}'s host ({api.label}, an outside API). "
                    "Setup builds can't call outside APIs: a build belongs to the deployment, "
                    "so there's no project to approve it for and none to bill (ADR 0141).\n"
                    "  hint: give the setting another host, or drop it from the setup's "
                    "`network`: a setup downloads its data, such as a model's weights, from "
                    "the host that serves the files.\n"
                )
    return ok


def _compute(lock: Lock, fetched: list[_Fetched], err: TextIO) -> compute_spec.Compute | None:
    """The deployment's compute templates and bindings, checked against its plugins."""
    file = lock.directory / compute_spec.FILENAME
    plugins = [f.plugin for f in fetched if f.plugin is not None]
    result = compute_spec.load_compute(file, plugins, str(file))
    if result.problems:
        write_problems(err, result.problems, lock.directory, {str(file): file})
    return result.compute if result.ok else None


def run_images(plugins: Sequence[Plugin], compute: compute_spec.Compute) -> list[str]:
    """The manifest and deployment images used by container templates, sorted and unique."""
    found = set()
    for plugin in plugins:
        for environment in plugin.environments.values():
            qualified = f"{plugin.name}.{environment.name}"
            binding = compute.bindings.get(qualified)
            if binding is None:
                continue
            if any(
                name in compute.templates and compute.templates[name].tier == "container"
                for name, _ in binding.templates
            ):
                image = environment.image
                if environment.customer_image_required:
                    supplied = compute.image_binding(qualified, plugin.version)
                    image = supplied.image if supplied else None
                if image is not None:
                    found.add(image)
    return sorted(found)


#: In ``iki_installed_plugins``: each pinned plugin's code, for workers to fetch (ADR 0059).
BUNDLES = "bundles"
#: What a plugin from a local directory's bundle is named for, in place of its commit.
LOCAL = "local"


def _bundles(fetched: list[_Fetched], output: Path) -> dict[str, dict[str, Any]]:
    """Each pinned plugin's bundle in `output`, as ``<label>-<sha>.tar.gz``, and what the
    server tells workers of it, by plugin."""
    made: dict[str, dict[str, Any]] = {}
    for item in fetched:
        if item.plugin is None:
            continue
        # A local directory (development only) has no commit: its bundle is ``local``, for
        # the pods a development cluster starts for runs (ADR 0061).
        output.mkdir(exist_ok=True)
        try:
            packed = workers.bundle(
                item.directory,
                output / f"{naming.app_label(item.entry.name)}-{item.entry.sha or LOCAL}.tar.gz",
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
        if item.source is not None:
            # Where its commit came from: the lockfile's repository, or a mirror of Iki's.
            entry["fetched_from"] = item.source
        entry.update((made or {}).get(item.entry.name, {}))
        if item.entry.settings:
            entry["settings_sha256"] = _sha256(lock.directory / item.entry.settings)
        plugins.append(entry)
    # A purged plugin isn't built, but the server reads it here: its migrate Job drops
    # the plugin's tables, and its Plugins page keeps the record.
    plugins += [p.to_dict() for p in lock.plugins if p.state == "purged"]
    return {"lockfile_sha256": _sha256(lock.file), "lock": lock_to_dict(lock), "plugins": plugins}


def _sha256(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def _migrations_complete(fetched: Sequence[_Fetched], build: Path, err: TextIO) -> bool:
    """Whether every plugin's migrations make the tables its manifests describe."""
    labels = [naming.app_label(f.entry.name) for f in fetched]
    command = [sys.executable, "-m", "iki_server.plugins.makemigrations", *labels]
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

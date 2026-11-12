"""`iki plugin …`: commands for plugin authors."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from iki_codegen import build as build_apps
from iki_spec import API_VERSION, naming
from iki_spec.model import Plugin
from iki_spec.problems import Problem
from iki_spec.validate import Result, check_settings_values, load_plugin
from iki_spec.yamlsource import read_documents


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    plugin = subparsers.add_parser(
        "plugin",
        help="Check and build plugins.",
        description="Commands for plugin authors.",
    )
    commands = plugin.add_subparsers(dest="plugin_command", metavar="COMMAND")
    validate = commands.add_parser(
        "validate",
        help="Check a plugin's manifests.",
        description=(
            f"Check every manifest in a plugin against the {API_VERSION} spec: the JSON Schemas, "
            "units, references between types and plugins, and the rules for tables, indexes, "
            "identities and settings."
        ),
    )
    validate.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="The plugin's directory, holding plugin.yaml (default: the current directory).",
    )
    validate.add_argument(
        "--with",
        dest="dependencies",
        action="append",
        default=[],
        type=Path,
        metavar="DIRECTORY",
        help="A plugin this one depends on, so references to it can be checked. Repeat for each.",
    )
    validate.add_argument(
        "--settings",
        type=Path,
        metavar="FILE",
        help="Also check a deployment's values for this plugin's settings (a YAML mapping).",
    )
    validate.add_argument(
        "--format",
        choices=("text", "json", "github"),
        default="text",
        help="text for people (default), json for tools, github for GitHub Actions annotations.",
    )
    validate.add_argument(
        "--strict",
        action="store_true",
        help="Fail on warnings too.",
    )
    validate.set_defaults(handler=run_validate)

    build = commands.add_parser(
        "build",
        help="Compile plugins into Django apps.",
        description=(
            "Check plugins, then compile them into Django apps: a package per plugin "
            "(iki_plugin_<name>) with its models and migrations, and iki_installed_plugins, "
            "which the server installs. Give every plugin the deployment installs."
        ),
    )
    build.add_argument("directories", nargs="+", type=Path, metavar="DIRECTORY")
    build.add_argument(
        "--output",
        "-o",
        required=True,
        type=Path,
        metavar="DIRECTORY",
        help="Where to write the packages (put it on the server's PYTHONPATH).",
    )
    build.set_defaults(handler=run_build)

    makemigrations = commands.add_parser(
        "makemigrations",
        help="Make migrations for a plugin's changes.",
        description=(
            "Compile the plugin and the plugins it depends on, then run Django's makemigrations "
            "on it, asking about renames. New migrations are written to the plugin's "
            "migrations/ directory, to be committed with the change. Needs no database."
        ),
    )
    makemigrations.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="The plugin's directory (default: the current directory).",
    )
    makemigrations.add_argument(
        "--with",
        dest="dependencies",
        action="append",
        default=[],
        type=Path,
        metavar="DIRECTORY",
        help="A plugin this one depends on, directly or not. Repeat for each.",
    )
    makemigrations.add_argument("--name", help="A name for the migration.")
    makemigrations.add_argument(
        "--check",
        action="store_true",
        help="Write nothing; fail if the manifests have changes no migration has (for CI).",
    )
    makemigrations.add_argument(
        "--dry-run", action="store_true", help="Show what the migration would do; write nothing."
    )
    makemigrations.add_argument(
        "--no-input",
        action="store_true",
        help="Ask nothing: no renames, and fail where Django needs an answer.",
    )
    makemigrations.add_argument(
        "--merge", action="store_true", help="Merge migrations that branched (two leaves)."
    )
    makemigrations.add_argument(
        "--empty", action="store_true", help="Write an empty migration, to fill in by hand."
    )
    makemigrations.set_defaults(handler=run_makemigrations)
    plugin.set_defaults(handler=lambda _args: _help(plugin))


def _help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def run_validate(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    directory: Path = args.directory
    result = load_plugin(directory, dependencies=args.dependencies)
    problems = list(result.problems)
    sources: dict[str, Path] = {p.file: directory / p.file for p in problems if p.file}
    if args.settings is not None and result.plugin is not None:
        name = str(args.settings)
        settings = check_settings_values(result.plugin, args.settings, name)
        problems.extend(settings)
        sources[name] = args.settings
    errors = sum(p.is_error for p in problems)
    warnings = len(problems) - errors
    failed = errors > 0 or (args.strict and warnings > 0)
    if args.format == "json":
        _json(out, result, problems, directory, failed=failed)
    elif args.format == "github":
        _github(out, problems, directory, sources)
        out.write(_summary(result, errors, warnings, color=False) + "\n")
    else:
        _text(out, result, problems, directory, sources, errors, warnings)
    return 1 if failed else 0


def _load(directory: Path, dependencies: Sequence[Path], out: TextIO) -> Result | None:
    """The plugin, or None after printing why it can't be built."""
    result = load_plugin(directory, dependencies=dependencies)
    if result.plugin is not None:
        return result
    problems = list(result.problems)
    sources = {p.file: directory / p.file for p in problems if p.file}
    errors = sum(p.is_error for p in problems)
    _text(out, result, problems, directory, sources, errors, len(problems) - errors)
    return None


def run_build(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    directories: list[Path] = args.directories
    missing = _missing_dependencies(directories)
    if missing:
        sys.stderr.write("".join(f"error: {m}\n" for m in missing))
        return 1
    plugins = []
    for directory in directories:
        others = [d for d in directories if d != directory]
        result = _load(directory, others, sys.stderr)
        if result is None:
            return 1
        assert result.plugin is not None  # noqa: S101 - _load returns loaded plugins
        plugins.append((result.plugin, directory))
    try:
        packages = build_apps(
            [p for p, _ in plugins],
            args.output,
            migrations={p.name: d / "migrations" for p, d in plugins},
            # Built without a deployment, settings take their defaults, as they do when a
            # deployment leaves them out.
            settings={p.name: _defaults(p) for p, _ in plugins},
        )
    except ValueError as error:
        sys.stderr.write(f"error: {error}.\n")
        return 1
    for package in packages:
        out.write(f"Built {package}\n")
    return 0


def _defaults(plugin: Plugin) -> dict[str, Any]:
    return {
        name: setting.default for name, setting in plugin.settings.items() if setting.has_default
    }


def _missing_dependencies(directories: Sequence[Path]) -> list[str]:
    """Plugins the given ones depend on but that weren't given, from each plugin.yaml."""
    declared: dict[str, list[str]] = {}
    for directory in directories:
        documents, _ = read_documents(directory / "plugin.yaml", "plugin.yaml")
        data = documents[0].data if documents else None
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            dependencies = data.get("dependencies")
            declared[data["name"]] = sorted(dependencies) if isinstance(dependencies, dict) else []
    return [
        f"{name} needs the {dependency} plugin: give its directory too."
        for name, dependencies in declared.items()
        for dependency in dependencies
        if dependency not in declared
    ]


def run_makemigrations(args: argparse.Namespace) -> int:
    directory: Path = args.directory
    dependencies: list[Path] = args.dependencies
    result = _load(directory, dependencies, sys.stderr)
    if result is None:
        return 1
    plugin = result.plugin
    assert plugin is not None  # noqa: S101 - _load returns loaded plugins
    plugins = [(plugin, directory)]
    for dependency in dependencies:
        loaded = _load(dependency, [d for d in dependencies if d != dependency], sys.stderr)
        if loaded is None:
            return 1
        assert loaded.plugin is not None  # noqa: S101 - _load returns loaded plugins
        if not any((dependency / "migrations").glob("0*.py")):
            sys.stderr.write(
                f"error: {loaded.plugin.name} has no migrations. Run "
                f"`iki plugin makemigrations` in {dependency} first.\n"
            )
            return 1
        plugins.append((loaded.plugin, dependency))
    with tempfile.TemporaryDirectory(prefix="iki-build-") as tmp:
        try:
            build_apps(
                [p for p, _ in plugins],
                Path(tmp),
                migrations={p.name: d / "migrations" for p, d in plugins},
            )
        except ValueError as error:
            sys.stderr.write(f"error: {error}. Pass it with --with.\n")
            return 1
        built = Path(tmp) / naming.package(plugin.name) / "migrations"
        before = {f.name for f in built.glob("*.py")}
        target = directory / "migrations"
        command = [sys.executable, "-m", "iki_server.plugins.makemigrations", plugin.name]
        command += [f"--name={args.name}"] if args.name else []
        for flag in ("check", "dry_run", "no_input", "merge", "empty"):
            if getattr(args, flag):
                command.append("--" + flag.replace("_", "-"))
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, [tmp, os.environ.get("PYTHONPATH")])),
            "PYTHONDONTWRITEBYTECODE": "1",
            "IKI_MIGRATIONS_DISPLAY_DIR": _display_path(target, None, {}),
        }
        code = subprocess.run(command, env=env, check=False).returncode  # noqa: S603 - our own module, with this interpreter
        new = sorted(f for f in built.glob("*.py") if f.name not in before)
        if new and not (args.check or args.dry_run):
            target.mkdir(exist_ok=True)
            for file in new:
                shutil.copyfile(file, target / file.name)
    return code


# --- Output --------------------------------------------------------------------------------------


def _display_path(directory: Path, file: str | None, sources: dict[str, Path]) -> str:
    """Relative to the current directory when inside it (so editors can open it), else as given."""
    if file is None:
        return str(directory)
    path = sources.get(file, directory / file)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


class _Style:
    def __init__(self, out: TextIO) -> None:
        force = os.environ.get("FORCE_COLOR") not in (None, "", "0")
        self.on = "NO_COLOR" not in os.environ and (force or (out.isatty()))

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


def _text(
    out: TextIO,
    result: Result,
    problems: Sequence[Problem],
    directory: Path,
    sources: dict[str, Path],
    errors: int,
    warnings: int,
) -> None:
    style = write_problems(out, problems, directory, sources)
    out.write(_summary(result, errors, warnings, color=style.on) + "\n")


def write_problems(
    out: TextIO,
    problems: Sequence[Problem],
    directory: Path,
    sources: dict[str, Path],
    *,
    label: str | None = None,
) -> _Style:
    """Each problem the way compilers print them. ``label`` names files instead of their paths."""
    style = _Style(out)
    lines_cache: dict[str, list[str]] = {}
    for problem in problems:
        if label is not None:
            where = f"{label}/{problem.file}" if problem.file else label
        else:
            where = _display_path(directory, problem.file, sources)
        if problem.location:
            where += f":{problem.location.line}:{problem.location.column}"
        level = style("error", "1;31") if problem.is_error else style("warning", "1;33")
        out.write(
            f"{style(where, '1')}: {level}: {problem.message} {style(f'[{problem.code}]', '2')}\n"
        )
        if problem.file and problem.location:
            source = _source_line(
                sources, directory, problem.file, problem.location.line, lines_cache
            )
            if source is not None:
                number = str(problem.location.line)
                gutter = " " * len(number)
                caret = " " * (problem.location.column - 1) + "^"
                out.write(f" {style(number, '2')} | {source}\n")
                out.write(f" {gutter} | {style(caret, '1;31' if problem.is_error else '1;33')}\n")
        if problem.hint:
            out.write(f"  {style('hint', '1;36')}: {problem.hint}\n")
        out.write("\n")
    return style


def _source_line(
    sources: dict[str, Path], directory: Path, file: str, line: int, cache: dict[str, list[str]]
) -> str | None:
    if file not in cache:
        path = sources.get(file, directory / file)
        try:
            cache[file] = path.read_text("utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            cache[file] = []
    lines = cache[file]
    if 0 < line <= len(lines):
        return lines[line - 1].expandtabs(1)
    return None


def _summary(result: Result, errors: int, warnings: int, *, color: bool) -> str:
    plugin = result.plugin
    if errors:
        counts = _count(errors, "error") + (
            f" and {_count(warnings, 'warning')}" if warnings else ""
        )
        return f"{counts} in {result.directory}."
    assert plugin is not None  # noqa: S101 - a plugin without errors was read
    parts = [
        _count(len(plugin.types), "entity type"),
        _count(len(plugin.extensions), "extension"),
        _count(len(plugin.settings), "setting"),
        _count(len(plugin.actions), "action"),
        _count(len(plugin.environments), "environment"),
    ]
    tail = f", with {_count(warnings, 'warning')}" if warnings else ""
    mark = "\033[1;32m✓\033[0m" if color else "✓"
    return f"{mark} {plugin.name} {plugin.version} is valid: {', '.join(parts)}{tail}."


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _github(
    out: TextIO, problems: Sequence[Problem], directory: Path, sources: dict[str, Path]
) -> None:
    for problem in problems:
        level = "error" if problem.is_error else "warning"
        properties = [f"title={_escape_property(problem.code)}"]
        if problem.file:
            properties.insert(
                0, f"file={_escape_property(_display_path(directory, problem.file, sources))}"
            )
        if problem.location:
            properties.insert(1, f"line={problem.location.line}")
            properties.insert(2, f"col={problem.location.column}")
        message = problem.message + (f"\n{problem.hint}" if problem.hint else "")
        out.write(f"::{level} {','.join(properties)}::{_escape_data(message)}\n")


def _escape_data(text: str) -> str:
    # https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _json(
    out: TextIO, result: Result, problems: Sequence[Problem], directory: Path, *, failed: bool
) -> None:
    plugin = result.plugin
    document = {
        "apiVersion": API_VERSION,
        "directory": str(directory),
        "valid": not failed,
        "plugin": (
            {
                "name": plugin.name,
                "version": plugin.version,
                "types": sorted(plugin.types),
                "extensions": [e.extends_type for e in plugin.extensions],
                "settings": sorted(plugin.settings),
                "actions": sorted(plugin.actions),
                "environments": sorted(plugin.environments),
            }
            if plugin
            else None
        ),
        "errors": sum(p.is_error for p in problems),
        "warnings": sum(not p.is_error for p in problems),
        "problems": [p.as_dict() for p in problems],
    }
    json.dump(document, out, indent=2, ensure_ascii=False)
    out.write("\n")

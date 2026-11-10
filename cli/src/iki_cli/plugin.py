"""`iki plugin …`: commands for plugin authors."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from iki_spec import API_VERSION
from iki_spec.problems import Problem
from iki_spec.validate import Result, check_settings_values, load_plugin


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
    style = _Style(out)
    lines_cache: dict[str, list[str]] = {}
    for problem in problems:
        where = _display_path(directory, problem.file, sources)
        if problem.location:
            where += f":{problem.location.line}:{problem.location.column}"
        label = style("error", "1;31") if problem.is_error else style("warning", "1;33")
        out.write(
            f"{style(where, '1')}: {label}: {problem.message} {style(f'[{problem.code}]', '2')}\n"
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
    out.write(_summary(result, errors, warnings, color=style.on) + "\n")


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

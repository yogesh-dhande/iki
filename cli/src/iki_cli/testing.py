"""`iki plugin test`: run a plugin's action tests through ``iki-worker run --local`` (ADR 0093)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from iki_spec import action_tests, outputs, schemas
from iki_spec.action_tests import ActionTest
from iki_spec.builtin import core_plugin
from iki_spec.model import Action, EntityType, Extension, Plugin

#: How much of a failing command's log is shown.
LOG_LINES = 20


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    test = commands.add_parser(
        "test",
        help="Run a plugin's action tests.",
        description=(
            f"Run each action test in the plugin's {action_tests.DIRECTORY}/ directory: make the "
            "run input the server would, run the action with `iki-worker run --local`, and "
            "compare what it writes with what the test expects. The commands run on this "
            "machine, so install what their environments need first."
        ),
    )
    test.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="The plugin's directory (default: the current directory).",
    )
    test.add_argument(
        "--with",
        dest="dependencies",
        action="append",
        default=[],
        type=Path,
        metavar="DIRECTORY",
        help="A plugin this one depends on, directly or not. Repeat for each.",
    )
    test.add_argument(
        "-k",
        dest="select",
        metavar="TEXT",
        help="Run only the tests whose name contains this.",
    )
    test.add_argument(
        "--worker",
        type=Path,
        metavar="PATH",
        help="The iki-worker binary (default: $IKI_WORKER, or iki-worker on the PATH).",
    )
    test.add_argument(
        "--upgrade-from",
        metavar="REF",
        help=(
            "Also test upgrading from the plugin at this git commit (ADR 0094): migrate a "
            "scratch database to it, make its action tests' entities, migrate to the plugin "
            "now, and roll back."
        ),
    )
    test.add_argument(
        "--database",
        metavar="URL",
        help=(
            "A PostgreSQL server to make the scratch database on, as a postgres:// URL to a "
            "database there (default: $IKI_TEST_DATABASE_URL). Its user needs CREATEDB."
        ),
    )
    test.add_argument(
        "--no-rollback",
        action="store_true",
        help="Don't roll the upgrade back: for one meant to be one-way.",
    )
    test.set_defaults(handler=run_test)


def run_test(args: argparse.Namespace, out: TextIO | None = None) -> int:
    from iki_cli.plugin import _load, _missing_dependencies, _text

    out = out or sys.stdout
    err = sys.stderr
    directory: Path = args.directory
    missing = _missing_dependencies([directory, *args.dependencies])
    if missing:
        err.write(
            "".join(
                f"error: {m.replace('give its directory too', 'give it with --with')}\n"
                for m in missing
            )
        )
        return 1
    result = _load(directory, args.dependencies, err)
    if result is None:
        return 1
    plugin = result.plugin
    assert plugin is not None  # noqa: S101 - _load returns loaded plugins
    found = _types(args.dependencies, err)
    if found is None:
        return 1
    types, extensions = found
    installed = outputs.SpecTypes(
        {**types, **{t.qualified_name: t for t in plugin.types.values()}},
        [*extensions, *plugin.extensions],
    )
    loaded = action_tests.load_tests(directory, plugin, types)
    if not loaded.ok:
        sources = {p.file: directory / p.file for p in loaded.problems if p.file}
        errors = sum(p.is_error for p in loaded.problems)
        _text(err, result, loaded.problems, directory, sources, errors, 0)
        return 1
    tests = [t for t in loaded.tests if not args.select or args.select in t.name]
    failed = False
    if tests:
        worker = _worker(args.worker, err)
        if worker is None:
            return 1
        failed = not _action_tests(tests, plugin, types, installed, directory, worker, out)
    elif not args.upgrade_from:
        matching = f" matching {args.select}" if args.select else ""
        out.write(
            f"{plugin.name} has no action tests{matching}. "
            f"Add them to {action_tests.DIRECTORY}/ (ADR 0093).\n"
        )
        return 1 if args.select else 0
    if args.upgrade_from:
        from iki_cli import upgrade_test

        upgraded = upgrade_test.run(
            directory,
            args.dependencies,
            plugin,
            types,
            args.upgrade_from,
            database=args.database or os.environ.get("IKI_TEST_DATABASE_URL"),
            rollback=not args.no_rollback,
            out=out,
            err=err,
        )
        failed = failed or not upgraded
    return 1 if failed else 0


def _action_tests(
    tests: list[ActionTest],
    plugin: Plugin,
    types: dict[str, EntityType],
    installed: outputs.Types,
    directory: Path,
    worker: Path,
    out: TextIO,
) -> bool:
    from iki_cli.plugin import _defaults

    out.write(f"Running {len(tests)} test(s) of {plugin.name} {plugin.version}:\n")
    started = time.monotonic()
    failed = 0
    settings = _defaults(plugin)
    for test in tests:
        took, problems, log = _run_one(test, plugin, types, installed, directory, settings, worker)
        mark = "ok    " if not problems else "FAILED"
        out.write(f"  {mark}  {test.name}  ({took:.1f}s)\n")
        if problems:
            failed += 1
            out.write(f"          {test.file}\n")
            out.write("".join(f"          {p}\n" for p in problems))
            if log:
                out.write("          Its log ended:\n")
                out.write("".join(f"          | {line}\n" for line in log[-LOG_LINES:]))
    took = time.monotonic() - started
    passed = len(tests) - failed
    out.write(f"{passed} passed{f', {failed} failed' if failed else ''} in {took:.1f}s.\n")
    return not failed


def _types(
    dependencies: Sequence[Path], err: TextIO
) -> tuple[dict[str, EntityType], list[Extension]] | None:
    """Every type the plugin can see, its dependencies' and the core plugin's, and their
    extensions."""
    from iki_cli.plugin import _load

    core = core_plugin()
    found = {t.qualified_name: t for t in core.types.values()}
    extensions = list(core.extensions)
    for dependency in dependencies:
        others = [d for d in dependencies if d != dependency]
        result = _load(dependency, others, err)
        if result is None:
            return None
        assert result.plugin is not None  # noqa: S101 - _load returns loaded plugins
        found.update({t.qualified_name: t for t in result.plugin.types.values()})
        extensions.extend(result.plugin.extensions)
    return found, extensions


def _worker(given: Path | None, err: TextIO) -> Path | None:
    named = given or (Path(os.environ["IKI_WORKER"]) if os.environ.get("IKI_WORKER") else None)
    if named is not None:
        if named.is_file() and os.access(named, os.X_OK):
            return named
        err.write(f"error: {named} isn't an iki-worker binary this can run.\n")
        return None
    found = shutil.which("iki-worker")
    if found is None:
        err.write(
            "error: iki plugin test runs actions with iki-worker, which isn't on the PATH. "
            "Install it, or give it with --worker or $IKI_WORKER.\n"
        )
        return None
    return Path(found)


def _run_one(
    test: ActionTest,
    plugin: Plugin,
    types: dict[str, EntityType],
    installed: outputs.Types,
    directory: Path,
    settings: dict[str, Any],
    worker: Path,
) -> tuple[float, list[str], list[str]]:
    """How long it took, what's wrong (nothing if it passed), and the command's log."""
    started = time.monotonic()
    root = os.environ.get("IKI_WORK_DIR")
    try:
        if root is not None:
            Path(root).mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix="iki-test-", dir=root)
    except OSError as error:
        return time.monotonic() - started, [f"Couldn't create the test directory: {error}."], []
    with scratch as tmp:
        work = Path(tmp)
        given = action_tests.run_input(test, plugin, types, directory, settings, work)
        (work / "input.json").write_text(json.dumps(given, indent=2), "utf-8")
        done = subprocess.run(  # noqa: S603 - the iki-worker binary, on our own input
            [
                str(worker),
                "run",
                "--local",
                str(work / "input.json"),
                "--plugin",
                str(directory.resolve()),
                "--timeout",
                str(test.action.timeout),
                "--keep",
            ],
            env={**os.environ, "IKI_WORK_DIR": str(work)},
            capture_output=True,
            text=True,
            check=False,
        )
        log = done.stderr.splitlines()
        # Judged while the files the command wrote, a creates file among them, are here.
        judged = _judge(test, installed, done.returncode, done.stdout, log)
    return time.monotonic() - started, judged, log


def _judge(
    test: ActionTest, installed: outputs.Types, code: int, stdout: str, log: list[str]
) -> list[str]:
    if code == 2:
        return ["iki-worker couldn't run it (see its log)."]
    if code == 130:
        return ["It was interrupted."]
    said = next(
        (line.split("::error", 1)[1].strip() for line in reversed(log) if "::error" in line), None
    )
    if test.error is not None:
        if code == 0:
            return [f"It succeeded, but should have failed with “{test.error}”."]
        if said is None or test.error not in said:
            return [
                f"It failed, but said {f'“{said}”' if said else 'nothing'}, not “{test.error}”."
            ]
        return []
    if code != 0:
        return [f"It failed: {said}" if said else "It failed, and said nothing about why."]
    try:
        output = json.loads(stdout)
    except ValueError:
        return ["Its output isn't JSON."]
    problems = [
        f"output.json: {issue.message}"
        for issue in schemas.check("run-output", _as_written(output))
    ]
    well_formed = not problems
    problems += action_tests.effects(test.action, output)
    if well_formed:
        # As the server checks them, so a test fails where a real run's output is refused.
        creates = output.get("creates") or []
        problems += [
            f"output.json at {p.pointer}: {p.detail}"
            for p in [
                *outputs.created_refs_in_updates(
                    test.action, output.get("updates") or {}, installed
                ),
                *outputs.check_creates(test.action, creates, installed),
            ]
        ]
        problems += _creates_file(test.action, output.get("creates_file"), creates, installed)
    if test.output is not None:
        problems += action_tests.compare(test.output, output, test.tolerance)
    return problems


def _creates_file(
    action: Action,
    described: Any,  # noqa: ANN401 - JSON data
    creates: list[dict[str, Any]],
    installed: outputs.Types,
) -> list[str]:
    """A bulk run's creates file (ADR 0056), each line checked as the server checks it
    before writing it. ``described``: the file, as ``iki-worker run --local`` describes
    one the command wrote."""
    if described is None:
        return []
    path = Path(described["path"]) if isinstance(described, dict) and "path" in described else None
    if path is None or not path.is_file():
        return ["output.json: its creates_file isn't a file the command wrote."]
    name = described.get("$file") or path.name
    with path.open("rb") as lines:
        found = outputs.check_creates_file(action, lines, creates, installed)
    return [f"{name} at {p.pointer}: {p.detail}" for p in found]


def _as_written(value: Any) -> Any:  # noqa: ANN401 - JSON data
    """The output as the command wrote it: ``iki-worker run --local`` describes each
    ``{"$file": …}`` with its name, size, SHA-256 and path, which a test can compare."""
    if isinstance(value, dict):
        if "$file" in value and "sha256" in value:
            return {"$file": value["$file"]}
        return {k: _as_written(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_as_written(v) for v in value]
    return value

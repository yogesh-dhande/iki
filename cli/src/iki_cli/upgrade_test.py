"""`iki plugin test --upgrade-from REF`: upgrade sample data from an earlier commit (ADR 0094).

It builds the plugin as it was at REF and as it is now, makes a scratch database, and
runs :mod:`iki_server.plugins.upgrade_test` there for each step, with that step's build:
``before`` (the earlier build, making the samples), ``after`` (the current one), and
``rollback`` (the earlier one again).

Plugins were named without a namespace before ADR 0113, and no deployment ran them: a commit
from then has nothing to upgrade from, so it's skipped, as is one where the plugin had another
name.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit, urlunsplit

from iki_codegen import build as build_apps
from iki_spec import action_tests
from iki_spec.model import EntityType, Field, Plugin
from iki_spec.yamlsource import read_documents

#: How much of a failing step's log is shown.
LOG_LINES = 20


def run(
    directory: Path,
    dependencies: Sequence[Path],
    plugin: Plugin,
    types: dict[str, EntityType],
    ref: str,
    *,
    database: str | None,
    rollback: bool,
    out: TextIO,
    err: TextIO,
) -> bool:
    """Whether the upgrade from ``ref`` keeps the samples, and rolls back if asked."""
    if not database:
        err.write(
            "error: an upgrade test needs PostgreSQL: give a server with --database or "
            "$IKI_TEST_DATABASE_URL, such as postgres://postgres@localhost:5432/postgres.\n"
        )
        return False
    with tempfile.TemporaryDirectory(prefix="iki-upgrade-") as tmp:
        work = Path(tmp)
        found = _checkout(directory, ref, work / "earlier", err)
        if found is None:
            return False
        sha, earlier_dir = found
        if earlier_dir is None:
            out.write(f"{plugin.name} isn't at {ref} ({sha[:12]}): there's nothing to upgrade.\n")
            return True
        named = _name(earlier_dir)
        if named != plugin.name:
            why = (
                f"it was `{named}`, from before plugins had namespaces (ADR 0113), which no "
                "deployment ran"
                if named is not None and "/" not in named
                else f"it was named `{named}` then"
            )
            out.write(
                f"{plugin.name} wasn't at {ref} ({sha[:12]}): {why}, so there's nothing to "
                "upgrade from.\n"
            )
            return True
        earlier = _built(earlier_dir, dependencies, work / "build-earlier", err)
        current = _built(directory, dependencies, work / "build-current", err)
        if earlier is None or current is None:
            return False
        samples = _samples(earlier_dir, earlier, types)
        steps = [
            (
                "before",
                work / "build-earlier",
                "migrated to the earlier commit and made the samples",
            ),
            ("after", work / "build-current", "upgraded: the samples and their history are intact"),
        ]
        if rollback:
            steps.append(
                ("rollback", work / "build-earlier", "rolled back: the samples have their values")
            )
        state = work / "state.json"
        state.write_text(json.dumps({"samples": samples}), "utf-8")
        scratch = _scratch(database, err)
        with scratch as url:
            if url is None:
                return False
            out.write(
                f"Upgrading {plugin.name} from {sha[:12]} ({earlier.version}) to "
                f"{plugin.version} on the scratch database {scratch.name}, with "
                f"{len(samples)} sample(s):\n"
            )
            if not samples:
                out.write(
                    f"          The earlier commit has no action tests with entities in "
                    f"{action_tests.DIRECTORY}/, so only its migrations run.\n"
                )
            unversioned = _unversioned(earlier, plugin)
            out.write(
                f"  {'ok    ' if not unversioned else 'FAILED'}  each type whose fields changed "
                "has a higher schema_version\n"
            )
            if unversioned:
                out.write("".join(f"          {p}\n" for p in unversioned))
                return False
            if earlier.version == plugin.version and _migrations(earlier_dir) != _migrations(
                directory
            ):
                out.write(
                    f"          Note: the migrations changed, and {plugin.name}'s version is still "
                    f"{plugin.version}. Increase it when you release this, so a deployment's "
                    "history tells the two apart.\n"
                )
            for step, build, done in steps:
                started = time.monotonic()
                problems, log = _step(step, build, url, state)
                took = time.monotonic() - started
                out.write(f"  {'ok    ' if not problems else 'FAILED'}  {done}  ({took:.1f}s)\n")
                if problems:
                    out.write("".join(f"          {p}\n" for p in problems))
                    if log:
                        out.write("          Its log ended:\n")
                        out.write("".join(f"          | {line}\n" for line in log[-LOG_LINES:]))
                    return False
        if scratch.left:
            return False
    return True


#: A field's attributes that change only how it's shown or found, not what it holds (ADR 0095).
_SHOWN = frozenset({"label", "description", "display_unit", "link", "indexed"})


def _unversioned(earlier: Plugin, current: Plugin) -> list[str]:
    """Each type whose entities may hold something new without a higher schema_version."""
    out = []
    for name, now in current.types.items():
        then = earlier.types.get(name)
        if then is None:
            continue
        if now.schema_version < then.schema_version:
            out.append(
                f"{name}'s schema_version went down, from {then.schema_version} to "
                f"{now.schema_version}. Schema versions only go up: plugins that depend on "
                f"{current.name} compare them."
            )
            continue
        changes = _changes(then, now)
        if changes and now.schema_version == then.schema_version:
            out.append(
                f"{name} changed ({'; '.join(changes)}), and its schema_version is still "
                f"{now.schema_version}. Increase it to {now.schema_version + 1}: plugins that "
                f"depend on {current.name} pin its types' schema versions."
            )
    return out


def _changes(then: EntityType, now: EntityType) -> list[str]:
    old, new = then.current_fields, now.current_fields
    out = [f"added {n}" for n in new if n not in old]
    out += [f"removed {n}" for n in old if n not in new]
    for n in (n for n in new if n in old):
        a, b = _held(old[n]), _held(new[n])
        changed = [k for k in a if a[k] != b[k]]
        if changed:
            out.append(f"changed {n}'s {', '.join(changed)}")
    out += [
        f"changed its {attr}"
        for attr in ("extends", "abstract", "identity")
        if getattr(then, attr) != getattr(now, attr)
    ]
    return out


def _held(f: Field) -> dict[str, Any]:
    """What a field may hold: its attributes but those only about showing it."""
    return {
        k.name: _held(v) if isinstance(v := getattr(f, k.name), Field) else v
        for k in dataclasses.fields(f)
        if k.name not in _SHOWN
    }


def _migrations(directory: Path) -> dict[str, bytes]:
    folder = directory / "migrations"
    if not folder.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*.py"))}


def _checkout(directory: Path, ref: str, into: Path, err: TextIO) -> tuple[str, Path | None] | None:
    """The commit's SHA, and the plugin's directory as it was then: None if it wasn't there,
    as for a pull request that adds it."""
    git = ["git", "-C", str(directory)]
    top = _git([*git, "rev-parse", "--show-toplevel"])
    sha = _git([*git, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"])
    if top is None or sha is None:
        err.write(f"error: {ref} isn't a commit of the git repository {directory} is in.\n")
        return None
    inside = directory.resolve().relative_to(Path(top).resolve()).as_posix()
    archive = subprocess.run(  # noqa: S603 - git, with a SHA it resolved
        [*git[:1], "-C", top, "archive", "--format=tar", sha, "--", inside],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        return sha, None
    into.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    earlier = into / inside
    return sha, earlier if (earlier / "plugin.yaml").is_file() else None


def _name(directory: Path) -> str | None:
    """The name the plugin.yaml in ``directory`` gives, if it gives one."""
    documents, _ = read_documents(directory / "plugin.yaml", "plugin.yaml")
    data = documents[0].data if documents else None
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def _git(command: list[str]) -> str | None:
    done = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603 - git
    return done.stdout.strip() if done.returncode == 0 else None


def _built(
    directory: Path, dependencies: Sequence[Path], output: Path, err: TextIO
) -> Plugin | None:
    """Builds the plugin with its dependencies into ``output``; returns the plugin."""
    from iki_cli.plugin import _defaults, _load

    loaded = []
    for d in [directory, *dependencies]:
        others = [o for o in [directory, *dependencies] if o != d]
        result = _load(d, others, err)
        if result is None or result.plugin is None:
            return None
        loaded.append((result.plugin, d))
    try:
        build_apps(
            [p for p, _ in loaded],
            output,
            migrations={p.name: d / "migrations" for p, d in loaded},
            sources={p.name: d for p, d in loaded},
            settings={p.name: _defaults(p) for p, _ in loaded},
        )
    except ValueError as error:
        err.write(f"error: {error}.\n")
        return None
    return loaded[0][0]


def _samples(directory: Path, plugin: Plugin, types: dict[str, EntityType]) -> list[dict[str, Any]]:
    """The earlier commit's action tests' input entities, without values that name files."""
    loaded = action_tests.load_tests(directory, plugin, types)
    out = []
    for test in loaded.tests:
        for name, entities in test.inputs.items():
            for n, entity in enumerate(entities, start=1):
                values = {k: v for k, v in entity["values"].items() if not _names_a_file(v)}
                out.append(
                    {
                        "type": entity.get("type") or test.action.inputs[name].type,
                        "values": values,
                        "from": f"{test.name}'s {name} {n}",
                    }
                )
    return out


def _names_a_file(value: Any) -> bool:  # noqa: ANN401 - YAML data
    if isinstance(value, dict):
        return action_tests.FILE_KEY in value or any(_names_a_file(v) for v in value.values())
    if isinstance(value, list):
        return any(_names_a_file(v) for v in value)
    return False


class _scratch:  # noqa: N801 - used as a context manager, like contextlib's
    """A database of its own on the given server, dropped after, passed or not."""

    def __init__(self, url: str, err: TextIO) -> None:
        self.url = url
        self.err = err
        self.name = f"iki_upgrade_{secrets.token_hex(4)}"
        self.made = False
        #: Whether it couldn't be dropped.
        self.left = False

    def __enter__(self) -> str | None:
        import psycopg
        from psycopg import sql

        try:
            with psycopg.connect(self.url, autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
        except psycopg.Error as error:
            self.err.write(
                f"error: couldn't make a scratch database on {_safe(self.url)}: {error}\n"
            )
            return None
        self.made = True
        parts = urlsplit(self.url)
        return urlunsplit(parts._replace(path=f"/{self.name}"))

    def __exit__(self, *_exc: object) -> None:
        if not self.made:
            return
        import psycopg
        from psycopg import sql

        try:
            with psycopg.connect(self.url, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.name))
                )
        except psycopg.Error as error:
            self.left = True
            self.err.write(
                f"error: couldn't drop the scratch database {self.name} on {_safe(self.url)}: "
                f"{error}. Drop it yourself with `DROP DATABASE {self.name} WITH (FORCE)`.\n"
            )


def _safe(url: str) -> str:
    """The URL without its password, to print."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"{parts.username}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


def _step(step: str, build: Path, url: str, state: Path) -> tuple[list[str], list[str]]:
    """What's wrong after the step (nothing if it passed), and its log."""
    before = json.loads(state.read_text("utf-8")).get("problems", [])
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(build), os.environ.get("PYTHONPATH")])),
        "PYTHONDONTWRITEBYTECODE": "1",
        "IKI_HARNESS_DATABASE_URL": url,
    }
    done = subprocess.run(  # noqa: S603 - our own module, with this interpreter
        [sys.executable, "-m", "iki_server.plugins.upgrade_test", step, str(state)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return ["It crashed."], (done.stdout + done.stderr).splitlines()
    found = json.loads(state.read_text("utf-8"))
    problems = found.get("problems", [])[len(before) :]
    return problems, str(found.get("logs", {}).get(step, "")).splitlines()

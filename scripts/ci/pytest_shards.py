"""Runs one CI shard of the Python tests: ``pytest -p pytest_shards --shard plain:2/4``.

CI splits the suite into kinds by the setup a test needs, and each kind into shards that
run as jobs side by side:

- ``browser``: marked ``built_web_app`` or ``browser``. They need Playwright browsers.
- ``worker``: marked ``worker_e2e``. They run iki-worker, which cargo builds.
- ``plain``: everything else, which needs Python, Postgres, Docker and iki-wasm (which
  every shard builds: saves evaluate plugins' rules with it).

Every shard collects the whole suite, then keeps the tests of its kind and its share of
them. A test file is never split across a kind's shards: its module-scoped fixtures (a
server, a browser) start once. Files are dealt out longest first, by their recorded
durations (``test-durations.json``, which CI's Python summary job refreshes), to whichever
shard has least so far; a file with no recording counts its tests at the average recorded
time of one. The same collection gives the same split in every shard, and in each of
pytest-xdist's workers.

With ``--shard-report DIR`` the shard writes what it planned and what it ran there;
``shard_summary.py`` checks across the shards that every test ran in exactly one.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

KINDS = ("plain", "worker", "browser")
DURATIONS = Path(__file__).with_name("test-durations.json")
PLAN = "plan.json"
RAN = "ran.json"

_SHARD = re.compile(r"^(?P<kind>[a-z]+):(?P<index>[0-9]+)/(?P<count>[0-9]+)$")


@dataclass(frozen=True)
class Shard:
    kind: str
    index: int
    count: int

    @classmethod
    def parse(cls, value: str) -> Shard:
        match = _SHARD.match(value)
        if match is None or match["kind"] not in KINDS:
            raise pytest.UsageError(
                f"--shard {value!r}: expected KIND:K/N, KIND one of {', '.join(KINDS)}"
            )
        shard = cls(match["kind"], int(match["index"]), int(match["count"]))
        if not 1 <= shard.index <= shard.count:
            raise pytest.UsageError(f"--shard {value!r}: K must be between 1 and N")
        return shard


def kind_of(markers: Iterable[str]) -> str:
    """The kind of a test with these markers: the setup its shard needs."""
    names = set(markers)
    if names & {"built_web_app", "browser"}:
        return "browser"
    if "worker_e2e" in names:
        return "worker"
    return "plain"


def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def assign(
    kinds: Mapping[str, str], kind: str, count: int, durations: Mapping[str, float]
) -> dict[str, int]:
    """Deals the tests of ``kind`` out to shards 1 to ``count``, a whole file at a time.

    ``kinds`` maps every collected test to its kind; ``durations`` the seconds a file's
    tests of this kind took when they last ran.
    """
    files: dict[str, list[str]] = defaultdict(list)
    for nodeid, test_kind in kinds.items():
        if test_kind == kind:
            files[file_of(nodeid)].append(nodeid)
    recorded = [f for f in files if f in durations]
    tests = sum(len(files[f]) for f in recorded)
    # A file with no recording yet counts its tests at the average recorded time of one.
    each = sum(durations[f] for f in recorded) / tests if tests else 1.0
    weight = {f: durations[f] if f in durations else len(ids) * each for f, ids in files.items()}
    loads = [0.0] * count
    shards: dict[str, int] = {}
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        least = min(range(count), key=lambda i: (loads[i], i))
        loads[least] += weight[f]
        for nodeid in files[f]:
            shards[nodeid] = least + 1
    return shards


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def recorded_durations(path: Path, python: str, kind: str) -> dict[str, float]:
    """The seconds each file's tests of this kind took, on this Python, when last recorded."""
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {str(f): float(s) for f, s in recorded.get(python, {}).get(kind, {}).items()}


_SHARD_KEY = pytest.StashKey[Shard]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("shards", "CI shards (scripts/ci/pytest_shards.py)")
    group.addoption(
        "--shard",
        metavar="KIND:K/N",
        help=f"Run shard K of N of the tests of a kind ({', '.join(KINDS)}).",
    )
    group.addoption(
        "--shard-report",
        metavar="DIR",
        help="Write the shard's plan and the tests it ran to DIR, for shard_summary.py.",
    )


def pytest_configure(config: pytest.Config) -> None:
    value = config.getoption("--shard")
    report = config.getoption("--shard-report")
    if not value:
        if report:
            raise pytest.UsageError("--shard-report needs --shard")
        return
    shard = Shard.parse(value)
    config.stash[_SHARD_KEY] = shard
    # pytest-xdist's controller sees every test's reports; its workers collect. Without
    # xdist, one process does both.
    if report and not hasattr(config, "workerinput"):
        config.pluginmanager.register(_Recorder(Path(report), shard), "pytest_shards_recorder")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.stash.get(_SHARD_KEY, None)
    if shard is None:
        return
    kinds = {item.nodeid: kind_of(m.name for m in item.iter_markers()) for item in items}
    python = python_version()
    durations = recorded_durations(DURATIONS, python, shard.kind)
    shards = assign(kinds, shard.kind, shard.count, durations)
    keep = [item for item in items if shards.get(item.nodeid) == shard.index]
    drop = [item for item in items if shards.get(item.nodeid) != shard.index]
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
    report = config.getoption("--shard-report")
    # Every worker plans the same; the first writes it down.
    worker = getattr(config, "workerinput", {}).get("workerid")
    if report and worker in (None, "gw0"):
        _write(
            Path(report) / PLAN,
            {**_identity(python, shard), "kinds": kinds, "shards": shards},
        )


class _Recorder:
    """Records each test the shard ran, with the seconds its setup, call and teardown took."""

    def __init__(self, directory: Path, shard: Shard) -> None:
        self.directory = directory
        self.shard = shard
        self.seconds: dict[str, float] = defaultdict(float)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.seconds[report.nodeid] += report.duration

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        _write(
            self.directory / RAN,
            {**_identity(python_version(), self.shard), "seconds": dict(self.seconds)},
        )


def _identity(python: str, shard: Shard) -> dict[str, object]:
    return {"python": python, "kind": shard.kind, "shard": shard.index, "count": shard.count}


def _write(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    temporary.replace(path)

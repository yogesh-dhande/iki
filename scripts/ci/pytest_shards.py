"""Runs one CI shard of the Python tests: ``pytest -p pytest_shards --shard plain:2/4``.

CI splits the suite into kinds by the setup a test needs, and each kind into shards that
run as jobs side by side:

- ``browser``: use the Playwright or browser fixture, or declare browser assets with
  ``built_web_app`` or ``browser``.
- ``worker``: marked ``worker_e2e``. They run iki-worker, which cargo builds.
- ``plain``: everything else, which needs Python, Postgres, Docker and iki-wasm (which
  every shard builds: saves evaluate plugins' rules with it).

Every shard collects the whole suite, then keeps the tests of its kind and its share of
them. A test file is never split across a kind's shards: its module-scoped fixtures (a
server, a browser) start once. Files are dealt out longest first, by their recorded
durations (``test-durations.json``, which CI's Python summary job refreshes), to whichever
shard has least so far. A new file uses the per-test rate and whole-file times of nearby
measured files of the same kind. The same collection gives the same split in every shard,
and in each of pytest-xdist's workers.

With ``--shard-report DIR`` the shard writes what it planned and what it ran there;
``shard_summary.py`` checks across the shards that every test ran in exactly one.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from statistics import median, quantiles
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

    from shard_summary import FileDuration
    from xdist.workermanage import WorkerController  # type: ignore[import-untyped]

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


def kind_of(markers: Iterable[str], fixtures: Iterable[str] = ()) -> str:
    """The setup a test needs, including through an indirect or aliased fixture."""
    names = set(markers)
    if names & {"built_web_app", "browser"} or {"browser", "playwright"}.intersection(fixtures):
        return "browser"
    if "worker_e2e" in names:
        return "worker"
    return "plain"


def item_kind(item: pytest.Item) -> str:
    fixtures = item.fixturenames if isinstance(item, pytest.Function) else ()
    return kind_of((m.name for m in item.iter_markers()), fixtures)


def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _seconds(recording: float | FileDuration) -> float:
    return recording["seconds"] if isinstance(recording, dict) else recording


def file_weights(
    counts: Mapping[str, int], durations: Mapping[str, float | FileDuration]
) -> dict[str, float]:
    """Estimated seconds for the collected files in a single Python version and test kind."""
    weights = {}
    measured_counts = {}
    for f, recording in durations.items():
        if f in counts:
            weights[f] = _seconds(recording)
            # A legacy scalar has no record of the old count: keep its measured seconds.
            measured_counts[f] = recording["tests"] if isinstance(recording, dict) else counts[f]
    growing = {
        f: count - measured_counts.get(f, 0)
        for f, count in counts.items()
        if count > measured_counts.get(f, 0)
    }
    if not growing:
        return weights

    # A recorded zero is rounded to the nearest tenth of a second. Keep its weight,
    # but use measurable files to estimate a new file's time.
    peers: dict[Path, list[tuple[float, float]]] = defaultdict(list)
    for f, seconds in weights.items():
        if seconds > 0 and measured_counts[f] > 0:
            for directory in Path(f).parents:
                peers[directory].append((seconds, seconds / measured_counts[f]))

    estimates: dict[Path, tuple[float, float]] = {}
    for directory, samples in peers.items():
        # Three files give a local comparison. Broaden to the parent for a sparse
        # directory (e.g. a new plugin); at the root take whatever the kind has.
        if len(samples) < 3 and directory != Path():
            continue
        times = [seconds for seconds, _ in samples]
        # A one-case workflow can take as long as a whole file. Budget at least the
        # upper-quartile file time; for larger files the median per-case rate can win.
        floor = quantiles(times, method="inclusive")[2] if len(times) > 1 else times[0]
        estimates[directory] = floor, median(rate for _, rate in samples)

    for f, added in growing.items():
        floor, rate = next((estimates[d] for d in Path(f).parents if d in estimates), (0.0, 1.0))
        if f in weights:
            own_rate = weights[f] / measured_counts[f]
            weights[f] += added * max(own_rate, rate)
        else:
            weights[f] = max(floor, rate * added)
    return weights


def assign(
    kinds: Mapping[str, str], kind: str, count: int, durations: Mapping[str, float | FileDuration]
) -> dict[str, int]:
    """Deals the tests of ``kind`` out to shards 1 to ``count``, a whole file at a time.

    ``kinds`` maps every collected test to its kind; ``durations`` the seconds a file's
    tests of this kind took when they last ran.
    """
    files: dict[str, list[str]] = defaultdict(list)
    for nodeid, test_kind in kinds.items():
        if test_kind == kind:
            files[file_of(nodeid)].append(nodeid)
    weight = file_weights({f: len(ids) for f, ids in files.items()}, durations)
    loads = [0.0] * count
    shards: dict[str, int] = {}
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        least = min(range(count), key=lambda i: (loads[i], i))
        loads[least] += weight[f]
        for nodeid in files[f]:
            shards[nodeid] = least + 1
    return shards


def start_long_singletons_early(
    nodeids: list[str], workers: int, durations: Mapping[str, float]
) -> list[str]:
    """Start indivisible, measured work early without splitting another module's fixtures."""
    files = Counter(file_of(nodeid) for nodeid in nodeids)
    result = nodeids.copy()
    start = 0
    # WorkStealingScheduling initially sends each worker an equal contiguous slice,
    # dividing the remaining items among the remaining workers (remainder goes last).
    for remaining_workers in range(workers, 0, -1):
        end = start + (len(result) - start) // remaining_workers
        slowest = start
        longest = 0.0
        for i in range(start, end):
            file = file_of(result[i])
            if files[file] == 1 and durations.get(file, 0.0) > longest:
                slowest = i
                longest = durations[file]
        if slowest != start:
            result.insert(start, result.pop(slowest))
        start = end
    return result


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: WorkerController) -> None:
    # xdist resets the worker's local --dist option; the controller knows its scheduler.
    if node.config.getoption("dist") == "worksteal":
        node.workerinput["shard_worksteal_workers"] = node.workerinput["workercount"]


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def recorded_durations(path: Path, python: str, kind: str) -> dict[str, float | FileDuration]:
    """Read measured times, including legacy scalar seconds with no original test count."""
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    result: dict[str, float | FileDuration] = {}
    for f, value in recorded.get(python, {}).get(kind, {}).items():
        structured = isinstance(value, dict)
        seconds = float(value["seconds"] if structured else value)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"{path}: {f} needs finite nonnegative seconds")
        if structured:
            tests = value["tests"]
            if type(tests) is not int or tests < 1:
                raise ValueError(f"{path}: {f} needs a positive integer test count")
            result[f] = {"tests": tests, "seconds": seconds}
        else:
            result[f] = seconds
    return result


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
    kinds = {item.nodeid: item_kind(item) for item in items}
    python = python_version()
    durations = recorded_durations(DURATIONS, python, shard.kind)
    shards = assign(kinds, shard.kind, shard.count, durations)
    keep = [item for item in items if shards.get(item.nodeid) == shard.index]
    workers = getattr(config, "workerinput", {}).get("shard_worksteal_workers")
    if shard.kind == "plain" and workers:
        by_id = {item.nodeid: item for item in keep}
        measured_seconds = {f: _seconds(recording) for f, recording in durations.items()}
        keep = [
            by_id[n] for n in start_long_singletons_early(list(by_id), workers, measured_seconds)
        ]
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

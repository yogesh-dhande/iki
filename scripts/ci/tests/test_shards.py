"""CI's Python shards (pytest_shards.py) and their check (shard_summary.py)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_shards
import shard_summary
from pytest_shards import assign, kind_of

CI = Path(__file__).parents[1]

SUITE = {
    "pytest.ini": "[pytest]\nmarkers =\n  built_web_app: x\n  wasm: x\n  worker_e2e: x\n",
    "test_mixed.py": (
        "import pytest\n"
        "@pytest.mark.parametrize('n', range(4))\n"
        "def test_plain(n): pass\n"
        "@pytest.mark.wasm\n"
        "def test_wasm(): pass\n"
    ),
    "test_pages.py": (
        "import pytest\n"
        "pytestmark = pytest.mark.built_web_app\n"
        "def test_one(): pass\n"
        "@pytest.mark.wasm\n"
        "def test_two(): pass\n"
    ),
    "test_worker.py": "import pytest\npytestmark = pytest.mark.worker_e2e\ndef test_runs(): pass\n",
    "test_more.py": "def test_a(): pass\ndef test_b(): pass\n",
    "test_last.py": "import pytest\n@pytest.mark.skip\ndef test_skipped(): pass\n",
}

ALL = {
    "test_mixed.py::test_plain[0]",
    "test_mixed.py::test_plain[1]",
    "test_mixed.py::test_plain[2]",
    "test_mixed.py::test_plain[3]",
    "test_mixed.py::test_wasm",
    "test_pages.py::test_one",
    "test_pages.py::test_two",
    "test_worker.py::test_runs",
    "test_more.py::test_a",
    "test_more.py::test_b",
    "test_last.py::test_skipped",
}


def test_a_test_s_kind_is_the_setup_it_needs() -> None:
    assert kind_of(["django_db"]) == "plain"
    # Every shard builds iki-wasm.
    assert kind_of(["wasm"]) == "plain"
    assert kind_of(["worker_e2e", "django_db"]) == "worker"
    assert kind_of(["browser"]) == "browser"
    assert kind_of(["built_web_app"]) == "browser"
    assert kind_of(["built_web_app", "wasm"]) == "browser"


def test_shards_take_whole_files_longest_first() -> None:
    kinds = {f"{f}::test_{i}": "plain" for f in ("a.py", "b.py", "c.py", "d.py") for i in range(3)}
    kinds["e.py::test_0"] = "browser"
    durations = {"a.py": 100.0, "b.py": 60.0, "c.py": 50.0, "d.py": 10.0}

    shards = assign(kinds, "plain", 2, durations)

    assert set(shards) == {n for n, k in kinds.items() if k == "plain"}
    by_file = {n.split("::")[0]: s for n, s in shards.items()}
    for nodeid, shard in shards.items():
        assert by_file[nodeid.split("::")[0]] == shard
    # a (100) | b, c, d (120): longest first, each to the shard with least so far.
    assert by_file == {"a.py": 1, "b.py": 2, "c.py": 2, "d.py": 1}


def test_a_file_without_a_recording_counts_its_tests_at_the_recorded_average() -> None:
    kinds = {f"new.py::test_{i}": "plain" for i in range(10)}
    kinds |= {"old.py::test_0": "plain", "old.py::test_1": "plain"}
    # 2 tests in 50s: 25s each, so new.py's 10 count 250s and go first.
    shards = assign(kinds, "plain", 2, {"old.py": 50.0, "gone.py": 999.0})
    assert shards["new.py::test_0"] == 1
    assert shards["old.py::test_0"] == 2


def test_without_recordings_shards_balance_test_counts() -> None:
    sizes = {"a.py": 6, "b.py": 3, "c.py": 3}
    kinds = {f"{f}::test_{i}": "plain" for f, n in sizes.items() for i in range(n)}
    shards = assign(kinds, "plain", 2, {})
    assert sum(1 for s in shards.values() if s == 1) == 6
    assert sum(1 for s in shards.values() if s == 2) == 6


def test_the_split_does_not_depend_on_collection_order() -> None:
    kinds = {f"{f}.py::test_{i}": "plain" for f in "abcdefg" for i in range(len(f) + 2)}
    backwards = dict(reversed(list(kinds.items())))
    assert assign(kinds, "plain", 3, {}) == assign(backwards, "plain", 3, {})


def test_a_shard_is_kind_k_of_n() -> None:
    assert pytest_shards.Shard.parse("browser:2/3") == pytest_shards.Shard("browser", 2, 3)
    for bad in ("plain", "plain:0/2", "plain:3/2", "gpu:1/1", "plain:1/2/3"):
        with pytest.raises(pytest.UsageError):
            pytest_shards.Shard.parse(bad)


def _run_shards(tmp_path: Path, shards: list[str]) -> Path:
    """Runs a small suite in each shard, in parallel workers as CI does."""
    for name, text in SUITE.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    reports = tmp_path / "reports"
    # Not this run's plugins and settings: only the shards' and pytest-xdist's.
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(CI),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    for shard in shards:
        done = subprocess.run(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-p",
                "xdist.plugin",
                "-p",
                "pytest_shards",
                "-n",
                "2",
                "--shard",
                shard,
                "--shard-report",
                str(reports / shard.replace(":", "-").replace("/", "-")),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert done.returncode == 0, done.stdout + done.stderr
    return reports


def test_every_test_runs_in_exactly_one_shard(tmp_path: Path) -> None:
    reports = _run_shards(tmp_path, ["plain:1/2", "plain:2/2", "worker:1/1", "browser:1/1"])

    loaded, errors = shard_summary.load(reports)
    assert errors == []
    assert shard_summary.check(loaded) == []
    ran = [n for r in loaded for n in r.seconds]
    assert sorted(ran) == sorted(ALL)
    by_shard = {r.label.split(" ", 2)[2]: {n.split("::")[0] for n in r.seconds} for r in loaded}
    assert by_shard["worker 1/1"] == {"test_worker.py"}
    assert by_shard["browser 1/1"] == {"test_pages.py"}
    # A file's plain tests stay together, in one of the plain shards.
    assert {"test_mixed.py", "test_more.py", "test_last.py"} == (
        by_shard["plain 1/2"] | by_shard["plain 2/2"]
    )
    assert not by_shard["plain 1/2"] & by_shard["plain 2/2"]

    out = tmp_path / "durations.json"
    summary = tmp_path / "summary.md"
    args = [str(reports), "--durations", str(out), "--markdown", str(summary)]
    assert shard_summary.main(args) == 0
    recorded = json.loads(out.read_text(encoding="utf-8"))
    python = pytest_shards.python_version()
    assert set(recorded[python]) == {"plain", "worker", "browser"}
    assert set(recorded[python]["worker"]) == {"test_worker.py"}
    assert f"Python {python} browser 1/1" in summary.read_text(encoding="utf-8")


def test_a_missing_shard_fails_the_check(tmp_path: Path) -> None:
    reports = _run_shards(tmp_path, ["plain:1/2", "worker:1/1", "browser:1/1"])

    loaded, errors = shard_summary.load(reports)
    found = shard_summary.check(loaded)

    assert errors == []
    assert any("expected shards 1 to 2, found [1]" in e for e in found)
    assert any("ran in no plain shard" in e for e in found)
    assert any("ran on no Python version" in e for e in found)


def test_a_kind_no_version_runs_fails_the_check(tmp_path: Path) -> None:
    reports = _run_shards(tmp_path, ["plain:1/1", "worker:1/1"])

    loaded, _ = shard_summary.load(reports)

    assert shard_summary.check(loaded) == [
        "2 tests ran on no Python version: test_pages.py::test_one, test_pages.py::test_two"
    ]


def test_a_shard_run_twice_or_disagreeing_fails_the_check(tmp_path: Path) -> None:
    reports = _run_shards(tmp_path, ["plain:1/1", "worker:1/1", "browser:1/1"])
    loaded, _ = shard_summary.load(reports)
    browser = next(r for r in loaded if r.kind == "browser")
    plain = next(r for r in loaded if r.kind == "plain")
    changed = shard_summary.Report(
        **{**vars(plain), "name": "other", "kinds": {**plain.kinds, "test_new.py::test": "plain"}}
    )

    twice = shard_summary.check([*loaded, browser])
    disagreeing = shard_summary.check([*loaded, changed])

    assert any("expected shards 1 to 1, found [1, 1]" in e for e in twice)
    assert any("2 tests ran in Python" in e and "test_pages.py::test_one" in e for e in twice)
    assert any("collected a different suite" in e for e in disagreeing)


def test_a_shard_without_its_reports_fails_the_check(tmp_path: Path) -> None:
    (tmp_path / "python-3.14-plain-1").mkdir()
    _, errors = shard_summary.load(tmp_path)
    assert errors == ["python-3.14-plain-1: no plan.json"]

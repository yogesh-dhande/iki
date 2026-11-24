"""Gate-only skip checks, exercised through real pytest reports and exit codes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[3] / "pyproject.toml"


def run_suite(tmp_path: Path, *args: str, gate: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"]}
    if gate:
        env[gate] = "1"
    return subprocess.run(  # noqa: S603 - fixed Python and pytest
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-c",
            str(CONFIG),
            "--rootdir",
            str(tmp_path),
            ".",
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("gate", "workers", "selection"),
    [
        ("IKI_GATE", "0", "test_missing_tool or test_passes"),
        ("CI", "2", "not test_missing_tool"),
        (None, "0", ""),
    ],
)
def test_gate_rejects_an_unknown_skip_among_passing_tests(
    tmp_path: Path, gate: str | None, workers: str, selection: str
) -> None:
    # The normal gate runs on both Python versions. The import-semantics check is
    # intentionally 3.12-only. Its exact reason belongs to that test alone.
    suite = tmp_path / "scripts/ci/tests"
    suite.mkdir(parents=True)
    (suite / "test_check_imports.py").write_text(
        "import pytest\n"
        "def test_evaluates_annotations_in_the_selected_tree():\n"
        "    pytest.skip('checks Python 3.12 import semantics')\n"
        "def test_unrelated(): pytest.skip('checks Python 3.12 import semantics')\n"
        "@pytest.fixture\ndef tool(): pytest.skip('new missing tool')\n"
        "def test_missing_tool(tool): pass\n"
        "def test_passes(): pass\n"
    )
    run = run_suite(tmp_path, "-n", workers, "-k", selection, gate=gate)
    output = run.stdout + run.stderr
    assert run.returncode == (1 if gate else 0), output
    if gate == "IKI_GATE":
        # Everything else passed; this one fixture skip must turn the entire run red.
        assert "1 passed" in output and "1 error" in output, output
        assert "Unexpected skip in gate: new missing tool" in output, output
    elif gate == "CI":
        assert "1 failed, 1 passed, 1 skipped" in output, output
        assert "Unexpected skip in gate: checks Python 3.12 import semantics" in output, output
    else:
        assert "1 passed, 3 skipped" in output, output


def test_only_two_engine_cases_can_skip_viewport_confirmation(tmp_path: Path) -> None:
    suite = tmp_path / "server/tests"
    suite.mkdir(parents=True)
    (suite / "test_click_stability.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('mode', ['viewport-confirmation', 'event'])\n"
        "@pytest.mark.parametrize('engine', ['chromium', 'firefox', 'webkit'])\n"
        "def test_viewport_resize_suppresses_a_pre_press_shift(mode, engine):\n"
        "    pytest.skip('Only Chromium records layout shifts around a test resize')\n"
    )
    run = run_suite(tmp_path, gate="IKI_GATE")
    output = run.stdout + run.stderr
    assert run.returncode == 1 and "4 failed, 2 skipped" in output, output


@pytest.mark.parametrize("workers", ["0", "2"])
def test_skipping_a_module_at_collection_fails_the_gate(tmp_path: Path, workers: str) -> None:
    (tmp_path / "test_absent.py").write_text(
        "import pytest\npytest.skip('optional module absent', allow_module_level=True)\n"
    )
    (tmp_path / "test_ok.py").write_text("def test_ok(): pass\n")
    run = run_suite(tmp_path, "-n", workers, "--continue-on-collection-errors", gate="IKI_GATE")
    output = run.stdout + run.stderr
    assert run.returncode == 1, output
    assert "Unexpected skip in gate: optional module absent" in output, output
    assert "test_absent.py" in output, output


def test_direct_pytest_requires_the_shared_wasm(tmp_path: Path) -> None:
    # Exercise the spec suite that used to turn a missing build into a green skip.
    suite = CONFIG.parent / "spec/tests/test_dna_annotation.py"
    (tmp_path / suite.name).write_bytes(suite.read_bytes())
    (tmp_path / "conftest.py").write_text(
        "import threading\nfrom pathlib import Path\nfrom iki_spec import wasm\n"
        "wasm.WASM = Path(__file__).with_name('missing.wasm')\n"
        "wasm._compiled = None\n"
        "wasm._local = threading.local()\n"
    )
    run = run_suite(tmp_path, gate=None)
    output = run.stdout + run.stderr
    assert run.returncode == 1, output
    assert "missing.wasm" in output and "Run scripts/build-wasm.sh" in output, output


def test_configured_worker_binary_must_be_executable(tmp_path: Path) -> None:
    for kind in ("missing", "directory", "nonexecutable", "executable"):
        candidate = tmp_path / kind / "iki-worker"
        candidate.parent.mkdir()
        if kind == "directory":
            candidate.mkdir()
        elif kind != "missing":
            candidate.write_bytes(b"#!/bin/sh\n")
            candidate.chmod(0o755 if kind == "executable" else 0o644)
        (tmp_path / f"test_{kind}.py").write_text(
            "from pathlib import Path\nimport pytest\nfrom test_worker_e2e import worker_bin\n"
            "@pytest.fixture(scope='module', autouse=True)\ndef choose():\n"
            "    with pytest.MonkeyPatch.context() as patch:\n"
            f"        patch.setenv('IKI_WORKER_BIN', {str(candidate)!r})\n"
            "        yield\n"
            "def test_configured(worker_bin):\n"
            f"    assert worker_bin == Path({str(candidate)!r})\n"
        )
    run = run_suite(tmp_path, gate=None)
    output = run.stdout + run.stderr
    assert run.returncode == 1 and "1 passed, 3 errors" in output, output
    for kind in ("missing", "directory", "nonexecutable"):
        assert f"IKI_WORKER_BIN={tmp_path / kind / 'iki-worker'}" in output, output
    assert "cargo build --locked -p iki-worker" in output, output

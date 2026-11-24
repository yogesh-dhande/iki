"""A missing Playwright revision must be visible in the result of running pytest."""

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
# This browser test needs neither a database nor a built app.
CASE = "server/tests/test_plate_map_component.py::test_preview_issues_and_repeated_wells[chromium]"


def test_browser_shards_follow_the_real_cases_and_their_fixtures(tmp_path: Path) -> None:
    # These standalone component suites use the shared browser, including through
    # an alias. Some have no application-build marker.
    click_tests = "server/tests/test_click_stability.py"
    expected = {
        "server/tests/test_qc_release_component.py": "browser",
        "server/tests/test_stability_component.py": "browser",
        "server/tests/test_equipment_components.py": "browser",
        f"{click_tests}::test_stable_press": "browser",
        f"{click_tests}::test_finish_writes_reports_and_only_fails_for_mismatches": "plain",
        "plugins/qc_release/tests/test_release_actions.py": "plain",
    }
    run = subprocess.run(  # noqa: S603 - the repository's collection entry point
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "pytest_shards",
            "--shard",
            "browser:1/1",
            "--shard-report",
            str(tmp_path),
            *expected,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    kinds = json.loads((tmp_path / "plan.json").read_text())["kinds"]
    for selection, kind in expected.items():
        assert {value for test, value in kinds.items() if test.startswith(selection)} == {kind}, (
            selection
        )


@pytest.mark.parametrize(
    ("gate", "opt_out"),
    [
        (None, False),
        ("CI", False),
        ("IKI_GATE", False),
        (None, True),
        ("CI", True),
        ("IKI_GATE", True),
        ("IKI_VM_RUN", True),
    ],
)
def test_browser_coverage_with_an_empty_installation(
    tmp_path: Path, gate: str | None, opt_out: bool
) -> None:
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(tmp_path), PYTEST_ADDOPTS="")
    for key in ("CI", "IKI_GATE", "IKI_VM_RUN", "IKI_SKIP_BROWSER_TESTS"):
        env.pop(key, None)
    if opt_out:
        env["IKI_SKIP_BROWSER_TESTS"] = "1"
    if gate:
        env[gate] = "vm-test-job" if gate == "IKI_VM_RUN" else "1"
    # The assertions read the child summary as text, including when CI forces color.
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-rN",
        "--color=no",
        "-c",
        str(ROOT / "pyproject.toml"),
        CASE,
    ]
    if opt_out:
        # This test launches a disposable browser in a subprocess without a browser fixture.
        args.extend(
            [
                "server/tests/test_browser_artifacts.py::test_failure_evidence_survives_context_close_and_isolated_workers",
                "server/tests/test_click_stability.py::test_stable_press[firefox]",
                "server/tests/test_click_stability.py::test_finish_writes_reports_and_only_fails_for_mismatches",
            ]
        )
        other = tmp_path / "test_other.py"
        other.write_text(
            "import pytest\ndef test_plain(): pass\n"
            "def test_capability(): pytest.skip('unrelated capability')\n"
        )
        args.append(str(other))
    result = subprocess.run(  # noqa: S603 - fixed argv
        args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    output = result.stdout + result.stderr
    if opt_out and gate:
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
        assert "full gate" in output and "partial local" in output, output
        assert "playwright install" not in output
    elif opt_out:
        assert result.returncode == 0 and "4 passed, 4 skipped" in output, output
        assert "3 selected tests skipped by opt-out" in output, output
        assert "IKI_SKIP_BROWSER_TESTS=1" in output and "browser coverage disabled" in output, (
            output
        )
    else:
        assert result.returncode == 1 and "1 error" in output, output
        assert "playwright install" in output, output


@pytest.mark.parametrize("goal", ["check", "check-python"])
@pytest.mark.parametrize("command_line", [False, True], ids=["inherited", "command-line"])
def test_full_make_refuses_a_requested_browser_opt_out(
    tmp_path: Path, goal: str, command_line: bool
) -> None:
    env = os.environ.copy()
    env.pop("IKI_SKIP_BROWSER_TESTS", None)
    args = ["make", "-n", "-k", "-f", str(ROOT / "Makefile"), goal]
    if command_line:
        args.append("IKI_SKIP_BROWSER_TESTS=1")
    else:
        env["IKI_SKIP_BROWSER_TESTS"] = "1"
    run = subprocess.run(  # noqa: S603 - fixed make recipe
        args, cwd=tmp_path, env=env, capture_output=True, text=True, check=False, timeout=10
    )
    assert run.returncode != 0 and "partial local" in run.stderr, run.stderr
    assert "IKI_SKIP_BROWSER_TESTS=1" in run.stderr


def test_make_runs_the_locked_browsers_and_retains_failures(tmp_path: Path) -> None:
    # Run the actual recipe against recording tools, without starting a second full gate.
    check_imports = tmp_path / "scripts" / "check-imports-py312"
    check_imports.parent.mkdir()
    check_imports.write_text("#!/bin/sh\nexit 0\n")
    check_imports.chmod(0o755)
    trace_wrapper = Path("scripts/ci/with-browser-traces.sh")
    (tmp_path / trace_wrapper).parent.mkdir()
    (tmp_path / trace_wrapper).symlink_to(ROOT / trace_wrapper)
    wasm = check_imports.with_name("build-wasm.sh")
    wasm.write_text("#!/bin/sh\nexit 0\n")
    wasm.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "UV %s [%s] gate=%s\\n" "$*" "${IKI_SKIP_BROWSER_TESTS:-}" "$IKI_GATE"\n'
        'if [ "$1 $2" = "run pytest" ] && [ -n "${SIMULATE_BROWSER_FAILURE:-}" ]; then\n'
        '  mkdir -p "$IKI_BROWSER_ARTIFACTS/gw1/test_name"\n'
        '  echo "$SIMULATE_BROWSER_FAILURE" > '
        '"$IKI_BROWSER_ARTIFACTS/gw1/test_name/context-1-trace.zip"\n'
        "  exit 17\n"
        "fi\n"
    )
    uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.defpath}"}
    env.pop("CI", None)
    env.pop("IKI_GATE", None)
    env.pop("IKI_VM_RUN", None)
    env.pop("IKI_SKIP_BROWSER_TESTS", None)
    env.pop("IKI_BROWSER_ARTIFACTS", None)
    args = ["make", "-f", str(ROOT / "Makefile"), "-k", "check-python", "PYTEST_PARALLEL="]
    artifacts = tmp_path / "build/browser-failures"
    bundles: dict[Path, bytes] = {}
    for label in ("first", "second"):
        failed = subprocess.run(  # noqa: S603 - fixed make recipe
            args,
            cwd=tmp_path,
            env={**env, "SIMULATE_BROWSER_FAILURE": label},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert failed.returncode != 0, failed.stdout
        (bundle,) = set(artifacts.glob("run-*/traces.tar.gz")) - bundles.keys()
        bundles[bundle] = bundle.read_bytes()
        with tarfile.open(bundle) as archive:
            (trace,) = [entry for entry in archive if entry.name.endswith(".zip")]
            assert trace.name.startswith("./attempt-")
            assert trace.name.endswith("/gw1/test_name/context-1-trace.zip")
            contents = archive.extractfile(trace)
            assert contents is not None and contents.read() == f"{label}\n".encode()
    run = subprocess.run(  # noqa: S603 - fixed make recipe
        args, cwd=tmp_path, env=env, capture_output=True, text=True, check=True, timeout=10
    )
    recorded = [line for line in run.stdout.splitlines() if line.startswith("UV ")]
    assert any("playwright install chromium firefox webkit" in line for line in recorded), recorded
    assert any("check_test_prerequisites.py wasm" in line for line in recorded)
    assert any("check_test_prerequisites.py web" in line for line in recorded)
    assert "UV run pytest [] gate=1" in recorded, recorded
    assert {path: path.read_bytes() for path in artifacts.glob("run-*/traces.tar.gz")} == bundles

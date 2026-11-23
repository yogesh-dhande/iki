"""A missing Playwright revision must be visible in the result of running pytest."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
# This browser test needs neither a database nor a built app.
CASE = "server/tests/test_plate_map_component.py::test_preview_issues_and_repeated_wells[chromium]"


@pytest.mark.parametrize(
    ("opt_out", "ci"),
    [(False, False), (True, False), (True, True)],
    ids=["required", "explicit-local-opt-out", "ci-requires-browsers"],
)
def test_browser_coverage_with_an_empty_installation(
    tmp_path: Path, opt_out: bool, ci: bool
) -> None:
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(tmp_path), PYTEST_ADDOPTS="")
    env.pop("CI", None)
    env.pop("IKI_SKIP_BROWSER_TESTS", None)
    if opt_out:
        env["IKI_SKIP_BROWSER_TESTS"] = "1"
    if ci:
        env["CI"] = "true"
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "pytest", "-q", "-rs", CASE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    if opt_out and not ci:
        assert result.returncode == 0 and "1 skipped" in output, output
        assert "IKI_SKIP_BROWSER_TESTS" in output, output
    else:
        assert result.returncode == 1 and "1 error" in output, output
        assert "playwright install" in output, output


@pytest.mark.parametrize("command_line", [False, True], ids=["inherited", "command-line"])
def test_make_check_python_requires_browsers(tmp_path: Path, command_line: bool) -> None:
    # Run the actual recipe against recording tools, without starting a second full gate.
    for name in [
        "server/src/iki_server/web/static/app/manifest.json",
        "spec/src/iki_spec/iki_wasm.wasm",
    ]:
        artifact = tmp_path / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"test artifact")
    check_imports = tmp_path / "scripts" / "check-imports-py312"
    check_imports.parent.mkdir()
    check_imports.write_text("#!/bin/sh\nexit 0\n")
    check_imports.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "UV %s [%s]\\n" "$*" "${IKI_SKIP_BROWSER_TESTS:-}"\n')
    uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.defpath}"}
    env.pop("CI", None)
    env.pop("IKI_SKIP_BROWSER_TESTS", None)
    args = ["make", "-f", str(ROOT / "Makefile"), "-k", "check-python", "PYTEST_PARALLEL="]
    if command_line:
        args.append("IKI_SKIP_BROWSER_TESTS=1")
    else:
        env["IKI_SKIP_BROWSER_TESTS"] = "1"
    run = subprocess.run(  # noqa: S603 - fixed make recipe
        args, cwd=tmp_path, env=env, capture_output=True, text=True, check=True, timeout=10
    )
    recorded = [line for line in run.stdout.splitlines() if line.startswith("UV ")]
    assert any("playwright install chromium firefox webkit" in line for line in recorded), recorded
    assert "UV run pytest []" in recorded, recorded

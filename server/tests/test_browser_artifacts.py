"""A real pytest run keeps each failed browser attempt's Playwright trace."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

pytestmark = pytest.mark.built_web_app


def run_probe(tmp_path: Path, *args: str, gate: bool = False) -> subprocess.CompletedProcess[str]:
    tests = Path(__file__).resolve().parent
    (tmp_path / "conftest.py").symlink_to(tests / "conftest.py")
    target = tmp_path / "artifacts"
    env = os.environ.copy()
    for key in ("IKI_GATE", "CI", "IKI_VM_RUN", "IKI_SKIP_BROWSER_TESTS"):
        env.pop(key, None)
    if gate:
        env["IKI_GATE"] = "1"
    env["IKI_BROWSER_ARTIFACTS"] = str(target)
    return subprocess.run(  # noqa: S603 - this repository's tests and interpreter
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(tests.parents[1] / "pyproject.toml"),
            "--rootdir",
            str(tmp_path),
            *args,
            "-k",
            "chromium or test_short_webkit",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def test_failure_evidence_survives_context_close_and_isolated_workers(tmp_path: Path) -> None:
    target = tmp_path / "artifacts"
    (tmp_path / "test_probe.py").write_text(
        """
import pytest
from test_browser import browser, playwright

def visit(page):
    page.route('http://iki.test/', lambda route: route.fulfill(
        content_type='text/html',
        headers={'Content-Security-Policy': "default-src 'self'; style-src 'self'"},
        body='<button>Open fixture</button>'))
    page.goto('http://iki.test/')
    page.get_by_role('button').click()

@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    visit(page)
    yield page
    context.close()

def test_pass(page): pass

def test_skip(page): pytest.skip('explicitly skipped in the inner suite')

@pytest.mark.parametrize('browser', ['webkit'], indirect=True, scope='module')
def test_short_webkit(page, browser):
    page.context.close()
    assert False

@pytest.mark.parametrize('browser', ['webkit'], indirect=True, scope='module')
def test_short_webkit_preserves_style_policy(browser):
    with browser.new_context() as context:
        page = context.new_page()
        errors = []
        page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' else None)
        visit(page)
        assert errors == []
        blocked = 'http://elsewhere.test/blocked.css'
        page.route(blocked, lambda route: route.fulfill(content_type='text/css', body='body {}'))
        with page.expect_event('console', predicate=lambda msg: blocked in msg.text):
            page.evaluate('''href => {
                const link = document.createElement('link');
                link.rel = 'stylesheet';
                link.href = href;
                document.head.append(link);
            }''', blocked)
        assert len(errors) == 1 and blocked in errors[0] and 'style-src' in errors[0]

@pytest.mark.xfail(reason='expected assertion')
def test_xfail(page): assert False

def test_closed(page, browser):
    page.context.close()
    other = browser.new_page()
    visit(other)
    other.close()
    assert False, 'failed after closing the context'

@pytest.fixture
def broken_setup(page):
    raise RuntimeError('failed during setup')

def test_setup(broken_setup): pass

@pytest.fixture
def broken_teardown(page):
    yield
    raise RuntimeError('failed during teardown')

def test_teardown(broken_teardown): pass
"""
    )
    # A different browser fixture can point at a real deployment. Only the
    # disposable suite opts in, even when the other fixture has the same name.
    (tmp_path / "test_other.py").write_text(
        """
import pytest
from test_browser import playwright

@pytest.fixture
def browser(playwright):
    instance = playwright.chromium.launch()
    yield instance
    instance.close()

def test_other_chromium(browser):
    page = browser.new_page()
    page.set_content('<button>Unregistered fixture</button>')
    page.close()
    assert False
"""
    )
    run = run_probe(tmp_path, "-n", "2", "--dist", "each")
    assert run.returncode == 1, run.stdout + run.stderr
    assert "6 failed, 6 passed, 2 skipped, 2 xfailed, 4 errors" in run.stdout, (
        run.stdout + run.stderr
    )
    assert "Browser evidence (path in archive)" in run.stdout
    paths = sorted(target.rglob("*.zip"))
    assert len(paths) == 10, paths
    for worker in ("gw0", "gw1"):
        failures = sorted(target.joinpath(worker).glob("*/*.zip"))
        assert len(failures) == 5
        for case, count in (("closed", 2), ("setup", 1), ("teardown", 1), ("short_webkit", 1)):
            assert sum(f"test_{case}" in path.parent.name for path in failures) == count
        for trace in failures:
            with ZipFile(trace) as archive:
                actions = [json.loads(line) for line in archive.read("trace.trace").splitlines()]
                assert any(event["type"] == "frame-snapshot" for event in actions)
                assert any(event.get("method") == "click" for event in actions)
                # WebKit can close before its first native screencast frame.
                if "short_webkit" not in trace.parent.name:
                    assert any(event["type"] == "screencast-frame" for event in actions)
                network = [json.loads(line) for line in archive.read("trace.network").splitlines()]
                assert any(
                    event["snapshot"]["request"]["url"] == "http://iki.test/" for event in network
                )
                assert not any(name.startswith("resources/src@") for name in archive.namelist())
    shutil.rmtree(target)


@pytest.mark.parametrize("gate", [False, True])
def test_aliased_disposable_browser_retains_the_final_outcome(tmp_path: Path, gate: bool) -> None:
    (tmp_path / "test_detector.py").write_text(
        """
import pytest
from test_browser import browser as detector_browser, playwright

@pytest.fixture
def page(detector_browser):
    with detector_browser.new_context() as context:
        page = context.new_page()
        page.set_content('<button>Detector fixture</button>')
        yield page

def test_assertion(page):
    assert page.title() == 'missing'

def test_skip(page):
    pytest.skip('unexpected browser skip')

@pytest.mark.xfail(reason='expected detector failure')
def test_xfail(page):
    assert False
"""
    )
    run = run_probe(tmp_path, gate=gate)
    output = run.stdout + run.stderr
    assert run.returncode == 1, output
    expected = {"test_assertion"}
    if gate:
        assert "2 failed" in output, output
        assert "Unexpected skip in gate: unexpected browser skip" in output, output
        expected.add("test_skip")
    else:
        assert "1 skipped" in output, output
    assert "1 xfailed" in output, output
    traces = list((tmp_path / "artifacts").rglob("*.zip"))
    assert len(traces) == len(expected), traces
    for case in expected:
        [trace] = [path for path in traces if case in path.parent.name]
        with ZipFile(trace) as archive:
            assert "trace.trace" in archive.namelist()


def test_the_original_failed_attempt_survives_a_successful_retry(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    # Exercise pytest's same-Item retry protocol. The project's locked suite
    # does not require a retry plugin. Only the final attempt is reported.
    (suite / "conftest.py").write_text(
        """
import hashlib
import json
import os
from pathlib import Path

import pytest
from _pytest.runner import runtestprotocol

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    history = []
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    for attempt in range(3):
        item.attempt = attempt
        reports = runtestprotocol(item, log=False, nextitem=nextitem)
        files = Path(os.environ["IKI_BROWSER_ARTIFACTS"]).rglob("*.zip")
        history.append({str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
        if all(report.passed for report in reports):
            break
    Path(__file__).with_name("history.json").write_text(json.dumps(history))
    for report in reports:
        item.ihook.pytest_runtest_logreport(report=report)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True
"""
    )
    (suite / "test_retry.py").write_text(
        """
from test_browser import browser, playwright

def test_retry(browser, request):
    page = browser.new_page()
    page.set_content("<button>Retry fixture</button>")
    page.get_by_role("button").click()
    page.close()
    assert request.node.attempt == 2
"""
    )
    run = run_probe(tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "1 passed" in run.stdout
    first, second, passing = json.loads((suite / "history.json").read_text())
    assert len(first) == 1
    assert "attempt-1-" in next(iter(first))
    assert len(second) == 2
    assert "attempt-2-" in next(iter(second.keys() - first.keys()))
    assert first.items() <= second.items()
    assert passing == second
    shutil.rmtree(tmp_path / "artifacts")

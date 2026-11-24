"""A gate fails on every pytest skip except these intentional coverage boundaries."""

import os
from collections.abc import Generator

import pytest

_LOCAL_OPT_OUT = (
    "Partial local run: browser coverage disabled by IKI_SKIP_BROWSER_TESTS=1 "
    "(Playwright/browser fixtures and tests marked browser or built_web_app)."
)

# These separately invoked suites attach an exact opt-in reason to marked tests.
_SUITE_BY_REASON = {
    "a load test: run it with --scale (make check-scale)": "scale",
    "needs the Slurm cluster in a container: run it with --slurm (make check-slurm)": "slurm_e2e",
    "the live AWS test: run it with --aws-live": "aws_live",
    "the live AWS test never runs on CI: it bills a real account": "aws_live",
    "the live Google Cloud test: run it with --gcp-live": "gcp_live",
    "the live Google Cloud test never runs on CI: it bills a real account": "gcp_live",
}

_RESIZE_TEST = (
    "server/tests/test_click_stability.py::test_viewport_resize_suppresses_a_pre_press_shift"
)
_RESIZE_REASON = "Only Chromium records layout shifts around a test resize"

# Reasons belong to a test or, when the capability varies, a specific case.
_REASONS_BY_TEST = {
    f"{_RESIZE_TEST}[firefox-viewport-confirmation]": {_RESIZE_REASON},
    f"{_RESIZE_TEST}[webkit-viewport-confirmation]": {_RESIZE_REASON},
    "server/tests/test_api_idor.py::test_another_projects_objects_are_never_reached": {
        "names nothing of a project's"
    },
    "server/tests/test_token_reach.py::test_every_operation_beyond_its_projects_is_refused": {
        "within the token's projects, or the person's own",
        "a lookup",
    },
    "scripts/ci/tests/test_check_imports.py::test_evaluates_annotations_in_the_selected_tree": {
        "checks Python 3.12 import semantics"
    },
    "spec/tests/test_actions.py::test_files_cant_be_symbolic_links": {"needs symbolic links"},
    "server/tests/test_run_results.py::test_workers_reach_the_store_at_their_own_address": {
        "the test store has no second name to reach it by"
    },
}


def _reason(report: pytest.TestReport | pytest.CollectReport) -> str:
    # Pytest represents a skip as (filename, line number, exception message).
    where = report.longrepr
    return where[2].removeprefix("Skipped: ") if isinstance(where, tuple) else str(where)


def _reject(report: pytest.TestReport | pytest.CollectReport, reason: str) -> None:
    report.outcome = "failed"
    report.longrepr = f"{report.nodeid}: Unexpected skip in gate: {reason}"


class _GateSkips:
    # Change the report at its source, before xdist and the CI shard reporter see it.
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        report = yield
        if report.skipped and not hasattr(report, "wasxfail"):
            reason = _reason(report)
            test = item.nodeid.partition("[")[0]
            marker = _SUITE_BY_REASON.get(reason)
            allowed = _REASONS_BY_TEST.get(item.nodeid, _REASONS_BY_TEST.get(test, set()))
            if reason not in allowed and not (marker and item.get_closest_marker(marker)):
                _reject(report, reason)
        return report

    @pytest.hookimpl(wrapper=True)
    def pytest_make_collect_report(
        self,
    ) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
        report = yield
        if report.skipped:
            _reject(report, _reason(report))
        return report


class _LocalBrowserOptOut:
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        # Load the shard plugin after pytest has registered any requested plugins.
        from pytest_shards import item_kind

        skip = pytest.mark.skip(reason=_LOCAL_OPT_OUT)
        for item in items:
            if item_kind(item) == "browser":
                item.add_marker(skip)

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        count = sum(
            _reason(report) == _LOCAL_OPT_OUT
            for report in terminalreporter.stats.get("skipped", [])
            if isinstance(report, pytest.TestReport)
        )
        terminalreporter.write_line(f"{_LOCAL_OPT_OUT} {count} selected tests skipped by opt-out.")


def pytest_configure(config: pytest.Config) -> None:
    gate = os.environ.get("IKI_GATE") == "1" or os.environ.get("CI")
    if os.environ.get("IKI_SKIP_BROWSER_TESTS") == "1":
        if gate:
            raise pytest.UsageError(
                "IKI_SKIP_BROWSER_TESTS=1 is refused by the full gate. "
                "For a partial local run use IKI_SKIP_BROWSER_TESTS=1 uv run pytest outside CI."
            )
        config.pluginmanager.register(_LocalBrowserOptOut())
    if gate:
        config.pluginmanager.register(_GateSkips())

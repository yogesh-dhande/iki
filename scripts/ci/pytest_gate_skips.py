"""A gate fails on every pytest skip except these intentional coverage boundaries."""

import os
from collections.abc import Generator

import pytest

# These separately invoked suites attach an exact opt-in reason to marked tests.
_SUITE_BY_REASON = {
    "a load test: run it with --scale (make check-scale)": "scale",
    "needs the Slurm cluster in a container: run it with --slurm (make check-slurm)": "slurm_e2e",
    "the live AWS test: run it with --aws-live": "aws_live",
    "the live AWS test never runs on CI: it bills a real account": "aws_live",
    "the live Google Cloud test: run it with --gcp-live": "gcp_live",
    "the live Google Cloud test never runs on CI: it bills a real account": "gcp_live",
}

# Each other reason applies to a particular test, including its parametrized cases.
_REASONS_BY_TEST = {
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
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        report = yield
        if report.skipped and not hasattr(report, "wasxfail"):
            reason = _reason(report)
            test = item.nodeid.partition("[")[0]
            marker = _SUITE_BY_REASON.get(reason)
            if reason not in _REASONS_BY_TEST.get(test, set()) and not (
                marker and item.get_closest_marker(marker)
            ):
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


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("IKI_GATE") == "1" or os.environ.get("CI"):
        config.pluginmanager.register(_GateSkips())

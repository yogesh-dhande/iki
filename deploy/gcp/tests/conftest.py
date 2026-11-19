"""The live Google Cloud test (test_gcp_live.py) runs only when asked for, and never on CI.

It stands a real deploy/gcp deployment up in a Google Cloud project, checks it and tears
it down: an hour or more, billed to that project. So it needs all three of ``--gcp-live``,
``IKI_GCP_PROJECT``, and no ``CI`` variable (every CI service sets it).
"""

from collections.abc import Generator

import iki_live
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--gcp-live",
        action="store_true",
        help="Run the live Google Cloud test (marked gcp_live) in IKI_GCP_PROJECT: it makes, "
        "checks and deletes a real deployment, billed to that project (make check-gcp-live).",
    )


def gcp_live_skip_reason(config: pytest.Config) -> str | None:
    """Why the live test doesn't run here, or None if it does."""
    return iki_live.skip_reason(
        config,
        cloud="Google Cloud",
        flag="--gcp-live",
        required={"IKI_GCP_PROJECT": "the project to deploy in"},
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    iki_live.skip_unless_asked(config, items, "gcp_live", gcp_live_skip_reason(config))


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Once the run reaches its cap (IKI_GCP_CAP_HOURS), only the check that tears down runs."""
    if "gcp_live" in item.keywords:
        iki_live.fail_if_capped(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Each live test's outcome, for the record of a passing run (iki_live.lifecycle)."""
    report = yield
    if "gcp_live" in item.keywords:
        iki_live.record_outcome(item, report)
    return report

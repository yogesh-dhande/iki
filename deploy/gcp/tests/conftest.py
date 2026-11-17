"""The live Google Cloud test (test_gcp_live.py) runs only when asked for, and never on CI.

It stands a real deploy/gcp deployment up in a Google Cloud project, checks it and tears
it down: an hour or more, billed to that project. So it needs all three of ``--gcp-live``,
``IKI_GCP_PROJECT``, and no ``CI`` variable (every CI service sets it).
"""

import os

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
    if os.environ.get("CI"):
        return "the live Google Cloud test never runs on CI: it bills a real project"
    if not config.getoption("--gcp-live"):
        return "the live Google Cloud test: run it with --gcp-live (make check-gcp-live)"
    if not os.environ.get("IKI_GCP_PROJECT"):
        return "the live Google Cloud test needs IKI_GCP_PROJECT, the project to deploy in"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    reason = gcp_live_skip_reason(config)
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "gcp_live" in item.keywords:
            item.add_marker(skip)

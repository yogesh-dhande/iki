"""The live AWS test (test_aws_live.py) runs only when asked for, and never on CI.

It stands a real deploy/aws deployment up in an AWS account, checks it and tears it down:
an hour or two, billed to that account. So it needs ``--aws-live``, IKI_AWS_ROLE_ARN and
IKI_AWS_TOKEN_COMMAND, and no ``CI`` variable (every CI service sets it).
"""

from collections.abc import Generator

import iki_live
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--aws-live",
        action="store_true",
        help="Run the live AWS test (marked aws_live) as IKI_AWS_ROLE_ARN: it makes, checks "
        "and deletes a real deployment, billed to that role's account (make check-aws-live).",
    )


def aws_live_skip_reason(config: pytest.Config) -> str | None:
    """Why the live test doesn't run here, or None if it does."""
    return iki_live.skip_reason(
        config,
        cloud="AWS",
        flag="--aws-live",
        required={
            "IKI_AWS_ROLE_ARN": "the role to deploy as",
            "IKI_AWS_TOKEN_COMMAND": "a command printing a web identity token the role trusts",
        },
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    iki_live.skip_unless_asked(config, items, "aws_live", aws_live_skip_reason(config))


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("aws_live"):
        iki_live.fail_if_capped(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if item.get_closest_marker("aws_live"):
        iki_live.record_outcome(item, report)
    return report

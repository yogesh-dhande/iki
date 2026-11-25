"""The seed verifies this run's scientific results before using any fixture plugin."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from deploy.dev import sso


@pytest.mark.parametrize("fixtures", [False, True])
def test_check_runs_product_then_selected_fixtures(
    monkeypatch: pytest.MonkeyPatch, fixtures: bool
) -> None:
    seed = Mock()
    project = {"id": "fresh-project"}
    seed.check.return_value = project
    seed.kubectl.return_value = ""
    fixture_checks = ["lims", "foldcloud", "containers"]
    steps = [
        "route",
        "set_up",
        "check",
        "provisioning",
        "rest_api",
        "webhooks",
        "certify_dev_signatures",
        "sign_in_as",
        "product_scenarios",
        "kubectl",
        *fixture_checks,
        "oauth",
        "saml",
    ]
    for name in steps:
        monkeypatch.setattr(sso, name, getattr(seed, name))
    monkeypatch.setattr(sys, "argv", ["sso.py", "--check", *(["--fixtures"] if fixtures else [])])
    sso.main()

    alice = seed.sign_in_as.return_value
    seed.product_scenarios.assert_called_once_with(alice, project)
    seed.kubectl.assert_called_once()
    command = seed.kubectl.call_args
    assert f"IKI_SEED_PROJECT={project['id']}" in command.args
    assert command.kwargs["stdin"] == Path(sso.__file__).with_name("check_seed.py").read_text()
    scenario_calls = [
        c[0] for c in seed.mock_calls if c[0] in {"product_scenarios", "kubectl", *fixture_checks}
    ]
    assert scenario_calls == ["product_scenarios", "kubectl", *(fixture_checks if fixtures else [])]
    for name in fixture_checks:
        if fixtures:
            getattr(seed, name).assert_called_once_with(alice, project)

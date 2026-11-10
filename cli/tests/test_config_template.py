"""The example config repository (deploy/config-template) stays valid as Iki changes."""

import re
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

from iki_spec.lockfile import load_lock

ROOT = Path(__file__).parents[2]
TEMPLATE = ROOT / "deploy" / "config-template"
DEPLOYMENTS = sorted(p.name for p in (TEMPLATE / "deployments").iterdir())


def test_has_staging_and_production() -> None:
    assert DEPLOYMENTS == ["production", "staging"]


@pytest.mark.parametrize("deployment", DEPLOYMENTS)
def test_lockfiles_are_valid(deployment: str) -> None:
    result = load_lock(TEMPLATE / "deployments" / deployment / "plugins.lock.yaml")
    assert result.problems == []
    assert result.lock is not None
    assert not result.lock.has_local


@pytest.mark.parametrize("deployment", DEPLOYMENTS)
def test_names_this_iki_release(deployment: str) -> None:
    iki = yaml.safe_load((TEMPLATE / "deployments" / deployment / "iki.yaml").read_text())
    release = version("iki-cli").split(".dev")[0]
    assert iki["version"] == release
    assert iki["chart"] == release
    assert re.fullmatch(
        rf"ghcr\.io/ikisci/iki-server:{re.escape(release)}@sha256:[0-9a-f]{{64}}",
        iki["serverImage"],
    )


def test_uses_ikis_own_dockerfile() -> None:
    assert (TEMPLATE / "plugins.Dockerfile").read_text() == (
        ROOT / "deploy" / "plugins.Dockerfile"
    ).read_text()


def test_actions_are_pinned_by_commit() -> None:
    workflow = (TEMPLATE / ".github" / "workflows" / "build.yml").read_text()
    uses = re.findall(r"uses: (\S+)", workflow)
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), action
    parsed = yaml.safe_load(workflow)
    assert parsed["permissions"] == {}

"""Product dev and the fixture build use the same product plugins and compute bindings."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from deploy.dev.fixtures.config import DEV, compose

from iki_spec import lockfile, naming

FIXTURE_PLUGINS = {
    "acme/lims_connector",
    "acme/hosted_models",
    "fixtures/worker_execution",
    "acme/liquid_handling",
}


def test_fixture_lock_only_adds_its_plugins_and_compute(tmp_path: Path) -> None:
    compose(tmp_path)
    product = lockfile.load_lock(DEV / lockfile.FILENAME)
    combined = lockfile.load_lock(tmp_path / lockfile.FILENAME, test_only=True)
    assert product.lock is not None, product.problems
    assert combined.lock is not None, combined.problems

    base = {p.name for p in product.lock.plugins}
    assert base == naming.FIRST_PARTY - {naming.CORE} - FIXTURE_PLUGINS
    assert {p.name for p in combined.lock.plugins} == base | FIXTURE_PLUGINS
    for plugin in combined.lock.plugins:
        assert plugin.local is not None
        actual = yaml.safe_load((tmp_path / plugin.local / "plugin.yaml").read_text())
        assert actual["name"] == plugin.name

    product_compute = list(yaml.safe_load_all((DEV / "compute.yaml").read_text()))
    combined_compute = list(yaml.safe_load_all((tmp_path / "compute.yaml").read_text()))
    assert combined_compute[: len(product_compute)] == product_compute
    for docs, expected in ((product_compute, base), (combined_compute, base | FIXTURE_PLUGINS)):
        for doc in docs:
            if doc["kind"] in {"EnvironmentBinding", "ApiPrice"}:
                qualified_name = doc.get("environment") or doc["api"]
                assert qualified_name.rsplit(".", 1)[0] in expected


@pytest.mark.parametrize("image", ["server", "worker"])
@pytest.mark.parametrize("fixtures", [False, True])
def test_build_selects_test_only_for_fixtures(tmp_path: Path, image: str, fixtures: bool) -> None:
    # Observe the deploy CLI call without building any images.
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    if fixtures:
        env["IKI_DEV_FIXTURES"] = "1"
    else:
        env.pop("IKI_DEV_FIXTURES", None)
    result = subprocess.run(  # noqa: S603 - repository script with fixed arguments
        [str(DEV / "build-plugins.sh"), image, "--output", "out"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    args = result.stdout.splitlines()
    assert ("--test-only" in args) == fixtures
    assert args[args.index("build") + 1] == (
        f"build/dev/{image}-config" if fixtures else "deploy/dev"
    )
    assert "--allow-local" in args

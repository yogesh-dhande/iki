"""Plugin code from the server, for workers on people's computers (ADR 0059): the bundles
``iki deploy build`` wrote, listed with their SHA-256 and how to build each environment."""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_client import problem
from org_world import World, build_world
from test_workers import ROOMY, WorkerClient, queued, registration

from iki_cli import workers as bundles
from iki_server.entities import registry
from iki_server.runs import catalog
from iki_server.runs.models import ActionRun
from iki_spec.validate import load_plugin

pytestmark = pytest.mark.django_db

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"
SHA = "c" * 40


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A deployment's build: protein at SHA, with its bundle, as `iki deploy build` writes."""
    directory = PLUGINS / "protein"
    loaded = load_plugin(directory, dependencies=[PLUGINS / "dna"])
    assert loaded.plugin is not None
    out = tmp_path / "bundles"
    out.mkdir()
    entry = {
        "name": "protein",
        "version": loaded.plugin.version,
        "sha": SHA,
        "state": "active",
        "bundle": bundles.bundle(directory, out / f"protein-{SHA}.tar.gz"),
        "environments": bundles.environments(loaded.plugin, directory),
    }
    # dna comes from a local directory here: nothing to fetch.
    record = {"plugins": [entry, {"name": "dna", "version": "1.0.0", "local": "x"}]}
    monkeypatch.setattr(registry, "installed", lambda: record)
    monkeypatch.setattr(registry, "bundles", lambda: out)
    return entry


def pod(world: World, plugins: list[dict[str, Any]] | None = None) -> WorkerClient:
    made = WorkerClient(registration(world)).post(
        "/register",
        {"name": "laptop", "version": "0.1.0", "capacity": ROOMY, "plugins": plugins or []},
    )
    assert made.status_code == 201, made.content
    return WorkerClient(made.json()["token"])


def test_workers_see_which_plugins_to_fetch(world: World, installed: dict[str, Any]) -> None:
    listed = pod(world).call("GET", "/plugins")
    assert listed.status_code == 200, listed.content
    plugins = listed.json()["plugins"]
    assert [p["name"] for p in plugins] == ["protein"]
    assert plugins[0]["sha"] == SHA
    assert plugins[0]["bundle"] == installed["bundle"]
    tools = plugins[0]["environments"]["protein-tools"]
    assert tools["kind"] == "python" and tools["requirements"] == "requirements.txt"
    assert tools["digest"] == installed["environments"]["protein-tools"]["digest"]


def test_workers_download_a_plugins_code(
    world: World, installed: dict[str, Any], tmp_path: Path
) -> None:
    got = pod(world).call("GET", f"/plugins/protein/{SHA}")
    assert got.status_code == 200
    assert got["Content-Type"] == "application/gzip"
    body = b"".join(got.streaming_content)
    assert hashlib.sha256(body).hexdigest() == installed["bundle"]["sha256"]
    assert len(body) == installed["bundle"]["size"]


@pytest.mark.parametrize(
    "path",
    [f"/plugins/protein/{'d' * 40}", f"/plugins/dna/{SHA}", "/plugins/protein/..%2f..%2fetc"],
)
def test_only_what_the_build_installs_is_downloaded(
    world: World, installed: dict[str, Any], path: str
) -> None:
    assert pod(world).call("GET", path).status_code == 404


def test_only_workers_see_or_download_plugin_code(world: World, installed: dict[str, Any]) -> None:
    for path in ("/plugins", f"/plugins/protein/{SHA}"):
        problem(WorkerClient("iki_wkr_nope").call("GET", path), 401, "not_authenticated")


def test_a_worker_that_fetched_the_code_runs_what_was_launched_with_it(
    world: World, installed: dict[str, Any]
) -> None:
    run = queued(world)
    assert ActionRun.objects.get(pk=run["id"]).plugin_sha == SHA
    digest = installed["environments"]["protein-tools"]["digest"]
    fetched = pod(
        world, [{"name": "protein", "sha": SHA, "environments": {"protein-tools": digest}}]
    )
    leased = fetched.lease()
    assert leased.status_code == 200, leased.content
    assert leased.json()["run"]["plugin"]["sha"] == SHA


def test_a_build_without_bundles_lists_nothing(world: World) -> None:
    listed = pod(world).call("GET", "/plugins")
    assert listed.status_code == 200 and listed.json() == {"plugins": []}

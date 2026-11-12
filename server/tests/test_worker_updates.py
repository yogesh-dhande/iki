"""Which iki-worker release the server wants (ADR 0060): workers on people's computers are
told to update themselves; any worker more than one minor release behind gets no jobs."""

from collections.abc import Iterator
from typing import Any

import pytest
from api_client import problem
from org_world import World, build_world
from test_worker_sign_in import register, sign_in
from test_workers import LOCAL, ROOMY, WorkerClient, registration

from iki_server.runs import catalog
from iki_server.runs.models import Worker

pytestmark = pytest.mark.django_db

MIRROR = "https://artifacts.example.com/iki"


@pytest.fixture
def world(settings: Any) -> Iterator[World]:
    settings.IKI_WORKER_VERSION = "0.5.0"
    settings.IKI_WORKER_UPDATES = True
    settings.IKI_WORKER_DOWNLOADS = MIRROR
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def laptop(world: World, version: str) -> WorkerClient:
    made = WorkerClient(sign_in(world)).post(
        "/register",
        {"name": "laptop", "version": version, "capacity": ROOMY, "plugins": LOCAL},
    )
    assert made.status_code == 201, made.content
    return WorkerClient(made.json()["token"])


def pod(world: World, version: str) -> WorkerClient:
    made = WorkerClient(registration(world)).post(
        "/register",
        {"name": "pod", "version": version, "capacity": ROOMY, "plugins": LOCAL},
    )
    assert made.status_code == 201, made.content
    return WorkerClient(made.json()["token"])


def told(worker: WorkerClient) -> Any:
    me = worker.call("GET", "/me")
    assert me.status_code == 200, me.content
    return me.json()["update"]


def test_a_worker_on_someones_computer_is_told_which_release_to_run(world: World) -> None:
    base = f"{MIRROR}/v0.5.0/iki-worker-0.5.0-{{target}}.tar.gz"
    assert told(laptop(world, "0.4.2")) == {
        "version": "0.5.0",
        "url": base,
        "bundle_url": f"{base}.sigstore.json",
    }
    # Already there, however Cargo writes it.
    assert told(laptop(world, "0.5.0")) is None


def test_workers_in_images_come_with_their_deployment(world: World) -> None:
    assert told(pod(world, "0.4.2")) is None


def test_updates_can_be_turned_off_and_need_a_release(world: World, settings: Any) -> None:
    worker = laptop(world, "0.4.2")
    settings.IKI_WORKER_UPDATES = False
    assert told(worker) is None
    settings.IKI_WORKER_UPDATES = True
    settings.IKI_WORKER_VERSION = "0.6.0.dev0"
    assert told(worker) is None


@pytest.mark.parametrize(
    ("version", "leases"),
    [("0.5.3", True), ("0.4.0", True), ("1.0.0", True), ("0.3.9", False), ("0.3.0-dev.1", False)],
)
def test_workers_more_than_one_minor_release_behind_get_no_jobs(
    world: World, version: str, leases: bool
) -> None:
    worker = laptop(world, version)
    got = worker.lease()
    if leases:
        assert got.status_code == 204, got.content
        return
    body = problem(got, 409, "worker_outdated")
    assert "takes 0.5.0 or the minor release before it" in body["detail"]
    assert body["update"]["version"] == "0.5.0"


def test_an_outdated_pod_is_refused_with_nothing_to_update_to(world: World) -> None:
    body = problem(pod(world, "0.2.0").lease(), 409, "worker_outdated")
    assert body["update"] is None


def test_what_it_runs_is_recorded_as_it_reports_it(world: World) -> None:
    worker = laptop(world, "0.4.2")
    assert register(sign_in(world), name="other").status_code == 201
    made = worker.call("PUT", "/me", {"version": "0.5.0", "capacity": ROOMY, "plugins": LOCAL})
    assert made.status_code == 200, made.content
    assert made.json()["update"] is None
    assert Worker.objects.get(name="laptop").version == "0.5.0"

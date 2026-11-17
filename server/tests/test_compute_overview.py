"""Compute → Overview: the queues, workers and machines, and the same numbers as metrics."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from api_client import problem
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_workers import queued, worker

from iki_server.runs import catalog, overview, queue
from iki_server.runs.models import Job, JobState, Launch, Worker

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def _launch(name: str, state: str, template: str = "gpu-pods") -> None:
    now = timezone.now()
    Launch.objects.create(
        template=template,
        provisioner="k8s_job",
        environment="protein.protein-tools",
        plugin_sha="0" * 40,
        image="ghcr.io/acme/tools@sha256:" + "a" * 64,
        namespace="iki-runs",
        name=name,
        token_hash=name,
        state=state,
        expires_at=now + timedelta(hours=1),
        ended_at=now if state == "ended" else None,
    )


def test_compute_admins_see_whats_waiting_running_and_on_what(world: World) -> None:
    admin = api_as(world, "compute_admin")
    pod = worker(world)
    worker(world, name="pod-2")
    first = queued(world)
    queued(world)
    leased = pod.lease()
    assert leased.status_code == 200, leased.content
    # The second has waited ten minutes.
    Job.objects.filter(state=JobState.QUEUED).update(
        queued_at=timezone.now() - timedelta(minutes=10)
    )
    _launch("run-a", "starting")
    _launch("run-b", "running")
    _launch("run-c", "ended")

    body = admin.get("/compute/overview").json()
    (line,) = body["queues"]
    assert line["tier"] == "backend"
    assert line["environment"] == "protein.protein-tools"
    assert (line["awaiting_approval"], line["queued"], line["running"]) == (0, 1, 1)
    assert 600 <= line["waiting_seconds"] < 660
    backend = next(w for w in body["workers"] if w["tier"] == "backend")
    assert (backend["busy"], backend["idle"], backend["offline"]) == (1, 1, 0)
    # Every tier is listed, so an empty one shows as empty.
    assert [w["tier"] for w in body["workers"]] == ["self_hosted", "backend", "container", "vm"]
    assert body["machines"] == [
        {"template": "gpu-pods", "provisioner": "k8s_job", "starting": 1, "running": 1}
    ]

    # A worker not heard from is offline, and a revoked one isn't counted at all.
    Worker.objects.filter(name="pod-2").update(last_seen_at=timezone.now() - timedelta(hours=1))
    backend = next(
        w for w in admin.get("/compute/overview").json()["workers"] if w["tier"] == "backend"
    )
    assert (backend["busy"], backend["idle"], backend["offline"]) == (1, 0, 1)
    Worker.objects.filter(name="pod-2").update(revoked_at=timezone.now())
    backend = next(
        w for w in admin.get("/compute/overview").json()["workers"] if w["tier"] == "backend"
    )
    assert backend["offline"] == 0
    assert first["status"] == "queued"


def test_a_retry_waiting_out_its_backoff_hasnt_waited(world: World) -> None:
    queued(world)
    Job.objects.update(queued_at=timezone.now() + timedelta(minutes=1))
    (line,) = api_as(world, "compute_admin").get("/compute/overview").json()["queues"]
    assert line["queued"] == 1
    assert line["waiting_seconds"] == 0


def test_only_compute_admins_see_it(world: World) -> None:
    problem(api_as(world, "org_admin").get("/compute/overview"), 403, "forbidden")
    problem(api_as(world, "editor").get("/compute/overview"), 403, "forbidden")


def _observed(read: Any) -> list[tuple[float, dict[str, Any]]]:
    return [(o.value, dict(o.attributes or {})) for o in read(None)]


def test_metrics_say_what_the_page_says(world: World) -> None:
    pod = worker(world)
    queued(world)
    queued(world)
    pod.lease()
    _launch("run-a", "starting")

    attempts = _observed(overview._queued)
    labels = {"tier": "backend", "environment": "protein.protein-tools"}
    assert (1, {**labels, "state": "queued"}) in attempts
    assert (1, {**labels, "state": "running"}) in attempts
    assert (1, {"tier": "backend", "state": "busy"}) in _observed(overview._workers)
    assert (0, {"tier": "vm", "state": "idle"}) in _observed(overview._workers)
    assert (
        1,
        {"template": "gpu-pods", "provisioner": "k8s_job", "state": "starting"},
    ) in _observed(overview._machines)
    ((seconds, where),) = _observed(overview._oldest)
    assert where == labels
    assert seconds >= 0


def test_a_metric_that_cant_be_read_reports_nothing(world: World) -> None:
    def broken(options: Any) -> Any:
        raise RuntimeError("the database went away")

    assert overview._safe(broken)(None) == []  # type: ignore[arg-type]


def test_each_lease_records_how_long_it_waited(
    world: World, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    recorded: list[tuple[float, dict[str, str]]] = []

    class Histogram:
        def record(self, value: float, attributes: dict[str, str]) -> None:
            recorded.append((value, attributes))

    monkeypatch.setattr(queue, "queue_wait", Histogram())
    pod = worker(world)
    queued(world)
    Job.objects.update(queued_at=timezone.now() - timedelta(seconds=90))
    with django_capture_on_commit_callbacks(execute=True):
        assert pod.lease().status_code == 200
    ((seconds, labels),) = recorded
    assert 90 <= seconds < 150
    assert labels == {"tier": "backend"}

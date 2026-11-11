"""Following a run: its attempts, their logs, and live updates about it
(``GET /api/v1/runs/{ref}/events``)."""

import asyncio
from typing import Any

import pytest
from api_client import problem
from asgiref.sync import sync_to_async
from django.test import AsyncClient
from org_world import World, build_world
from test_api_entities import api_as
from test_realtime import Events, signed_in
from test_workers import keep_connection, queued, worker, world

from iki_server.api import runs as runs_api
from iki_server.realtime.hub import hub
from iki_server.runs import queue
from iki_server.runs.models import Worker

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers


@pytest.mark.django_db
def test_a_runs_attempts_say_where_and_how_they_ran(world: World) -> None:
    run = queued(world)
    viewer = api_as(world, "viewer")
    (attempt,) = viewer.get(f"/runs/{run['hrid']}/attempts").json()
    assert (attempt["attempt"], attempt["state"], attempt["worker"]) == (1, "queued", None)
    pod = worker(world, name="pod-7")
    job = pod.lease().json()
    pod.report(job, "progress", fraction=0.25, message="Weighing")
    pod.report(job, "fail", reason="interrupted", message="The node went away.")
    first, second = viewer.get(f"/runs/{run['id']}/attempts").json()
    assert (first["state"], first["worker"]) == ("interrupted", "pod-7")
    assert first["error"]["code"] == "interrupted"
    assert (first["progress"], first["progress_message"]) == (0.25, "Weighing")
    assert first["started_at"] and first["finished_at"]
    assert (second["attempt"], second["state"]) == (2, "queued")
    outsider = api_as(world, "outsider")
    problem(outsider.get(f"/runs/{run['id']}/attempts"), 404, "not_found")


@pytest.mark.django_db
def test_an_attempts_log_reads_from_any_offset(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = queued(world)
    pod = worker(world)
    job = pod.lease().json()
    viewer = api_as(world, "viewer")
    log = f"/runs/{run['hrid']}/attempts/1/log"
    empty = viewer.get(log).json()
    assert empty == {
        "offset": 0,
        "text": "",
        "next_offset": 0,
        "more": False,
        "size": 0,
        "ended": False,
    }
    pod.report(job, "logs", offset=0, text="héllo\n")  # 7 bytes
    pod.report(job, "logs", offset=7, text="world\n")
    whole = viewer.get(log).json()
    assert (whole["text"], whole["next_offset"], whole["size"]) == ("héllo\nworld\n", 13, 13)
    assert viewer.get(log, offset=7).json()["text"] == "world\n"
    assert viewer.get(log, offset=9).json()["text"] == "rld\n"  # inside a chunk
    assert viewer.get(log, offset=13).json() == {**whole, "offset": 13, "text": ""}
    # Long logs come a page at a time.
    monkeypatch.setattr(runs_api, "LOG_PAGE_BYTES", 8)
    page = viewer.get(log).json()
    assert (page["text"], page["next_offset"], page["more"]) == ("héllo\n", 7, True)
    pod.report(job, "fail", reason="failed", message="ValueError")
    assert viewer.get(log, offset=7).json()["ended"] is True
    problem(viewer.get(f"/runs/{run['hrid']}/attempts/2/log"), 404, "not_found")
    problem(viewer.get(log, offset=-1), 422, "validation_failed")
    problem(api_as(world, "outsider").get(log), 404, "not_found")


# --- Live updates --------------------------------------------------------------------------------


async def follow(client: AsyncClient, ref: str) -> Events:
    response = await client.get(f"/api/v1/runs/{ref}/events", secure=True)
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "text/event-stream; charset=utf-8"
    events = Events(response)
    ready = await events.next()
    assert ready is not None
    assert ready[0] == "ready"
    await asyncio.wait_for(hub().listening.wait(), 5)
    return events


@pytest.mark.django_db(transaction=True)
async def test_a_runs_page_hears_as_it_starts_logs_and_ends() -> None:
    world: World = await sync_to_async(build_world)()
    run: dict[str, Any] = await sync_to_async(queued)(world)
    await sync_to_async(worker)(world)
    pod = await Worker.objects.aget(name="pod-1")
    events = await follow(await signed_in(world.users["viewer"]), run["hrid"])
    try:
        leased = await sync_to_async(queue.lease)(pod)
        assert leased is not None
        job = leased[0]
        token = job.lease_token
        assert token is not None
        assert await events.next() == ("run", {"id": run["id"]})
        await sync_to_async(queue.append_log)(pod, job.pk, token, 0, "héllo\n")
        assert await events.next() == ("log", {"attempt": 1, "size": 7})
        await sync_to_async(queue.progress)(pod, job.pk, token, 0.5, "Halfway")
        assert await events.next() == ("run", {"id": run["id"]})
        await sync_to_async(queue.fail)(pod, job.pk, token, "failed", "ValueError")
        assert await events.next() == ("run", {"id": run["id"]})
    finally:
        await events.close()


@pytest.mark.django_db(transaction=True)
async def test_only_people_who_can_read_a_run_follow_it() -> None:
    world: World = await sync_to_async(build_world)()
    run: dict[str, Any] = await sync_to_async(queued)(world)
    outsider = await signed_in(world.users["outsider"])
    response = await outsider.get(f"/api/v1/runs/{run['id']}/events", secure=True)
    assert response.status_code == 404
    response = await AsyncClient().get(f"/api/v1/runs/{run['id']}/events", secure=True)
    assert response.status_code == 401
    response = await outsider.post(f"/api/v1/runs/{run['id']}/events", secure=True)
    assert response.status_code == 405


@pytest.mark.django_db
def test_an_attempts_whole_log_downloads_as_a_text_file(world: World) -> None:
    run = queued(world)
    pod = worker(world)
    job = pod.lease().json()
    pod.report(job, "logs", offset=0, text="héllo\n")
    pod.report(job, "logs", offset=7, text="world\n")
    viewer = api_as(world, "viewer")
    reply = viewer.get(f"/runs/{run['hrid']}/attempts/1/log.txt")
    assert reply.status_code == 200
    assert reply.content == "héllo\nworld\n".encode()
    assert reply["Content-Type"] == "text/plain; charset=utf-8"
    assert reply["Content-Disposition"] == f'attachment; filename="{run["hrid"]}-attempt-1.log"'
    assert reply["Cache-Control"] == "no-store"
    problem(viewer.get(f"/runs/{run['hrid']}/attempts/2/log.txt"), 404, "not_found")
    problem(
        api_as(world, "outsider").get(f"/runs/{run['id']}/attempts/1/log.txt"), 404, "not_found"
    )

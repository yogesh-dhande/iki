"""Authorization fuzzing for workers: every job route, with another worker's job.

A worker reports on a job with the job's lease token, which only the worker that leased
it was given. Here two workers each lease a job, and the first tries every job route
against the second's: with its own token, with the second's (as if it had leaked), and
on its own job with the second's token. Each must answer exactly as it does for a job
that doesn't exist, never succeed, and leave the second worker's job as it was.
"""

import json
from typing import Any

import pytest

# keep_connection is autouse: the lease view would otherwise close the test's connection.
from test_workers import keep_connection, queued, worker, world  # noqa: F401 - fixtures

from iki_server.api.worker import api
from iki_server.core.ids import uuid7
from iki_server.runs.models import Job

pytestmark = pytest.mark.django_db

SHA = "0" * 64
#: Each job route, and the least body it takes besides the lease token.
ROUTES: dict[str, dict[str, Any]] = {
    "/jobs/{job_id}/heartbeat": {},
    "/jobs/{job_id}/progress": {"fraction": 0.5},
    "/jobs/{job_id}/logs": {"offset": 0, "text": "x"},
    "/jobs/{job_id}/fail": {"reason": "failed"},
    "/jobs/{job_id}/complete": {"output": {}},
    "/jobs/{job_id}/uploads": {
        "name": "a.txt",
        "media_type": "text/plain",
        "size": 1,
        "sha256": SHA,
    },
    "/jobs/{job_id}/artifacts": {
        "path": "a.txt",
        "media_type": "text/plain",
        "size": 1,
        "sha256": SHA,
    },
    "/jobs/{job_id}/checkpoints": {"size": 1, "sha256": SHA},
    "/jobs/{job_id}/checkpoints/{checkpoint_id}/download": {},
    "/jobs/{job_id}/checkpoints/{checkpoint_id}/restored": {},
    "/jobs/{job_id}/uploads/{upload_id}/parts": {},
    "/jobs/{job_id}/uploads/{upload_id}/complete": {},
    "/jobs/{job_id}/files/{file_id}": {},
    # An async view outside the OpenAPI document; wait=0 answers at once.
    "/jobs/{job_id}/watch?wait=0": {},
}


def test_every_job_route_is_fuzzed() -> None:
    paths = {path.removeprefix("/worker/v1") for path in api.get_openapi_schema()["paths"]}
    # Setups' routes: test_setup_builds' test_only_a_setups_own_machine_reports_on_it.
    watch = {"/jobs/{job_id}/watch?wait=0"}
    assert {p for p in paths if p.startswith("/jobs/")} == set(ROUTES) - watch


def _said(response: Any, job_id: str) -> tuple[int, Any]:
    body = response.content.decode().replace(job_id, "<job>")
    said = json.loads(body) if body else None
    if isinstance(said, dict):
        said.pop("request_id", None)
    return response.status_code, said


@pytest.mark.parametrize("route", ROUTES)
def test_a_worker_never_reports_on_another_workers_job(
    world: Any,  # noqa: F811 - the fixture
    route: str,
    s3: Any,
) -> None:
    """With storage set up, so file routes reach their lease check."""
    mine, theirs = worker(world), worker(world, name="pod-2")
    queued(world)
    queued(world)
    my_job, their_job = mine.lease().json()["job"], theirs.lease().json()["job"]
    assert my_job["id"] != their_job["id"]
    before = Job.objects.filter(pk=their_job["id"]).values().get()
    nested = {"checkpoint_id": str(uuid7()), "upload_id": str(uuid7()), "file_id": str(uuid7())}
    body = ROUTES[route]

    def send(job_id: str, token: str) -> Any:
        path = route.format(job_id=job_id, **nested)
        return mine.post(path, {"lease_token": token, **body})

    made_up = str(uuid7())
    nothing = _said(send(made_up, my_job["lease_token"]), made_up)
    for job_id, token in (
        (their_job["id"], my_job["lease_token"]),
        (their_job["id"], their_job["lease_token"]),
        (my_job["id"], their_job["lease_token"]),
    ):
        response = send(job_id, token)
        where = f"{route} on {'their' if job_id == their_job['id'] else 'my'} job"
        assert response.status_code >= 400, (where, response.content[:300])
        assert response.status_code < 500, (where, response.content[:300])
        if job_id == their_job["id"]:
            assert _said(response, job_id) == nothing, where
    assert Job.objects.filter(pk=their_job["id"]).values().get() == before

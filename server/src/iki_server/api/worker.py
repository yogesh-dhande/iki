"""``/worker/v1``: the API ``iki-worker`` uses (ADR 0043).

Not for people or the SDK: a worker registers with a registration token
(``iki_wreg_…``), then uses its own token (``iki_wkr_…``) to lease jobs and
report on them. ``POST /worker/v1/lease`` waits up to 30 seconds for a job
it can run, so idle workers make two requests a minute, not hundreds. Every
report names the lease token it got with the job; one that isn't current
answers 409 ``stale_lease``, and the worker should stop that job.

Its OpenAPI document is at ``/worker/v1/openapi.json``.
"""

import asyncio
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.views.decorators.csrf import csrf_exempt
from ninja import Field, NinjaAPI, Schema, Status
from ninja.errors import AuthenticationError, HttpError, ValidationError
from ninja.security import HttpBearer

from iki_server.api.problems import Problem, from_validation, response
from iki_server.api.security import ANONYMOUS_LIMIT, _limit
from iki_server.realtime.views import db
from iki_server.runs import queue, wakeup, workers
from iki_server.runs.models import RegistrationToken, Worker
from iki_server.web.ratelimit import RateLimiter, client_key

#: The longest a lease request waits for a job.
MAX_WAIT = 30.0
#: Requests one worker may make per minute: heartbeats, progress and log chunks for its jobs.
WORKER_LIMIT = 1200


def _refuse(request: HttpRequest) -> None:
    _limit(request, RateLimiter("worker_anonymous", ANONYMOUS_LIMIT), client_key(request))


class RegistrationAuth(HttpBearer):
    """A registration token, to register with."""

    def authenticate(self, request: HttpRequest, token: str) -> RegistrationToken | None:
        found = workers.registration_for(f"Bearer {token}")
        if found is None:
            _refuse(request)
        return found


class WorkerAuth(HttpBearer):
    """A worker's own token."""

    def authenticate(self, request: HttpRequest, token: str) -> Worker | None:
        worker = workers.authenticate(f"Bearer {token}")
        if worker is None:
            _refuse(request)
            return None
        _limit(request, RateLimiter("worker", WORKER_LIMIT), f"worker:{worker.pk}")
        return worker


api = NinjaAPI(
    title="Iki worker API",
    version="1",
    description="What `iki-worker` uses to lease jobs and report on them. Not for people.",
    urls_namespace="worker-v1",
    auth=WorkerAuth(),
    docs_url=None,
    openapi_url="/openapi.json",
)


@api.exception_handler(Problem)
def _problem(request: HttpRequest, exc: Problem) -> HttpResponse:
    return response(exc)


@api.exception_handler(ValidationError)
def _invalid(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    return response(from_validation(list(exc.errors)))


@api.exception_handler(AuthenticationError)
def _unauthenticated(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
    return response(Problem(401, "The worker token is missing, wrong or revoked."))


@api.exception_handler(HttpError)
def _http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    if exc.status_code == 400:
        return response(Problem(400, "The request body isn’t valid JSON.", code="invalid_json"))
    return response(Problem(exc.status_code, str(exc) or None))


@api.exception_handler(queue.Stale)
def _stale(request: HttpRequest, exc: queue.Stale) -> HttpResponse:
    return response(
        Problem(
            409,
            "This worker doesn’t hold the job’s lease any more. Stop the job and lease another.",
            code="stale_lease",
        )
    )


def _worker(request: HttpRequest) -> Worker:
    worker = request.auth  # type: ignore[attr-defined]
    assert isinstance(worker, Worker)  # noqa: S101 - WorkerAuth set it
    return worker


# --- Registering -------------------------------------------------------------------------------

Bytes = Annotated[int, Field(ge=0, le=2**53)]


class Capacity(Schema):
    """What the worker can give one job. Memory and disk are in bytes."""

    cpu: float = Field(gt=0, le=4096)
    memory: Bytes
    gpu_count: int = Field(0, ge=0, le=64)
    gpu_memory: Bytes = 0
    scratch_disk: Bytes = 0


class RegisterIn(Schema):
    name: str = Field(
        min_length=1, max_length=200, description="Its host or pod name, for people to tell it by."
    )
    version: str = Field(min_length=1, max_length=50, description="The iki-worker version.")
    capacity: Capacity


class RegisteredOut(Schema):
    id: UUID
    tier: str
    token: str = Field(description="Its own token, for everything else. Shown once.")
    heartbeat_every: int = Field(description="Seconds between heartbeats while it holds a job.")


@api.post(
    "/register",
    auth=RegistrationAuth(),
    response={201: RegisteredOut},
    summary="Register a worker",
)
def register(request: HttpRequest, payload: RegisterIn) -> Any:  # noqa: ANN401
    registration = request.auth  # type: ignore[attr-defined]
    assert isinstance(registration, RegistrationToken)  # noqa: S101 - RegistrationAuth set it
    worker, token = workers.register(
        registration,
        name=payload.name,
        version=payload.version,
        capacity=payload.capacity.model_dump(),
    )
    return Status(
        201,
        {
            "id": worker.pk,
            "tier": worker.tier,
            "token": token,
            "heartbeat_every": int(queue.HEARTBEAT_EVERY.total_seconds()),
        },
    )


class WorkerOut(Schema):
    id: UUID
    name: str
    tier: str
    version: str
    capacity: dict[str, Any]


@api.get("/me", response=WorkerOut, summary="This worker")
def me(request: HttpRequest) -> Worker:
    """Whether its token still works, and what the server knows of it."""
    return _worker(request)


@api.post("/deregister", response={204: None}, summary="Stop being a worker")
def deregister(request: HttpRequest) -> Any:  # noqa: ANN401
    """Revokes this worker's token, when it shuts down for good. Jobs it still holds are
    interrupted, and retried elsewhere if their action allows."""
    workers.revoke_worker(_worker(request), by=None)
    return Status(204, None)


# --- Reporting on a job ------------------------------------------------------------------------

JobState = Literal["leased", "staging", "running", "uploading"]


class LeaseIn(Schema):
    lease_token: UUID


class HeartbeatIn(LeaseIn):
    state: JobState | None = Field(
        None, description="What it's doing now: staging inputs, running, or uploading results."
    )


class HeartbeatOut(Schema):
    lease_expires_at: datetime
    cancel_requested: bool = Field(
        description="Someone cancelled the run: stop the command, then report `cancelled`."
    )


@api.post("/jobs/{job_id}/heartbeat", response=HeartbeatOut, summary="Keep a lease")
def heartbeat(request: HttpRequest, job_id: UUID, payload: HeartbeatIn) -> dict[str, Any]:
    try:
        expires, cancelled = queue.heartbeat(
            _worker(request), job_id, payload.lease_token, payload.state
        )
    except ValueError as error:
        raise Problem(409, str(error), code="wrong_state") from None
    return {"lease_expires_at": expires, "cancel_requested": cancelled}


class ProgressIn(LeaseIn):
    fraction: float = Field(ge=0, le=1)
    message: str = Field("", max_length=500)


@api.post("/jobs/{job_id}/progress", response={204: None}, summary="Report progress")
def progress(request: HttpRequest, job_id: UUID, payload: ProgressIn) -> Any:  # noqa: ANN401
    queue.progress(_worker(request), job_id, payload.lease_token, payload.fraction, payload.message)
    return Status(204, None)


class LogIn(LeaseIn):
    offset: int = Field(ge=0, description="Where this chunk starts, in bytes of UTF-8.")
    text: str = Field(max_length=256 * 1024)


class LogOut(Schema):
    next_offset: int


@api.post("/jobs/{job_id}/logs", response=LogOut, summary="Send log output")
def logs(request: HttpRequest, job_id: UUID, payload: LogIn) -> dict[str, int]:
    """Chunks go in order. Sending one again (after a timeout, say) is harmless; skipping
    ahead answers 409 ``log_gap`` with where the log continues."""
    try:
        end = queue.append_log(
            _worker(request), job_id, payload.lease_token, payload.offset, payload.text
        )
    except queue.LogGap as gap:
        raise Problem(
            409,
            str(gap),
            code="log_gap",
            errors=[{"pointer": "#/offset", "detail": f"Send from byte {gap.expected}."}],
        ) from None
    return {"next_offset": end}


class FailIn(LeaseIn):
    reason: Literal["failed", "timed_out", "interrupted", "cancelled"] = Field(
        description="`failed`: the command's own error. `timed_out`: it ran past its timeout. "
        "`interrupted`: the machine was stopped. `cancelled`: it was asked to stop."
    )
    message: str = Field("", max_length=2000)


class FailOut(Schema):
    run_status: str = Field(description="`queued` again if it's retried; otherwise how it ended.")


@api.post("/jobs/{job_id}/fail", response=FailOut, summary="End an attempt without results")
def fail(request: HttpRequest, job_id: UUID, payload: FailIn) -> dict[str, str]:
    run = queue.fail(_worker(request), job_id, payload.lease_token, payload.reason, payload.message)
    return {"run_status": run.status}


class CompleteIn(LeaseIn):
    output: dict[str, Any] = Field(
        description="The command's output.json (`run-output/v1`). Up to 2.5 MB; files come "
        "separately."
    )


class OutputErrorOut(Schema):
    pointer: str = Field(description="Where in the output, as a JSON pointer.")
    detail: str


class CompleteOut(Schema):
    run_status: str = Field(
        description="`succeeded` if the results were written; `failed` (`invalid_output`) or "
        "`rejected` if they weren't. Either way the output is kept with the run."
    )
    code: str | None = None
    message: str | None = None
    errors: list[OutputErrorOut] = Field(default_factory=list)


@api.post("/jobs/{job_id}/complete", response=CompleteOut, summary="Finish with results")
def complete(request: HttpRequest, job_id: UUID, payload: CompleteIn) -> dict[str, Any]:
    """Writes the results in one transaction, all or none, after checking them against
    what the action declares. Whatever the answer, the attempt is over: don't retry it."""
    run = queue.complete(_worker(request), job_id, payload.lease_token, payload.output)
    error = run.error or {}
    return {
        "run_status": run.status,
        "code": error.get("code"),
        "message": error.get("message"),
        "errors": error.get("errors", []),
    }


# --- Leasing (an async view, outside django-ninja) ---------------------------------------------


def _authenticate(request: HttpRequest) -> Worker:
    worker = workers.authenticate(request.headers.get("Authorization", ""))
    if worker is None:
        _refuse(request)
        raise Problem(401, "The worker token is missing, wrong or revoked.")
    _limit(request, RateLimiter("worker", WORKER_LIMIT), f"worker:{worker.pk}")
    return worker


def _lease(worker: Worker) -> dict[str, Any] | None:
    found = queue.lease(worker)
    return found[1] if found is not None else None


def _wait(request: HttpRequest) -> float:
    raw = request.GET.get("wait", str(MAX_WAIT))
    try:
        wait = float(raw)
    except ValueError:
        raise Problem(
            422,
            "`wait` is a number of seconds.",
            errors=[{"parameter": "wait", "detail": f"Use 0 to {MAX_WAIT:g}."}],
        ) from None
    if not 0 <= wait <= MAX_WAIT:
        raise Problem(
            422,
            f"`wait` is from 0 to {MAX_WAIT:g} seconds.",
            errors=[{"parameter": "wait", "detail": f"Use 0 to {MAX_WAIT:g}."}],
        )
    return wait


#: While waiting, how often to look again anyway (a retry's backoff ends without a notice).
RECHECK = 10.0


@csrf_exempt
async def lease(request: HttpRequest) -> HttpResponseBase:
    """``POST /worker/v1/lease?wait=30``: the next job this worker should run.

    200 with the job's run input (``spec/…/run-input/v1``), or 204 if none came within
    ``wait`` seconds; the worker then asks again.
    """
    if request.method != "POST":
        return response(Problem(405, headers={"Allow": "POST"}))
    try:
        wait = _wait(request)
        worker = await db(_authenticate, request)
    except Problem as problem:
        return response(problem)
    waker = wakeup.waker()
    loop_time = asyncio.get_running_loop().time
    deadline = loop_time() + wait
    while True:
        found = await db(_lease, worker)
        if found is not None:
            reply = HttpResponse(
                json.dumps(found, separators=(",", ":")), content_type="application/json"
            )
            reply["Cache-Control"] = "no-store"
            return reply
        left = deadline - loop_time()
        if left <= 0 or waker.closing.is_set():
            break
        await waker.wait(worker.tier, min(left, RECHECK))
    reply = HttpResponse(status=204)
    reply["Cache-Control"] = "no-store"
    return reply

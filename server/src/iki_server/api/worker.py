"""``/worker/v1``: the API ``iki-worker`` uses (ADR 0043).

Not for people or the SDK: a worker registers with a registration token
(``iki_wreg_…``), then uses its own token (``iki_wkr_…``) to lease jobs and
report on them. ``POST /worker/v1/lease`` waits up to 30 seconds for a job
it can run, so idle workers make two requests a minute, not hundreds. Every
report names the lease token it got with the job; one that isn't current
answers 409 ``stale_lease``, and the worker should stop that job. While a job
runs, ``POST /worker/v1/jobs/{id}/watch`` waits the same way for its run to
be cancelled, so a cancel reaches the command at once, not at the next
heartbeat.

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
from pydantic import field_validator

from iki_server.api.problems import Problem, from_validation, response
from iki_server.api.security import ANONYMOUS_LIMIT, _limit
from iki_server.files import services as files
from iki_server.files import storage
from iki_server.files.models import Upload, UploadState
from iki_server.org.services import Refused
from iki_server.realtime.views import db
from iki_server.runs import input_files, queue, uploads, wakeup, workers
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


@api.exception_handler(Refused)
def _refused(request: HttpRequest, exc: Refused) -> HttpResponse:
    if exc.field is not None:
        return response(
            Problem(
                422,
                "Some fields are invalid.",
                errors=[{"pointer": f"#/{exc.field}", "detail": str(exc)}],
            )
        )
    return response(Problem(409, str(exc), code="refused"))


@api.exception_handler(LookupError)
def _missing(request: HttpRequest, exc: LookupError) -> HttpResponse:
    return response(Problem(404, str(exc)))


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


PluginName = Annotated[
    str, Field(min_length=2, max_length=30, pattern=r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
]
EnvironmentName = Annotated[
    str, Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
]
BuildDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class PluginCode(Schema):
    """Plugin code a worker has. It leases only runs launched with that very code."""

    name: PluginName
    sha: str | None = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="The commit it was built from, or null for a local directory, which "
        "runs only what a development server launches.",
    )
    environments: dict[EnvironmentName, BuildDigest] | None = Field(
        max_length=100,
        description="Each environment it has built, with that build's digest; the worker "
        "leases only runs in these. Null runs every one with the host's own software, "
        "as for development.",
    )


class WorkerIn(Schema):
    version: str = Field(min_length=1, max_length=50, description="The iki-worker version.")
    capacity: Capacity
    plugins: list[PluginCode] = Field(
        max_length=200, description="All it runs. A worker without plugins leases nothing."
    )

    @field_validator("plugins")
    @classmethod
    def _once_each(cls, plugins: list[PluginCode]) -> list[PluginCode]:
        names = [p.name for p in plugins]
        if len(set(names)) != len(names):
            raise ValueError("lists a plugin more than once.")
        return plugins


class RegisterIn(WorkerIn):
    name: str = Field(
        min_length=1, max_length=200, description="Its host or pod name, for people to tell it by."
    )


def _plugins(payload: WorkerIn) -> list[dict[str, Any]]:
    return [p.model_dump() for p in sorted(payload.plugins, key=lambda p: p.name)]


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
        plugins=_plugins(payload),
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
    plugins: list[PluginCode]


@api.get("/me", response=WorkerOut, summary="This worker")
def me(request: HttpRequest) -> Worker:
    """Whether its token still works, and what the server knows of it."""
    return _worker(request)


@api.put("/me", response=WorkerOut, summary="Say what this worker runs now")
def update_me(request: HttpRequest, payload: WorkerIn) -> Worker:
    """For a worker that kept its token, each time it starts: a new image can bring a new
    version, plugins or environments."""
    return workers.update(
        _worker(request),
        version=payload.version,
        capacity=payload.capacity.model_dump(),
        plugins=_plugins(payload),
    )


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
    ahead answers 409 ``log_gap`` with where the log continues, in ``next_offset``."""
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
            extensions={"next_offset": gap.expected},
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


# --- Uploading result files --------------------------------------------------------------------


class UploadIn(LeaseIn):
    name: str = Field(max_length=1000, description="Its file name, such as model_1.pdb.")
    media_type: str = Field(max_length=255, description="Such as chemical/x-pdb.")
    size: int = Field(ge=0, description="Bytes.")
    sha256: str = Field(min_length=64, max_length=64, description="Of its bytes, as hex.")


class PartsIn(LeaseIn):
    numbers: list[int] | None = Field(
        None, max_length=1000, description="The parts to sign again; all of them if left out."
    )


class PartOut(Schema):
    number: int
    size: int = Field(description="Bytes this part must hold.")
    url: str = Field(description="PUT the part's bytes here. It works for an hour.")


class UploadOut(Schema):
    id: UUID
    state: Literal["pending", "checking", "ready", "failed", "cancelled", "expired"]
    file: UUID | None = Field(
        alias="file_id", description="Once ready: the file's ID, for output.json."
    )
    error: str = Field(description="Why it failed.")


class StartedOut(UploadOut):
    parts: list[PartOut]


def _storage() -> None:
    if not storage.configured():
        raise Problem(503, str(storage.NotConfigured()), code="storage_not_configured")


@api.post("/jobs/{job_id}/uploads", response={201: StartedOut}, summary="Upload a result file")
def start_upload(request: HttpRequest, job_id: UUID, payload: UploadIn) -> Any:  # noqa: ANN401
    """Answers with the parts to PUT. Then finish it, and finish it again until it's `ready`
    (Iki checks the bytes), and attach its file to entities in output.json."""
    _storage()
    upload = uploads.start(
        _worker(request),
        job_id,
        payload.lease_token,
        name=payload.name,
        media_type=payload.media_type,
        size=payload.size,
        sha256=payload.sha256,
    )
    parts = files.parts(upload, workers=True) if upload.state == UploadState.PENDING else []
    return Status(201, {**_upload_out(upload), "parts": parts})


def _upload_out(upload: Upload) -> dict[str, Any]:
    return {
        "id": upload.pk,
        "state": upload.state,
        "file_id": upload.file_id,
        "error": upload.error,
    }


@api.post(
    "/jobs/{job_id}/uploads/{upload_id}/parts",
    response=list[PartOut],
    summary="Sign a result file's parts again",
)
def sign_parts(
    request: HttpRequest, job_id: UUID, upload_id: UUID, payload: PartsIn
) -> list[dict[str, Any]]:
    _storage()
    return uploads.parts(_worker(request), job_id, payload.lease_token, upload_id, payload.numbers)


@api.post(
    "/jobs/{job_id}/uploads/{upload_id}/complete",
    response=UploadOut,
    summary="Finish a result file",
)
def complete_upload(
    request: HttpRequest, job_id: UUID, upload_id: UUID, payload: LeaseIn
) -> dict[str, Any]:
    """Once every part is in. Ask again until it's `ready` (or `failed`): finishing it again
    changes nothing."""
    _storage()
    return _upload_out(uploads.complete(_worker(request), job_id, payload.lease_token, upload_id))


class DownloadOut(Schema):
    url: str = Field(description="GET the file's bytes here. It works for five minutes.")
    name: str
    media_type: str
    size: int = Field(description="Bytes.")
    sha256: str = Field(description="Of its bytes, as hex: check them against it.")


@api.post("/jobs/{job_id}/files/{file_id}", response=DownloadOut, summary="Download an input file")
def download(request: HttpRequest, job_id: UUID, file_id: UUID, payload: LeaseIn) -> dict[str, Any]:
    """For a file the job's inputs hold (the run input's ``files``). Ask again for a new
    URL if it runs out."""
    _storage()
    found, url = input_files.download(_worker(request), job_id, payload.lease_token, file_id)
    return {
        "url": url,
        "name": found.name,
        "media_type": found.media_type,
        "size": found.blob.size,
        "sha256": found.blob.sha256,
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


def _no_store(reply: HttpResponseBase) -> HttpResponseBase:
    reply["Cache-Control"] = "no-store"
    return reply


def _lease_token(request: HttpRequest) -> UUID:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise Problem(400, "The request body isn’t valid JSON.", code="invalid_json") from None
    try:
        return UUID(str(body["lease_token"]))
    except (KeyError, TypeError, ValueError):
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/lease_token", "detail": "The job’s lease token."}],
        ) from None


@csrf_exempt
async def watch(request: HttpRequest, job_id: UUID) -> HttpResponseBase:
    """``POST /worker/v1/jobs/{id}/watch?wait=30`` with ``{"lease_token": …}``: answers
    ``{"cancel_requested": true}`` the moment someone cancels the run, or ``false`` after
    ``wait`` seconds, and the worker asks again. 409 ``stale_lease`` once the worker
    doesn't hold the job. It doesn't renew the lease: heartbeats do that.
    """
    if request.method != "POST":
        return response(Problem(405, headers={"Allow": "POST"}))
    try:
        wait = _wait(request)
        worker = await db(_authenticate, request)
        token = _lease_token(request)
    except Problem as problem:
        return response(problem)
    waker = wakeup.waker()
    loop_time = asyncio.get_running_loop().time
    deadline = loop_time() + wait
    while True:
        try:
            cancelled = await db(queue.cancel_requested, worker, job_id, token)
        except queue.Stale as stale:
            return _stale(request, stale)
        left = deadline - loop_time()
        if cancelled or left <= 0 or waker.closing.is_set():
            return _no_store(
                HttpResponse(
                    json.dumps({"cancel_requested": cancelled}), content_type="application/json"
                )
            )
        await waker.wait(queue.watch_key(job_id), min(left, RECHECK))


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

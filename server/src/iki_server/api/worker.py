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
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from django.core.handlers.asgi import ASGIRequest
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from django.views.decorators.csrf import csrf_exempt
from ninja import Field, NinjaAPI, Schema, Status
from ninja.errors import AuthenticationError, HttpError, ValidationError
from ninja.security import HttpBearer
from pydantic import field_validator

from iki_server.accounts.models import User
from iki_server.api.problems import Problem, from_validation, response
from iki_server.api.security import ANONYMOUS_LIMIT, _limit
from iki_server.entities import registry
from iki_server.files import services as files
from iki_server.files import storage
from iki_server.files.models import Upload, UploadState
from iki_server.org.services import Refused
from iki_server.realtime.views import db
from iki_server.runs import (
    artifacts,
    catalog,
    checkpoints,
    input_files,
    launches,
    queue,
    uploads,
    wakeup,
    workers,
)
from iki_server.runs.models import Launch, RegistrationToken, Worker
from iki_server.web.ratelimit import RateLimiter, client_key

#: The longest a lease request waits for a job.
MAX_WAIT = 30.0
#: Requests one worker may make per minute: heartbeats, progress and log chunks for its jobs.
WORKER_LIMIT = 1200


def _refuse(request: HttpRequest) -> None:
    _limit(request, RateLimiter("worker_anonymous", ANONYMOUS_LIMIT), client_key(request))


@dataclass(frozen=True)
class SignedIn:
    """Someone who signed a worker in on their own computer (``iki-worker login``)."""

    person: User
    access_token: Any


class RegistrationAuth(HttpBearer):
    """A registration token, or the access token ``iki-worker login`` got, to register
    with."""

    def authenticate(
        self, request: HttpRequest, token: str
    ) -> RegistrationToken | SignedIn | Launch | None:
        if token.startswith(launches.LAUNCH_PREFIX):
            launch = launches.for_token(token)
            if launch is None:
                _refuse(request)
            return launch
        if token.startswith(workers.REGISTRATION_PREFIX):
            found = workers.registration_for(f"Bearer {token}")
            if found is None:
                _refuse(request)
            return found
        signed_in = workers.signed_in(token)
        if signed_in is None:
            _refuse(request)
            return None
        return SignedIn(*signed_in)


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
    """With a registration token for a deployment's workers, or, for a worker on someone's
    own computer, the access token ``iki-worker login`` got them (which this spends)."""
    auth = request.auth  # type: ignore[attr-defined]
    details: dict[str, Any] = {
        "name": payload.name,
        "version": payload.version,
        "capacity": payload.capacity.model_dump(),
        "plugins": _plugins(payload),
    }
    if isinstance(auth, SignedIn):
        worker, token = workers.register_client(auth.person, auth.access_token, **details)
    elif isinstance(auth, Launch):
        template = catalog.compute().templates.get(auth.template)
        if template is None:
            raise Problem(
                409,
                f"This deployment no longer has the compute template `{auth.template}` it was "
                "launched for.",
                code="template_gone",
            )
        try:
            worker, token = launches.register(
                auth,
                name=payload.name,
                version=payload.version,
                capacity=details["capacity"],
                template_capacity=launches.capacity_of(template),
            )
        except Launch.DoesNotExist:
            _refuse(request)
            raise Problem(401) from None
    else:
        assert isinstance(auth, RegistrationToken)  # noqa: S101 - RegistrationAuth set it
        worker, token = workers.register(auth, **details)
    return Status(
        201,
        {
            "id": worker.pk,
            "tier": worker.tier,
            "token": token,
            "heartbeat_every": int(queue.HEARTBEAT_EVERY.total_seconds()),
        },
    )


class UpdateOut(Schema):
    """A release of iki-worker to update to (ADR 0060). It checks the release's signature
    before running it."""

    version: str
    url: str = Field(
        description="The release for this computer, with `{target}` for its target, such as "
        "`aarch64-apple-darwin`."
    )
    bundle_url: str = Field(description="Its Sigstore bundle, the same way.")


class WorkerOut(Schema):
    id: UUID
    name: str
    tier: str
    version: str
    capacity: dict[str, Any]
    plugins: list[PluginCode]
    update: UpdateOut | None = Field(
        description="For a worker on someone's computer: the release to update itself to, "
        "when it's idle. Null when it runs the one it should."
    )

    @staticmethod
    def resolve_update(worker: Worker) -> dict[str, str] | None:
        return workers.update_for(worker)


def _current(worker: Worker) -> None:
    """Refuses to give an outdated worker jobs (409 ``worker_outdated``), with the release
    to update to when it can update itself."""
    try:
        workers.check_version(worker.version)
    except workers.Outdated as outdated:
        raise Problem(
            409,
            str(outdated),
            code="worker_outdated",
            extensions={"update": workers.update_for(worker)},
        ) from None


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


class BundleOut(Schema):
    sha256: str = Field(description="Of the `.tar.gz`, which the worker checks.")
    size: int


class EnvironmentBuildOut(Schema):
    digest: BuildDigest = Field(description="What the worker reports once it has built it.")
    kind: Literal["python", "conda"]
    python: str | None = Field(None, description="For `python`: the version uv makes it with.")
    requirements: str | None = Field(
        None, description="For `python`: the pinned, hashed requirements file in the bundle."
    )
    packages: bool = Field(
        False, description="For `python`: whether the requirements file names any packages."
    )
    lockfile: str | None = Field(None, description="For `conda`: the lockfile in the bundle.")


class AvailableOut(Schema):
    name: PluginName
    version: str
    sha: str
    bundle: BundleOut
    environments: dict[str, EnvironmentBuildOut]


class AvailableListOut(Schema):
    plugins: list[AvailableOut]


@api.get("/plugins", response=AvailableListOut, summary="Plugin code to fetch")
def available(request: HttpRequest) -> dict[str, Any]:
    """The code of each plugin this deployment installs from a commit, for a worker on
    someone's computer to fetch and build (ADR 0059): it then runs what's launched now.
    A machine Iki started for runs gets only its own plugin's (ADR 0061): on a development
    server, that may be one from a local directory, whose ``sha`` is ``local``."""
    worker = _worker(request)
    mine = worker.launch.environment.partition(".")[0] if worker.launch is not None else None
    plugins = [
        {
            "name": entry["name"],
            "version": entry["version"],
            "sha": entry.get("sha") or registry.LOCAL,
            "bundle": entry["bundle"],
            "environments": entry.get("environments") or {},
        }
        for entry in (registry.installed() or {}).get("plugins", ())
        if entry.get("bundle")
        and entry.get("state", "active") == "active"
        and (entry["name"] == mine if mine is not None else bool(entry.get("sha")))
    ]
    return {"plugins": sorted(plugins, key=lambda p: p["name"])}


@api.get(
    "/plugins/{name}/{sha}",
    response={200: None},
    summary="Download a plugin's code",
    openapi_extra={
        "responses": {200: {"content": {"application/gzip": {}}, "description": "The bundle"}}
    },
)
def download_plugin(request: HttpRequest, name: str, sha: str) -> HttpResponseBase:
    """A gzipped tar of the plugin's code at that commit, which ``iki deploy build`` wrote.
    Check its SHA-256 against the list before unpacking it."""
    worker = _worker(request)
    local = sha == registry.LOCAL
    # A local plugin's code goes only to the machines a development server starts for it.
    mine = worker.launch is not None and worker.launch.environment.partition(".")[0] == name
    named = re.fullmatch(r"[0-9a-f]{40}", sha) is not None or (local and mine)
    file = registry.bundle(name, sha) if named else None
    if file is None:
        raise HttpError(404, "This deployment has no code for that plugin at that commit.")
    response = _send(request, file, "application/gzip")
    # A commit's code never changes; a local directory's may with each build.
    response["Cache-Control"] = "no-store" if local else "private, max-age=31536000, immutable"
    return response


#: How much of a file a download reads at a time.
CHUNK = 1 << 20


def _send(request: HttpRequest, file: Path, content_type: str) -> HttpResponseBase:
    """Streams `file`. Served over ASGI, as in production, it's read in a thread a chunk
    at a time, rather than by Django consuming a synchronous iterator."""
    if not isinstance(request, ASGIRequest):
        return FileResponse(file.open("rb"), content_type=content_type)

    async def chunks() -> AsyncIterator[bytes]:
        handle = await asyncio.to_thread(file.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, CHUNK):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    response = StreamingHttpResponse(chunks(), content_type=content_type)
    response["Content-Length"] = str(file.stat().st_size)
    return response


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
        "`rejected` if they weren't. Either way the output is kept with the run. `running` "
        "when its creates came in a file: Iki is importing them (ADR 0056), and the job is "
        "over for the worker all the same."
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


class ArtifactIn(LeaseIn):
    path: str = Field(
        max_length=500,
        description="Where the command wrote it, relative to its output directory, such as "
        "msas/uniref90.a3m. One of the action's `artifacts.keep` patterns must match it.",
    )
    media_type: str = Field(max_length=255, description="Such as text/plain.")
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


@api.post("/jobs/{job_id}/artifacts", response={201: StartedOut}, summary="Upload an artifact")
def start_artifact(request: HttpRequest, job_id: UUID, payload: ArtifactIn) -> Any:  # noqa: ANN401
    """A file the command wrote that the action keeps (ADR 0068), whether the command
    succeeded or not. Answers with the parts to PUT; then finish it like a result file,
    under `uploads/{id}`. Its `file_id` stays null: an artifact isn't a file."""
    _storage()
    upload = artifacts.start(
        _worker(request),
        job_id,
        payload.lease_token,
        path=payload.path,
        media_type=payload.media_type,
        size=payload.size,
        sha256=payload.sha256,
    )
    parts = files.parts(upload, workers=True) if upload.state == UploadState.PENDING else []
    return Status(201, {**_upload_out(upload), "parts": parts})


class CheckpointIn(LeaseIn):
    size: int = Field(ge=0, description="Bytes of the packed checkpoint directory.")
    sha256: str = Field(min_length=64, max_length=64, description="Of its bytes, as hex.")


@api.post("/jobs/{job_id}/checkpoints", response={201: StartedOut}, summary="Save a checkpoint")
def start_checkpoint(request: HttpRequest, job_id: UUID, payload: CheckpointIn) -> Any:  # noqa: ANN401
    """For a checkpointable action (ADR 0069): the command's checkpoint directory, packed as
    an uncompressed tar file of its regular files and directories. Answers with the parts to
    PUT; then finish it like a result file, under `uploads/{id}`, until it's `ready`. A later
    attempt resumes from the newest one that's ready."""
    _storage()
    upload = checkpoints.start(
        _worker(request), job_id, payload.lease_token, size=payload.size, sha256=payload.sha256
    )
    parts = files.parts(upload, workers=True) if upload.state == UploadState.PENDING else []
    return Status(201, {**_upload_out(upload), "parts": parts})


class CheckpointDownloadOut(Schema):
    url: str = Field(description="GET the checkpoint's bytes here. It works for five minutes.")
    size: int = Field(description="Bytes.")
    sha256: str = Field(description="Of its bytes, as hex: check them against it.")


@api.post(
    "/jobs/{job_id}/checkpoints/{checkpoint_id}/download",
    response=CheckpointDownloadOut,
    summary="Download a checkpoint",
)
def download_checkpoint(
    request: HttpRequest, job_id: UUID, checkpoint_id: UUID, payload: LeaseIn
) -> dict[str, Any]:
    """The checkpoint the run input's `job.checkpoint.saved` names, which an earlier attempt
    saved. Ask again for a new URL if it runs out."""
    _storage()
    found, url = checkpoints.download(_worker(request), job_id, payload.lease_token, checkpoint_id)
    return {"url": url, "size": found.upload.size, "sha256": found.upload.sha256}


@api.post(
    "/jobs/{job_id}/checkpoints/{checkpoint_id}/restored",
    response={204: None},
    summary="Say a checkpoint was restored",
)
def restored_checkpoint(
    request: HttpRequest, job_id: UUID, checkpoint_id: UUID, payload: LeaseIn
) -> Status[None]:
    """Once the checkpoint is unpacked into the command's checkpoint directory, before the
    command starts: the attempt records that it resumed from it. Not said for one that
    couldn't be restored, so the attempt starts afresh."""
    checkpoints.restored(_worker(request), job_id, payload.lease_token, checkpoint_id)
    return Status(204, None)


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
        _current(worker)
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

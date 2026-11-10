"""Files: uploading them in parts, and reading and downloading them (ADR 0036).

To add a file:

1. ``POST /uploads`` with its name, media type, size and SHA-256. The answer
   lists its parts, each with a signed URL.
2. ``PUT`` each part's bytes to its URL. Parts can go in parallel, and in
   any order. URLs last an hour: ``POST /uploads/{id}/parts`` gives new ones.
3. ``POST /uploads/{id}/complete``. Iki checks the bytes against the SHA-256
   (state ``checking``), then the upload is ``ready``, with its ``file``.
   ``upload.finished`` arrives on ``/events`` when it's done.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project
from iki_server.api.entities import ProjectRef
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, UserOut
from iki_server.files import services, storage
from iki_server.files.models import File, Upload, UploadState
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused

router = Router(tags=["files"])


# --- Schemas -------------------------------------------------------------------------------------


class FileOut(Schema):
    id: UUID
    name: str
    media_type: str
    size: int = Field(description="Bytes.")
    sha256: str
    project: ProjectRef | None = Field(description="Where it was added. Null if you can't see it.")
    created_by: UserOut | None
    created_at: datetime
    external: bool = Field(description="Registered where it already was, rather than uploaded.")


class PartOut(Schema):
    number: int
    size: int = Field(description="Bytes this part must hold.")
    url: str = Field(description="PUT the part's bytes here. It works for an hour.")


class UploadOut(Schema):
    id: UUID
    project: UUID
    name: str
    media_type: str
    size: int
    sha256: str
    state: Literal["pending", "checking", "ready", "failed", "cancelled", "expired"]
    part_size: int
    part_count: int
    file: FileOut | None = Field(description="The file it became, once ready.")
    error: str = Field(description="Why it failed.")
    created_at: datetime
    expires_at: datetime = Field(description="Unfinished then, it's cancelled.")


class StartedOut(UploadOut):
    parts: list[PartOut] = Field(description="Every part, with where to send it.")


class UploadIn(Schema):
    project: UUID = Field(description="The project to add it to. It needs you as an Editor.")
    name: str = Field(max_length=1000, description="Its file name, such as 1abc.pdb.")
    media_type: str = Field(max_length=255, description="Such as chemical/x-pdb.")
    size: int = Field(ge=0, description="Bytes.")
    sha256: str = Field(
        min_length=64, max_length=64, description="Of its bytes, as hex: Iki checks them."
    )


class PartsIn(Schema):
    numbers: list[int] | None = Field(
        None, max_length=1000, description="The parts to sign again; all of them if left out."
    )


class FileFilters(PageParams):
    project: UUID | None = None


# --- Helpers -------------------------------------------------------------------------------------


def _storage() -> None:
    if not storage.configured():
        raise Problem(503, str(storage.NotConfigured()), code="storage_not_configured")


def _file_out(user: User, files: list[File]) -> list[dict[str, Any]]:
    projects = {
        p.pk: p
        for p in permissions.visible_projects(user).filter(pk__in={f.project_id for f in files})
    }
    return [
        {
            "id": f.pk,
            "name": f.name,
            "media_type": f.media_type,
            "size": f.blob.size,
            "sha256": f.blob.sha256,
            "project": projects.get(f.project_id),
            "created_by": f.created_by,
            "created_at": f.created_at,
            "external": f.blob.registered_external,
        }
        for f in files
    ]


def _upload_out(user: User, upload: Upload) -> dict[str, Any]:
    file = None
    if upload.file_id is not None:
        found = File.objects.select_related("blob", "created_by").get(pk=upload.file_id)
        file = _file_out(user, [found])[0]
    return {
        "id": upload.pk,
        "project": upload.project_id,
        "name": upload.name,
        "media_type": upload.media_type,
        "size": upload.size,
        "sha256": upload.sha256,
        "state": upload.state,
        "part_size": upload.part_size,
        "part_count": storage.part_count(upload.size, upload.part_size),
        "file": file,
        "error": upload.error,
        "created_at": upload.created_at,
        "expires_at": upload.expires_at,
    }


def _upload(user: User, upload_id: UUID) -> Upload:
    """The caller's own upload. Nobody else's is found."""
    found = Upload.objects.filter(pk=upload_id, created_by=user).first()
    if found is None:
        raise Problem(404, "No such upload of yours.")
    return found


def _file(user: User, file_id: UUID) -> File:
    found = services.readable(user).select_related("blob", "created_by").filter(pk=file_id).first()
    if found is None:
        raise Problem(404, "No such file, or you can’t read it.")
    return found


# --- Uploads -------------------------------------------------------------------------------------


@router.post("/uploads", response={201: StartedOut}, summary="Start adding a file")
def start_upload(request: HttpRequest, payload: UploadIn) -> Status[dict[str, Any]]:
    """Answers with the parts to send. An empty file needs none, and is ready at once."""
    me = caller(request)
    target = project(me, payload.project, Action.PROJECT_EDIT)
    _storage()
    try:
        upload = services.start(
            me,
            target,
            name=payload.name,
            media_type=payload.media_type,
            size=payload.size,
            sha256=payload.sha256,
        )
    except Refused as error:
        if error.field is None:
            raise
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": f"#/{error.field}", "detail": str(error)}],
        ) from None
    upload.refresh_from_db()
    parts = services.parts(upload) if upload.state == UploadState.PENDING else []
    return Status(201, {**_upload_out(me, upload), "parts": parts})


@router.get("/uploads", response=Page[UploadOut], summary="Your uploads")
def list_uploads(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first."""
    me = caller(request)
    result = page(Upload.objects.filter(created_by=me), params, newest_first=True)
    result["items"] = [_upload_out(me, u) for u in result["items"]]
    return result


@router.get("/uploads/{upload_id}", response=UploadOut, summary="One of your uploads")
def get_upload(request: HttpRequest, upload_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _upload_out(me, _upload(me, upload_id))


@router.post(
    "/uploads/{upload_id}/parts", response=list[PartOut], summary="Sign an upload's parts again"
)
def sign_parts(request: HttpRequest, upload_id: UUID, payload: PartsIn) -> list[dict[str, Any]]:
    """New URLs, when the earlier ones expired before every part was sent."""
    me = caller(request)
    _storage()
    return services.parts(_upload(me, upload_id), payload.numbers)


@router.post(
    "/uploads/{upload_id}/complete",
    response={202: UploadOut, 200: UploadOut},
    summary="Finish an upload",
)
def complete_upload(request: HttpRequest, upload_id: UUID) -> Status[dict[str, Any]]:
    """Once every part has arrived. Iki then checks the bytes (202), and the upload becomes
    ready, or fails if they don't match its SHA-256. Finishing it again changes nothing."""
    me = caller(request)
    _storage()
    upload = services.complete(_upload(me, upload_id))
    return Status(202 if upload.state == UploadState.CHECKING else 200, _upload_out(me, upload))


@router.delete("/uploads/{upload_id}", response={204: None}, summary="Cancel an upload")
def cancel_upload(request: HttpRequest, upload_id: UUID) -> Status[None]:
    """What of it arrived is deleted. A finished upload stays as it is."""
    me = caller(request)
    _storage()
    services.cancel(_upload(me, upload_id))
    return Status(204, None)


# --- Files ---------------------------------------------------------------------------------------


@router.get("/files", response=Page[FileOut], summary="Files you can read")
def list_files(request: HttpRequest, filters: Query[FileFilters]) -> dict[str, Any]:
    """Oldest first. Files are read through the projects they were added to."""
    me = caller(request)
    files = services.readable(me).select_related("blob", "created_by")
    if filters.project:
        files = files.filter(project=project(me, filters.project))
    result = page(files, filters)
    result["items"] = _file_out(me, result["items"])
    return result


@router.get("/files/{file_id}", response=FileOut, summary="A file")
def get_file(request: HttpRequest, file_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _file_out(me, [_file(me, file_id)])[0]


@router.get("/files/{file_id}/content", response={302: None}, summary="Download a file")
def download_file(request: HttpRequest, file_id: UUID) -> HttpResponse:
    """Redirects to a signed URL that works for five minutes. It downloads the file under
    its name, rather than opening it in the browser."""
    me = caller(request)
    found = _file(me, file_id)
    _storage()
    blob = found.blob
    url = storage.download_url(blob.storage_uri, blob.object_version, found.name, found.media_type)
    response = HttpResponse(status=302)
    response["Location"] = url
    response["Cache-Control"] = "no-store"
    return response

"""Adding files: uploads in parts, checked against their SHA-256, stored once per content.

1. :func:`start` records the upload and opens a multipart upload to a
   staging object. The caller sends each part to a signed URL.
2. :func:`complete` assembles the parts once they've all arrived, then a
   task (:func:`check`) reads the bytes back. If their SHA-256 and size are
   what the caller declared, they move to the content's own key (unless the
   same content is there already) and become a :class:`File`. Otherwise the
   upload fails, and nothing of it is kept.

Every upload sends its bytes, even when Iki already has the same content.
Otherwise anyone could learn whether someone else holds a file, by asking
about its hash.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from typing import Any

import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.core.sizes import human_size
from iki_server.entities import access
from iki_server.files import storage
from iki_server.files.models import (
    MEDIA_TYPE,
    Blob,
    File,
    Registration,
    RegistrationState,
    Upload,
    UploadKind,
    UploadState,
)
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.realtime import events as realtime

logger = structlog.get_logger("iki.files")

#: Unfinished uploads are cancelled this long after they start.
UPLOAD_LIFETIME = timedelta(hours=24)
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_MEDIA_TYPE = re.compile(MEDIA_TYPE)


def clean_name(name: str) -> str:
    """The file's own name: no folders, no control characters, at most 255 characters."""
    name = unicodedata.normalize("NFC", name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C").strip()
    if name in ("", ".", ".."):
        raise Refused("Give the file a name.", field="name")
    if len(name) > 255:
        stem, dot, extension = name.rpartition(".")
        keep = extension if dot and len(extension) <= 16 else ""
        name = (stem if keep else name)[: 255 - len(keep) - (1 if keep else 0)] + (
            f".{keep}" if keep else ""
        )
    return name


def clean_media_type(media_type: str) -> str:
    """``Type/Subtype; charset=…`` → ``type/subtype``."""
    base = media_type.split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE.fullmatch(base):
        raise Refused(
            "That isn't a media type: use one such as chemical/x-pdb or application/json.",
            field="media_type",
        )
    return base


# --- Uploading -----------------------------------------------------------------------------------


def start(
    user: User,
    project: Project,
    *,
    name: str,
    media_type: str,
    size: int,
    sha256: str,
    kind: UploadKind = UploadKind.FILE,
) -> Upload:
    """A new upload. Callers check the user may add files to the project."""
    name, media_type, sha256 = clean_name(name), clean_media_type(media_type), sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise Refused("The SHA-256 must be 64 hexadecimal digits.", field="sha256")
    limit = settings.IKI_FILES_MAX_UPLOAD_BYTES
    if size > limit:
        raise Refused(f"Files can be at most {human_size(limit)} here.", field="size")
    upload = Upload(
        project=project,
        created_by=user,
        name=name,
        media_type=media_type,
        size=size,
        sha256=sha256,
        kind=kind,
        part_size=storage.part_size_for(size),
        expires_at=timezone.now() + UPLOAD_LIFETIME,
    )
    upload.staging_key = storage.staging_key(upload.pk)
    if size == 0:
        # Nothing to send. Its hash must be the empty content's, as for any other upload.
        if sha256 != EMPTY_SHA256:
            raise Refused("An empty file's SHA-256 is e3b0c442…b855.", field="sha256")
        upload.save()
        storage.put_empty(upload.staging_key)
        _finish(upload, sha256, 0)
        return upload
    upload.multipart_id = storage.start_multipart(upload.staging_key)
    upload.save()
    return upload


def parts(
    upload: Upload, numbers: list[int] | None = None, *, workers: bool = False
) -> list[dict[str, Any]]:
    """Signed URLs for the parts still to send (or the ones asked for), with their sizes:
    for a worker, signed for the address workers reach the store at."""
    if upload.state != UploadState.PENDING:
        raise Refused("The upload isn't waiting for parts any more.")
    count = storage.part_count(upload.size, upload.part_size)
    wanted = numbers if numbers is not None else list(range(1, count + 1))
    bad = [n for n in wanted if not 1 <= n <= count]
    if bad:
        raise Refused(f"This upload has parts 1 to {count}.", field="numbers")
    return [
        {
            "number": n,
            "size": _part_size(upload, n, count),
            "url": storage.part_url(upload.staging_key, upload.multipart_id, n, workers=workers),
        }
        for n in sorted(set(wanted))
    ]


def _part_size(upload: Upload, number: int, count: int) -> int:
    return upload.part_size if number < count else upload.size - upload.part_size * (count - 1)


def complete(upload: Upload) -> Upload:
    """Assembles the parts once all have arrived, and has a task check the bytes."""
    with transaction.atomic():
        locked = Upload.objects.select_for_update().get(pk=upload.pk)
        if locked.state != UploadState.PENDING:
            return locked
        count = storage.part_count(locked.size, locked.part_size)
        arrived = storage.uploaded_parts(locked.staging_key, locked.multipart_id)
        missing = [n for n in range(1, count + 1) if n not in arrived]
        if missing:
            raise Refused(f"Some parts haven't arrived yet: {_numbers(missing)}.")
        etags: dict[int, str] = {}
        for n in range(1, count + 1):
            needed = _part_size(locked, n, count)
            etag = next((e for size, e in arrived[n] if size == needed), None)
            if etag is not None:
                etags[n] = etag
        wrong = [n for n in range(1, count + 1) if n not in etags]
        if wrong:
            raise Refused(
                f"Some parts aren't the size this upload needs: {_numbers(wrong)}. Send them again."
            )
        storage.complete_multipart(locked.staging_key, locked.multipart_id, etags)
        locked.state = UploadState.CHECKING
        locked.save(update_fields=["state"])
        from iki_server.files.tasks import check_upload

        # Queued in the same transaction: the check exists exactly when the state does.
        check_upload.enqueue(str(locked.pk))
    return locked


def cancel(upload: Upload) -> Upload:
    with transaction.atomic():
        locked = Upload.objects.select_for_update().get(pk=upload.pk)
        if locked.state not in (UploadState.PENDING, UploadState.CHECKING):
            return locked
        if locked.state == UploadState.PENDING:
            storage.abort_multipart(locked.staging_key, locked.multipart_id)
        else:
            storage.delete(locked.staging_key)
        _end(locked, UploadState.CANCELLED)
    return locked


def check(upload_id: str) -> str:
    """Reads an assembled upload back, and makes it a file if its bytes are as declared."""
    upload = Upload.objects.select_related("created_by").filter(pk=upload_id).first()
    if upload is None or upload.state != UploadState.CHECKING:
        return "nothing to check"
    sha256, size = storage.digest(upload.staging_key)
    if (sha256, size) != (upload.sha256, upload.size):
        storage.delete(upload.staging_key)
        with transaction.atomic():
            locked = Upload.objects.select_for_update().get(pk=upload.pk)
            if locked.state != UploadState.CHECKING:
                return locked.state
            what = "size" if size != upload.size else "SHA-256"
            locked.error = (
                f"The bytes that arrived don't match the {what} you sent, so they weren't kept. "
                "Upload the file again."
            )
            _end(locked, UploadState.FAILED)
        logger.warning("upload_mismatch", upload=upload_id, expected=upload.sha256, got=sha256)
        return "mismatch"
    _finish(upload, sha256, size)
    return "ready"


def _finish(upload: Upload, sha256: str, size: int) -> None:
    """Moves checked bytes to their content's key, and makes the upload a file; or, for an
    artifact, to its own key."""
    if upload.kind == UploadKind.ARTIFACT:
        storage.copy(upload.staging_key, storage.artifact_key(upload.pk))
        storage.delete(upload.staging_key)
        with transaction.atomic():
            locked = Upload.objects.select_for_update().get(pk=upload.pk)
            if locked.state in (UploadState.PENDING, UploadState.CHECKING):
                _end(locked, UploadState.READY)
        return
    key = storage.blob_key(sha256)
    if not storage.exists(key):
        storage.copy(upload.staging_key, key)
    storage.delete(upload.staging_key)
    with transaction.atomic():
        locked = Upload.objects.select_for_update().get(pk=upload.pk)
        if locked.state not in (UploadState.PENDING, UploadState.CHECKING):
            return
        blob = _blob(sha256, size, storage.uri(key))
        file = File.objects.create(
            blob=blob,
            name=locked.name,
            media_type=locked.media_type,
            project_id=locked.project_id,
            created_by_id=locked.created_by_id,
        )
        locked.file = file
        _end(locked, UploadState.READY)
        log.record(
            "file.added",
            actor=locked.created_by,
            target=file,
            project=locked.project,
            data={"sha256": sha256, "size": size, "media_type": file.media_type},
        )


def _blob(sha256: str, size: int, uri: str) -> Blob:
    try:
        with transaction.atomic():
            blob, _ = Blob.objects.get_or_create(
                sha256=sha256,
                registered_external=False,
                defaults={"size": size, "storage_uri": uri},
            )
    except IntegrityError:  # someone else stored the same content just now
        blob = Blob.objects.get(sha256=sha256, registered_external=False)
    return blob


def _end(upload: Upload, state: UploadState) -> None:
    upload.state = state
    upload.finished_at = timezone.now()
    upload.save(update_fields=["state", "finished_at", "error", "file"])
    if upload.kind == UploadKind.ARTIFACT:
        return  # a worker's, for a run: the run's page says how it went
    # Postgres delivers it when the transaction commits.
    realtime.publish(
        upload.created_by_id,
        "upload.finished",
        {"upload": str(upload.pk), "state": str(state), "project": str(upload.project_id)},
    )


def expire() -> int:
    """Cancels uploads left unfinished past their time. Returns how many."""
    stale = Upload.objects.filter(
        state__in=[UploadState.PENDING, UploadState.CHECKING], expires_at__lt=timezone.now()
    )
    count = 0
    for upload in stale:
        with transaction.atomic():
            locked = Upload.objects.select_for_update(skip_locked=True).filter(pk=upload.pk).first()
            if locked is None or locked.state not in (UploadState.PENDING, UploadState.CHECKING):
                continue
            if locked.multipart_id:
                storage.abort_multipart(locked.staging_key, locked.multipart_id)
            if storage.exists(locked.staging_key):
                storage.delete(locked.staging_key)
            locked.error = "It wasn't finished in time."
            _end(locked, UploadState.EXPIRED)
            count += 1
    return count


# --- Registering in place ------------------------------------------------------------------------
# Files already in an approved bucket (a lab's instrument output, say) are
# registered where they are, pinned to one version of their object, rather
# than copied. Iki still reads the bytes once, to learn their SHA-256.

#: Registrations still being read after this long fail, so nobody waits forever.
REGISTRATION_LIFETIME = timedelta(hours=24)


def locations() -> list[str]:
    """Where files may be registered: ``s3://bucket/prefix/`` (see settings)."""
    return list(settings.IKI_FILES_REGISTER_LOCATIONS)


def register(
    user: User,
    project: Project,
    *,
    uri: str,
    version: str = "",
    name: str = "",
    media_type: str = "",
) -> Registration:
    """Registers one version of an object. Callers check the user may add files to the project.

    Without a ``version``, the object's current version is pinned.
    """
    uri = uri.strip()
    bucket_name, key = storage.split_uri(uri)
    if not uri.startswith("s3://") or not bucket_name or not key or key.endswith("/"):
        raise Refused("Name an object, such as s3://lab-data/runs/plate-7.csv.", field="uri")
    if ".." in key.split("/") or not any(uri.startswith(place) for place in locations()):
        places = ", ".join(locations()) or "none are set up"
        raise Refused(
            f"Files can be registered only from the locations your administrators approved "
            f"({places}). Upload this file instead, or ask them to add its location.",
            field="uri",
        )
    if not storage.versioned(bucket_name):
        raise Refused(
            f"Bucket {bucket_name} doesn't keep versions, so this file could change after "
            "it's registered. Turn on versioning for the bucket, or upload the file instead.",
            field="uri",
        )
    found = storage.head(bucket_name, key, version.strip())
    if found is None:
        what = f"version {version.strip()} of {uri}" if version.strip() else uri
        raise Refused(f"There's no object at {what}.", field="version" if version else "uri")
    pinned = found["version"]
    if pinned in ("", "null"):
        raise Refused(
            "This object was stored before the bucket kept versions, so it could still be "
            "overwritten. Copy it onto itself to give it a version, then register it.",
            field="uri",
        )
    name = clean_name(name or key.rsplit("/", 1)[-1])
    if media_type:
        media_type = clean_media_type(media_type)
    else:
        try:
            media_type = clean_media_type(found["media_type"])
        except Refused:
            media_type = "application/octet-stream"
    with transaction.atomic():
        registration = Registration.objects.create(
            project=project,
            created_by=user,
            name=name,
            media_type=media_type,
            storage_uri=uri,
            object_version=pinned,
            size=found["size"],
            expires_at=timezone.now() + REGISTRATION_LIFETIME,
        )
        blob = Blob.objects.filter(
            registered_external=True, storage_uri=uri, object_version=pinned
        ).first()
        if blob is not None:
            # Registered before: its hash is known, so it's a file at once.
            _registered(registration, blob)
        else:
            from iki_server.files.tasks import check_registration

            check_registration.enqueue(str(registration.pk))
    return registration


def check_registration(registration_id: str) -> str:
    """Reads a registered object's version, and makes it a file with the SHA-256 it found."""
    registration = Registration.objects.filter(pk=registration_id).first()
    if registration is None or registration.state != RegistrationState.CHECKING:
        return "nothing to check"
    bucket_name, key = storage.split_uri(registration.storage_uri)
    sha256, size = storage.digest_object(bucket_name, key, registration.object_version)
    with transaction.atomic():
        locked = Registration.objects.select_for_update().get(pk=registration.pk)
        if locked.state != RegistrationState.CHECKING:
            return str(locked.state)
        if size != locked.size:
            # A pinned version can't change; a store that says otherwise isn't trusted.
            locked.error = "The object's size changed while Iki read it, so it wasn't registered."
            _end_registration(locked, RegistrationState.FAILED)
            return "mismatch"
        try:
            with transaction.atomic():
                blob, _ = Blob.objects.get_or_create(
                    registered_external=True,
                    storage_uri=locked.storage_uri,
                    object_version=locked.object_version,
                    defaults={"sha256": sha256, "size": size},
                )
        except IntegrityError:  # registered by someone else just now
            blob = Blob.objects.get(
                registered_external=True,
                storage_uri=locked.storage_uri,
                object_version=locked.object_version,
            )
        _registered(locked, blob)
    return "ready"


def _registered(registration: Registration, blob: Blob) -> None:
    file = File.objects.create(
        blob=blob,
        name=registration.name,
        media_type=registration.media_type,
        project_id=registration.project_id,
        created_by_id=registration.created_by_id,
    )
    registration.file = file
    _end_registration(registration, RegistrationState.READY)
    log.record(
        "file.registered",
        actor=registration.created_by,
        target=file,
        project=registration.project,
        data={
            "uri": blob.storage_uri,
            "version": blob.object_version,
            "sha256": blob.sha256,
            "size": blob.size,
            "media_type": file.media_type,
        },
    )


def _end_registration(registration: Registration, state: RegistrationState) -> None:
    registration.state = state
    registration.finished_at = timezone.now()
    registration.save(update_fields=["state", "finished_at", "error", "file"])
    realtime.publish(
        registration.created_by_id,
        "registration.finished",
        {
            "registration": str(registration.pk),
            "state": str(state),
            "project": str(registration.project_id),
        },
    )


def expire_registrations() -> int:
    """Fails registrations still being read past their time. Returns how many."""
    stale = Registration.objects.filter(
        state=RegistrationState.CHECKING, expires_at__lt=timezone.now()
    )
    count = 0
    for registration in stale:
        with transaction.atomic():
            locked = (
                Registration.objects.select_for_update(skip_locked=True)
                .filter(pk=registration.pk, state=RegistrationState.CHECKING)
                .first()
            )
            if locked is None:
                continue
            locked.error = "Iki couldn't read the object in time. Register it again."
            _end_registration(locked, RegistrationState.FAILED)
            count += 1
    return count


# --- Reading -------------------------------------------------------------------------------------


def readable(user: Any) -> QuerySet[File]:  # noqa: ANN401 - User or AnonymousUser
    """Files the user can read (see :func:`iki_server.entities.access.readable_files`)."""
    return access.readable_files(user)


def _numbers(numbers: list[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:10])
    return shown + (f" and {len(numbers) - 10} more" if len(numbers) > 10 else "")

"""Adding files: uploads in parts, checked against their SHA-256, stored once per content.

1. :func:`start` records the upload and opens a multipart upload to a
   staging object. The caller sends each part to a signed URL.
2. :func:`complete` assembles the parts once they've all arrived, then a
   task (:func:`check`) reads the bytes back. If their SHA-256 and size are
   what the caller declared, they move to the content's own key (unless the
   same content is there already). The task scans the retained object before it
   parses metadata and records the :class:`File`. A held file has metadata only.
   An integrity mismatch fails the upload, and nothing of it is kept.

Every upload sends its bytes, even when Iki already has the same content.
Otherwise anyone could learn whether someone else holds a file, by asking
about its hash.
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, NamedTuple

import structlog
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.core.sizes import human_size
from iki_server.entities import access, registry
from iki_server.files import acquired, scan_state, scanner, storage
from iki_server.files import metadata as stated
from iki_server.files.models import (
    ACQUIRED,
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
#: A media type's top-level kind, such as image: ``image/*`` is any of its types.
MEDIA_KIND = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")
_ACQUIRED = re.compile(ACQUIRED)
_OCTET_STREAM = "application/octet-stream"
_STANDARD_TYPES = mimetypes.MimeTypes()
#: How a file's acquisition is known when whoever added it said, rather than its format.
GIVEN = "given"


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


def clean_acquired(text: str) -> str:
    """When a file's data was acquired, as someone gives it: ``2026-03-14``, or with a time
    (``2026-03-14T09:21``, seconds and an offset if they know them), in ISO 8601. A real
    day, not after today. "" for none."""
    text = text.strip().replace(" ", "T", 1)
    if not text:
        return ""
    problem = Refused(
        "Give when it was acquired as a date, such as 2026-03-14, or a date and time, such "
        "as 2026-03-14T09:21.",
        field="acquired_at",
    )
    if not _ACQUIRED.fullmatch(text):
        raise problem
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise problem from None
    if moment.year < 1900 or moment.date() > timezone.now().date() + timedelta(days=1):
        raise Refused(
            "That's not a day its data could have been acquired: check the date.",
            field="acquired_at",
        )
    return text


class _Stored:
    """A stored object for bounded signature and metadata reads, pinned for registrations."""

    def __init__(self, uri: str, version: str, size: int) -> None:
        self.uri, self.version, self._size = uri, version, size

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self._size or length <= 0:
            return b""
        return storage.read_range(self.uri, self.version, offset, min(length, self._size - offset))


def _recognize(name: str, media_type: str, source: acquired.Readable) -> str:
    """Verify the declaration against at most the registry's bounded signature prefix."""
    formats = registry.file_formats()
    length = min(formats.signature_bytes, source.size)
    prefix = source.read(0, length) if length else b""
    if len(prefix) != length:
        raise OSError("The store did not return the complete file prefix.")
    try:
        match = formats.recognize(name, prefix, media_type)
    except ValueError as error:
        raise Refused(str(error), field="media_type") from None
    if match is not None:
        media_type = match.canonical
    elif media_type == _OCTET_STREAM:
        media_type = _STANDARD_TYPES.guess_type(f"./{name}")[0] or _OCTET_STREAM
    return media_type


def _stated(metadata: acquired.FileMetadata) -> dict[str, Any]:
    """What the file says about where its data came from (ADR 0224), as a file keeps it."""
    facts = stated.stored(metadata.facts)
    groups = [dict(group) for group in metadata.read_groups]
    return {
        "facts": facts,
        "read_groups": groups,
        "stated_text": stated.search_text(facts, groups),
    }


def _upload_media_type(upload: Upload) -> str:
    if upload.kind not in (UploadKind.FILE, UploadKind.ARTIFACT):
        return upload.media_type
    source = _Stored(storage.uri(upload.staging_key), "", upload.size)
    return _recognize(upload.name, upload.media_type, source)


def _registration_media_type(registration: Registration) -> str:
    source = _Stored(registration.storage_uri, registration.object_version, registration.size)
    return _recognize(registration.name, registration.media_type, source)


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
    acquired_at: str = "",
) -> Upload:
    """A new upload. Callers check the user may add files to the project."""
    name, media_type, sha256 = clean_name(name), clean_media_type(media_type), sha256.lower()
    acquired_at = clean_acquired(acquired_at)
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise Refused("The SHA-256 must be 64 hexadecimal digits.", field="sha256")
    if kind == UploadKind.BUNDLE:
        limit = settings.IKI_BUNDLE_MAX_BYTES
        if size > limit:
            raise Refused(f"Bundles can be at most {human_size(limit)} here.", field="size")
    else:
        limit = settings.IKI_FILES_MAX_UPLOAD_BYTES
        if size > limit:
            raise Refused(f"Files can be at most {human_size(limit)} here.", field="size")
    upload = Upload(
        project=project,
        created_by=user,
        agent=log.current_agent(),
        name=name,
        media_type=media_type,
        acquired_at=acquired_at,
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
        from iki_server.files.tasks import check_upload

        upload.media_type = _upload_media_type(upload)
        permanent = kind == UploadKind.FILE
        key = storage.blob_key(sha256) if permanent else storage.kept_key(kind, upload.pk)
        if not storage.exists(key):
            storage.put_empty(key)
        with transaction.atomic():
            if permanent:
                _blob(sha256, 0, storage.uri(key))
            upload.state = UploadState.SCANNING
            upload.save()
            check_upload.enqueue(str(upload.pk))
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
    chosen = sorted(set(wanted))
    urls = storage.part_urls(upload.staging_key, upload.multipart_id, chosen, workers=workers)
    return [{"number": n, "size": _part_size(upload, n, count), "url": urls[n]} for n in chosen]


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
        if locked.state not in (UploadState.PENDING, UploadState.CHECKING, UploadState.SCANNING):
            return locked
        if locked.state == UploadState.PENDING:
            storage.abort_multipart(locked.staging_key, locked.multipart_id)
        else:
            storage.delete(locked.staging_key)
            if locked.kind != UploadKind.FILE:
                storage.delete_for_good(storage.kept_key(locked.kind, locked.pk))
        _end(locked, UploadState.CANCELLED)
    return locked


@contextmanager
def _scan_lock(source: Blob | Upload) -> Iterator[None]:
    """At most one scan streams a source at a time, without a database transaction."""
    lock = f"iki.scan:{source._meta.db_table}:{source.pk}"
    with connection.cursor() as cursor:
        physical = connection.connection
        reusable = False
        try:
            cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [lock])
            locked = cursor.fetchone()
            if not locked or not locked[0]:
                from iki_server.tasks.backend import Retry

                reusable = True
                raise Retry("Another task is scanning this object.")
            try:
                yield
            finally:
                cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [lock])
                reusable = True
        finally:
            if not reusable:
                # A failed operation can leave ownership unknown. Disconnect the physical
                # session so a lock cannot follow a connection back into Django's pool.
                if physical is not None:
                    physical.close()
                connection.close()


def _pinned_source(blob: Blob) -> bool:
    """Whether the configured store can read this recorded, immutable object location."""
    # Cloud Storage also reads its records from the S3-compatible API (ADR 0109).
    google = storage.scheme() == "gs"
    schemes = ("gs://", "s3://") if google else ("s3://",)
    version = blob.object_version
    bucket, key = storage.split_uri(blob.storage_uri)
    # Approving the prefix applies to registration; earlier pinned records remain
    # available after the reader's administrator removes it from the allowlist.
    return (
        version.strip() not in ("", "null")
        and (not google or (version.isascii() and version.isdecimal()))
        and blob.storage_uri.startswith(schemes)
        and bool(bucket and key)
        and not key.endswith("/")
        and ".." not in key.split("/")
    )


def scan_source(source: Blob | Upload) -> scan_state.ScanStatus:
    """Scan a verified immutable object once. An unavailable scanner leaves it pending."""
    status = scan_state.status_for(source)
    if status.state != "pending":
        return status
    with _scan_lock(source):
        status = scan_state.status_for(source)
        if status.state != "pending":
            return status
        if isinstance(source, Blob):
            uri, version = source.storage_uri, source.object_version
            if source.registered_external and not _pinned_source(source):
                return scan_state.record_result(
                    source,
                    state="not_scanned",
                    scanner=settings.IKI_FILES_SCANNER,
                    reason="unsupported_source",
                )
        else:
            uri, version = storage.uri(storage.kept_key(source.kind, source.pk)), ""
        result = scanner.scan_object(
            storage_uri=uri, version=version, sha256=source.sha256, size=source.size
        )
        with transaction.atomic():
            decision = scan_state.record_result(
                source,
                state=result.status,
                scanner=result.scanner,
                signature_version=result.signature_version,
                reason=result.reason,
                allow_unscanned=result.allow_unscanned,
            )
            if isinstance(source, Upload) and source.state == UploadState.READY and decision.usable:
                scan_released(source)
        return decision


def check(upload_id: str) -> str:
    """Verify staged bytes, then scan the retained object. Either step can be retried."""
    upload = Upload.objects.filter(pk=upload_id).first()
    if upload is None or upload.state not in (UploadState.CHECKING, UploadState.SCANNING):
        return "nothing to check"
    if upload.state == UploadState.CHECKING:
        # Concurrent deliveries may have read the same state. Only one verifies and
        # promotes the bytes; a late delivery must never resurrect an expired artifact.
        with _scan_lock(upload):
            if Upload.objects.filter(pk=upload.pk, state=UploadState.CHECKING).exists():
                _verify(upload)
        upload.refresh_from_db()
        if upload.state == UploadState.FAILED:
            return "mismatch"
    if upload.state != UploadState.SCANNING:
        return str(upload.state)
    source = (
        Blob.objects.get(sha256=upload.sha256, registered_external=False)
        if upload.kind == UploadKind.FILE
        else upload
    )
    status = scan_source(source)
    # Only a permitted object's format may be parsed. A held file keeps just the
    # acquisition information supplied by its uploader.
    facts = None
    if isinstance(source, Blob):
        facts = _file_facts(upload, source, status)
    with transaction.atomic():
        locked = Upload.objects.select_for_update().get(pk=upload.pk)
        if locked.state != UploadState.SCANNING:
            return str(locked.state)
        if isinstance(source, Blob) and facts is not None:
            _add_file(locked, source, facts)
        _end(locked, UploadState.READY)
        if locked.kind == UploadKind.BUNDLE and scan_state.status_for(locked).usable:
            from iki_server.bundles import imports

            imports.uploaded(locked)
    return "ready"


def _verify(upload: Upload) -> None:
    sha256, size = storage.digest(upload.staging_key)
    if (sha256, size) != (upload.sha256, upload.size):
        what = "size" if size != upload.size else "SHA-256"
        logger.warning("upload_mismatch", upload=str(upload.pk), expected=upload.sha256, got=sha256)
        _fail_upload(
            upload,
            f"The bytes that arrived don't match the {what} you sent, so they weren't kept. "
            "Upload the file again.",
        )
        return
    _finish(upload, sha256, size)


def _fail_upload(upload: Upload, reason: str) -> None:
    with transaction.atomic():
        locked = Upload.objects.select_for_update().get(pk=upload.pk)
        if locked.state == UploadState.CHECKING:
            locked.error = reason
            _end(locked, UploadState.FAILED)


def _finish(upload: Upload, sha256: str, size: int) -> None:
    """Keep the verified input and move it to scanning. A stale finisher copies nothing."""
    if not Upload.objects.filter(pk=upload.pk, state=UploadState.CHECKING).exists():
        return
    try:
        media_type = _upload_media_type(upload)
    except Refused as error:
        _fail_upload(upload, str(error))
        return
    permanent = upload.kind == UploadKind.FILE
    key = storage.blob_key(sha256) if permanent else storage.kept_key(upload.kind, upload.pk)
    # A retry can reuse the verified copy. The staged input stays until _end's cleanup
    # task sees a committed finish, even if an enclosing transaction rolls back.
    if not storage.exists(key):
        storage.copy(upload.staging_key, key)
    with transaction.atomic():
        locked = Upload.objects.select_for_update().get(pk=upload.pk)
        if locked.state == UploadState.CHECKING:
            if permanent:
                _blob(sha256, size, storage.uri(key))
            locked.media_type = media_type
            locked.state = UploadState.SCANNING
            locked.save(update_fields=["media_type", "state"])
        elif not permanent and locked.state in (UploadState.CANCELLED, UploadState.EXPIRED):
            # Its cancellation may have deleted the object while our copy was in flight.
            storage.delete_for_good(key)


class _FileFacts(NamedTuple):
    """When a file's data was acquired, and what it states about itself (ADR 0224)."""

    acquired_on: dt.date | None
    acquired_at: str
    acquired_from: str
    stated: dict[str, Any]


def _file_facts(
    added: Registration | Upload, blob: Blob, status: scan_state.ScanStatus
) -> _FileFacts:
    """The format's own date wins, followed by the date given when the file was added. Only
    a permitted object's bytes are parsed: a held file keeps just what its uploader gave."""
    metadata = acquired.FileMetadata()
    if status.usable and blob.size:
        fmt = registry.file_formats().by_media_type(added.media_type)
        if fmt is not None and fmt.reader is not None:
            source = _Stored(blob.storage_uri, blob.object_version, blob.size)
            metadata = acquired.read_metadata(fmt.reader, source)
    if metadata.acquired is not None:
        found = metadata.acquired
        return _FileFacts(found.on, found.at, found.source, _stated(metadata))
    given = added.acquired_at
    on = dt.date.fromisoformat(given[:10]) if given else None
    return _FileFacts(on, given, GIVEN if given else "", _stated(metadata))


def _add_file(upload: Upload, blob: Blob, read: _FileFacts) -> None:
    file = File.objects.create(
        blob=blob,
        name=upload.name,
        media_type=upload.media_type,
        project_id=upload.project_id,
        created_by_id=upload.created_by_id,
        agent_id=upload.agent_id,
        acquired_on=read.acquired_on,
        acquired_at=read.acquired_at,
        acquired_from=read.acquired_from,
        **read.stated,
    )
    upload.file = file
    # A run's worker uploaded it: the run made it (ADR 0177).
    from iki_server.runs.models import RunFile, RunFileRole, RunUpload

    made = RunUpload.objects.filter(upload=upload).values_list("run_id", flat=True).first()
    if made is not None:
        RunFile.objects.create(run_id=made, file=file, role=RunFileRole.MADE)
    log.record(
        "file.added",
        actor=upload.created_by,
        agent=upload.agent,
        target=file,
        project=upload.project,
        data={"sha256": blob.sha256, "size": blob.size, "media_type": file.media_type},
    )


def scan_released(source: Upload) -> None:
    """Resume a held archive that was waiting to be opened. Called in the release transaction."""
    if source.kind == UploadKind.BUNDLE:
        from iki_server.bundles import imports

        locked = Upload.objects.select_for_update().get(pk=source.pk)
        if locked.state == UploadState.READY:
            imports.uploaded(locked)


def add_written(
    path: Any,  # noqa: ANN401 - a Path
    *,
    name: str,
    media_type: str,
    project: Project,
    user: User,
    sha256: str,
    agent: Any = None,  # noqa: ANN401 - an Agent
) -> File:
    """A file Iki wrote itself, for ``user`` (a spreadsheet's rows as it read them, for the
    run that imports them, ADR 0178): stored once per content as an upload's bytes are, and
    audited as adding one is. ``sha256`` is what Iki worked out as it wrote it. Call outside
    a transaction: storing it may take a while."""
    size = path.stat().st_size
    key = storage.blob_key(sha256)
    if not storage.exists(key):
        storage.upload_file(path, key, "application/octet-stream")
    blob = _blob(sha256, size, storage.uri(key))
    scan_source(blob)
    with transaction.atomic():
        file = File.objects.create(
            blob=blob,
            name=clean_name(name),
            media_type=clean_media_type(media_type),
            project=project,
            created_by=user,
            agent=agent,
        )
        log.record(
            "file.added",
            actor=user,
            agent=agent,
            target=file,
            project=project,
            data={"sha256": sha256, "size": size, "media_type": file.media_type, "by": "iki"},
        )
    return file


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
    upload.save(update_fields=["state", "finished_at", "error", "file", "media_type"])
    if state in (UploadState.READY, UploadState.FAILED):
        from iki_server.files.tasks import discard_staging

        # The runner sees this deletion only after the enclosing transaction commits.
        # A rolled-back finish can read its input again, even if the final copy exists.
        discard_staging.enqueue(str(upload.pk))
    if upload.kind != UploadKind.FILE:
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
            if locked.state == UploadState.CHECKING and locked.kind != UploadKind.FILE:
                storage.delete_for_good(storage.kept_key(locked.kind, locked.pk))
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
    """Where files may be registered: ``s3://bucket/prefix/``, or ``gs://`` (see settings)."""
    return list(settings.IKI_FILES_REGISTER_LOCATIONS)


def registration_location(uri: str) -> tuple[str, str]:
    """The bucket and key of an object the deployment allows registering."""
    bucket_name, key = storage.split_uri(uri)
    scheme = storage.scheme()
    if not uri.startswith(f"{scheme}://") or not bucket_name or not key or key.endswith("/"):
        raise Refused(f"Name an object, such as {scheme}://lab-data/runs/plate-7.csv.", field="uri")
    if ".." in key.split("/") or not any(uri.startswith(place) for place in locations()):
        places = ", ".join(locations()) or "none are set up"
        raise Refused(
            f"Files can be registered only from the locations your administrators approved "
            f"({places}). Upload this file instead, or ask them to add its location.",
            field="uri",
        )
    return bucket_name, key


def registration_object(uri: str, version: str) -> dict[str, Any]:
    """Resolve an approved object to an immutable version and its storage metadata."""
    bucket_name, key = registration_location(uri)
    if not storage.versioned(bucket_name):
        raise Refused(
            f"Bucket {bucket_name} doesn't keep versions, so this file could change after "
            "it's registered. Turn on versioning for the bucket, or upload the file instead.",
            field="uri",
        )
    found = storage.head(bucket_name, key, version)
    if found is None:
        what = f"version {version} of {uri}" if version else uri
        raise Refused(f"There's no object at {what}.", field="version" if version else "uri")
    pinned = found["version"]
    if pinned in ("", "null"):
        raise Refused(
            "This object was stored before the bucket kept versions, so it could still be "
            "overwritten. Copy it onto itself to give it a version, then register it.",
            field="uri",
        )
    if version and pinned != version:
        raise Refused(
            "The object store returned a different version from the one requested.", field="version"
        )
    return found


def register(
    user: User,
    project: Project,
    *,
    uri: str,
    version: str = "",
    name: str = "",
    media_type: str = "",
    acquired_at: str = "",
) -> Registration:
    """Registers one version of an object. Callers check the user may add files to the project.

    Without a ``version``, the object's current version is pinned.
    """
    uri = uri.strip()
    acquired_at = clean_acquired(acquired_at)
    found = registration_object(uri, version.strip())
    pinned = found["version"]
    _, key = storage.split_uri(uri)
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
            agent=log.current_agent(),
            name=name,
            media_type=media_type,
            acquired_at=acquired_at,
            storage_uri=uri,
            object_version=pinned,
            size=found["size"],
            expires_at=timezone.now() + REGISTRATION_LIFETIME,
        )
        blob = Blob.objects.filter(
            registered_external=True, storage_uri=uri, object_version=pinned
        ).first()
        if blob is not None:
            # A known version saves a hash/scan, but each alias has its own declaration.
            registration.media_type = _registration_media_type(registration)
            status = scan_state.status_for(blob)
            if status.state != "pending":
                _registered(registration, blob, _file_facts(registration, blob, status))
                return registration
            registration.state = RegistrationState.SCANNING
            registration.save(update_fields=["state", "media_type"])
        from iki_server.files.tasks import check_registration

        check_registration.enqueue(str(registration.pk))
    return registration


def check_registration(registration_id: str) -> str:
    """Verify the pinned version, then scan it before parsing acquisition information."""
    registration = Registration.objects.filter(pk=registration_id).first()
    if registration is None or registration.state not in (
        RegistrationState.CHECKING,
        RegistrationState.SCANNING,
    ):
        return "nothing to check"
    identity = {
        "storage_uri": registration.storage_uri,
        "object_version": registration.object_version,
    }
    if registration.state == RegistrationState.CHECKING:
        bucket_name, key = storage.split_uri(registration.storage_uri)
        sha256, size = storage.digest_object(bucket_name, key, registration.object_version)
        reason = ""
        if size != registration.size:
            reason = "The object's size changed while Iki read it, so it wasn't registered."
        else:
            try:
                registration.media_type = _registration_media_type(registration)
            except Refused as error:
                reason = str(error)
        with transaction.atomic():
            locked = Registration.objects.select_for_update().get(pk=registration.pk)
            if locked.state != RegistrationState.CHECKING:
                return str(locked.state)
            if reason:
                locked.error = reason
                _end_registration(locked, RegistrationState.FAILED)
                return "mismatch"
            blob, _ = Blob.objects.get_or_create(
                registered_external=True, **identity, defaults={"sha256": sha256, "size": size}
            )
            locked.media_type = registration.media_type
            locked.state = RegistrationState.SCANNING
            locked.save(update_fields=["state", "media_type"])
    else:
        blob = Blob.objects.get(registered_external=True, **identity)
    status = scan_source(blob)
    facts = _file_facts(registration, blob, status)
    with transaction.atomic():
        locked = Registration.objects.select_for_update().get(pk=registration.pk)
        if locked.state != RegistrationState.SCANNING:
            return str(locked.state)
        _registered(locked, blob, facts)
    return "ready"


def _registered(registration: Registration, blob: Blob, read: _FileFacts) -> None:
    file = File.objects.create(
        blob=blob,
        name=registration.name,
        media_type=registration.media_type,
        project_id=registration.project_id,
        created_by_id=registration.created_by_id,
        agent_id=registration.agent_id,
        acquired_on=read.acquired_on,
        acquired_at=read.acquired_at,
        acquired_from=read.acquired_from,
        **read.stated,
    )
    registration.file = file
    _end_registration(registration, RegistrationState.READY)
    log.record(
        "file.registered",
        actor=registration.created_by,
        agent=registration.agent,
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
    registration.save(update_fields=["state", "finished_at", "error", "file", "media_type"])
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

"""Multipart uploads and verification of newly added files (ADR 0036)."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from pathlib import Path
from tempfile import TemporaryFile
from threading import Lock
from typing import BinaryIO, Literal, cast

import httpx2

from iki.client import Client, _path, _retry_after
from iki.errors import Forbidden, IkiError, TimedOut
from iki.schema import FileOut, PartOut, RegistrationStatusOut, StartedOut, UploadStatusOut

_CHUNK = 1024 * 1024


def upload(
    client: Client,
    source: str | os.PathLike[str] | BinaryIO,
    project_id: str,
    name: str | None,
    media_type: str | None,
    timeout: float | None,
    poll: float,
    acquired_at: str = "",
) -> FileOut:
    if poll < 0 or (timeout is not None and timeout < 0):
        raise ValueError("poll and timeout must be nonnegative.")
    with ExitStack() as stack:
        if isinstance(source, str | os.PathLike):
            source = stack.enter_context(Path(source).open("rb"))
        if name is None:
            filename = getattr(source, "name", None)
            if not isinstance(filename, str | os.PathLike):
                raise IkiError("Give the upload a filename with name=.")
            name = Path(filename).name

        # The hash goes in the start request. Only a stream we can't rewind needs a copy.
        spool = None if source.seekable() else stack.enter_context(TemporaryFile("w+b"))
        start = source.tell() if spool is None else 0
        if spool is None:
            stack.callback(source.seek, start)
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            if spool is not None:
                spool.write(chunk)
        sha256 = digest.hexdigest()

        try:
            started = cast(
                StartedOut,
                client.request(
                    "POST",
                    "/uploads",
                    json={
                        "project": project_id,
                        "name": name,
                        "media_type": media_type or "application/octet-stream",
                        "size": size,
                        "sha256": sha256,
                        **({"acquired_at": acquired_at} if acquired_at else {}),
                    },
                ),
            )
        except Forbidden as error:
            raise Forbidden(
                error.status,
                f"Uploading to project {project_id} needs Editor access and the write scope. "
                + error.detail,
                code=error.code,
                errors=error.errors,
                request_id=error.request_id,
                problem=error.problem,
            ) from None

        path = _path("/uploads/{id}", id=started["id"])
        status: UploadStatusOut = started
        if started["state"] == "pending":
            try:
                _send_parts(client, path, started, spool or source, start)
            except BaseException:
                # Release the parts that arrived, including when a notebook interrupts a PUT.
                with suppress(IkiError, httpx2.HTTPError):
                    client.request("DELETE", path)
                raise
            status = cast(UploadStatusOut, client.request("POST", path + "/complete"))

    file = wait_for_file(client, path, status, name, "upload", timeout, poll)
    if (file["sha256"], file["size"]) != (sha256, size):
        raise IkiError(f"The stored SHA-256 or size of {name} differs from the file you sent.")
    return file


def wait_for_file(
    client: Client,
    path: str,
    status: UploadStatusOut | RegistrationStatusOut,
    name: str,
    operation: Literal["upload", "registration"],
    timeout: float | None,
    poll: float,
) -> FileOut:
    """Wait until Iki has checked the bytes and issued a file record."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while status["state"] in ("pending", "checking"):
        delay = poll
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimedOut(f"Iki is still checking {name} after {timeout} s. See {path}.")
            delay = min(delay, remaining)
        client._sleep(delay)
        status = cast(UploadStatusOut | RegistrationStatusOut, client.request("GET", path))

    if status["state"] != "ready":
        reason = status["error"] or "Iki did not issue a file record."
        raise IkiError(f"The {operation} of {name} ended in state {status['state']}. {reason}")
    file = status["file"]
    if file is None:
        raise IkiError(
            f"The {operation} of {name} finished. You no longer have access to the file."
        )
    return file


def _send_parts(
    client: Client, path: str, upload: StartedOut, source: BinaryIO, start: int
) -> None:
    # Workers share the stream. Hold its lock only while reading a chunk, so PUTs can overlap
    # without holding whole parts in memory. Seeking also lets a retry replay just that part.
    reader = Lock()
    signer = Lock()

    def chunks(part: PartOut) -> Iterator[bytes]:
        offset = start + (part["number"] - 1) * upload["part_size"]
        remaining = part["size"]
        while remaining:
            with reader:
                source.seek(offset)
                chunk = source.read(min(remaining, _CHUNK))
            if not chunk:
                raise IkiError("The file became shorter while it was being uploaded.")
            yield chunk
            offset += len(chunk)
            remaining -= len(chunk)

    def send(part: PartOut) -> None:
        number, url = part["number"], part["url"]
        for attempt in range(4):
            # A bare request keeps Iki's token, cookies and agent headers off the signed URL.
            request = httpx2.Request(
                "PUT", url, headers={"Content-Length": str(part["size"])}, content=chunks(part)
            )
            try:
                response = client._http.send(request)
            except httpx2.TransportError:
                if attempt == 3:
                    raise IkiError(
                        f"Could not reach the file store to send part {number}."
                    ) from None
                client._sleep(2.0**attempt)
                continue
            if response.is_success:
                return
            code = response.status_code
            if attempt == 3 or (code not in (403, 408, 429) and code < 500):
                raise IkiError(f"The file store refused upload part {number} (HTTP {code}).")
            if code == 403:
                # A slow transfer can outlive a signature. Serialize authenticated requests
                # so concurrent retries can't both renew the same OAuth refresh token.
                with signer:
                    fresh = cast(
                        list[PartOut],
                        client.request("POST", path + "/parts", json={"numbers": [number]}),
                    )
                url = fresh[0]["url"]
            client._sleep(_retry_after(response, attempt))

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(send, upload["parts"]))

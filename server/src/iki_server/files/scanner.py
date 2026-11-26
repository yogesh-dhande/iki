"""The deployment's malware scanner and the default clamd adapter (ADR 0216)."""

from __future__ import annotations

import hashlib
import re
import socket
from contextlib import closing
from dataclasses import dataclass
from typing import Literal, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

from iki_server.files import storage

_CHUNK = 256 * 1024
_MAX_REPLY = 4096


class ScanStream(Protocol):
    """An object pinned by Iki. Read sequentially in bounded chunks until EOF."""

    def read(self, size: int, /) -> bytes: ...


@dataclass(frozen=True)
class ScanResult:
    status: Literal["clean", "infected", "not_scanned"]
    scanner: str
    signature_version: str
    reason: str = ""
    allow_unscanned: bool = False


class ScannerError(Exception):
    """The attempt produced no reliable verdict. Leave the scan pending."""


class _CheckedStream:
    def __init__(self, stream: ScanStream, size: int) -> None:
        self.source = stream
        self.size = size
        self.count = 0
        self.digest = hashlib.sha256()

    def read(self, size: int, /) -> bytes:
        if size <= 0:
            raise ValueError("The scanner must read a positive number of bytes.")
        block = self.source.read(min(size, _CHUNK))
        self.count += len(block)
        if self.count > self.size:
            raise ScannerError("The object is larger than its recorded size.")
        self.digest.update(block)
        return block

    def verify(self, expected_sha256: str) -> None:
        if self.count != self.size or self.read(1) or self.digest.hexdigest() != expected_sha256:
            raise ScannerError("The bytes scanned do not match the recorded object.")


def _oversize() -> ScanResult:
    return ScanResult(
        "not_scanned",
        settings.IKI_FILES_SCANNER,
        "",
        "too_large",
        allow_unscanned=settings.IKI_FILES_SCAN_OVERSIZE_POLICY == "allow",
    )


def scan_object(*, storage_uri: str, version: str, sha256: str, size: int) -> ScanResult:
    """Scan verified content in the bucket, including a registered object's pinned version."""
    if size > settings.IKI_FILES_SCAN_MAX_BYTES:
        decision = _oversize()
        if version and decision.allow_unscanned:
            # Bind a registration's size exception to the bytes of its pinned version.
            with closing(storage.open_object(storage_uri, version)) as source:
                checked = _CheckedStream(source, size)
                while checked.read(_CHUNK):
                    pass
                checked.verify(sha256)
        return decision
    with closing(storage.open_object(storage_uri, version)) as stream:
        return scan_blob(stream=stream, sha256=sha256, size=size)


def scan_blob(*, stream: ScanStream, sha256: str, size: int) -> ScanResult:
    """Run the configured adapter against a stream owned by the caller."""
    if size > settings.IKI_FILES_SCAN_MAX_BYTES:
        return _oversize()
    checked = _CheckedStream(stream, size)
    adapter = import_string(settings.IKI_FILES_SCANNER)
    result: object = adapter(stream=checked, sha256=sha256, size=size)
    if not isinstance(result, ScanResult) or result.status not in (
        "clean",
        "infected",
        "not_scanned",
    ):
        raise ScannerError("The scanner returned an unknown verdict.")
    if (
        not result.scanner.strip()
        or len(result.scanner) > 255
        or len(result.signature_version) > 255
        or len(result.reason) > 1024
        or result.allow_unscanned
        or result.reason == "too_large"
        or (result.status == "clean" and result.reason)
    ):
        raise ScannerError("The scanner returned invalid scan evidence.")
    if result.status != "clean" and not result.reason.strip():
        raise ScannerError("A held file needs the scanner's finding or reason.")
    if result.status in ("clean", "infected"):
        if not result.signature_version.strip():
            raise ScannerError("The scanner did not identify its signatures.")
        checked.verify(sha256)
    return result


def _reply(conn: socket.socket, request: int) -> str:
    """Read one NUL-terminated IDSESSION record. Only one request is outstanding."""
    data = bytearray()
    while b"\0" not in data and len(data) < _MAX_REPLY:
        packet = conn.recv(_MAX_REPLY - len(data))
        if not packet:
            break
        data.extend(packet)
    prefix = f"{request}: ".encode("ascii")
    if not data.startswith(prefix) or data.count(0) != 1 or not data.endswith(b"\0"):
        raise ScannerError("clamd returned an incomplete or unexpected response.")
    response = bytes(data[len(prefix) : -1]).decode("ascii")
    if not response.isprintable():
        raise ScannerError("clamd returned an invalid response.")
    return response


def clamd(*, stream: ScanStream, sha256: str, size: int) -> ScanResult:
    """Submit the object using INSTREAM on a private clamd TCP endpoint."""
    with socket.create_connection(
        (settings.IKI_CLAMD_HOST, settings.IKI_CLAMD_PORT),
        timeout=settings.IKI_CLAMD_TIMEOUT_SECONDS,
    ) as conn:
        # Both VERSION queries go to the daemon that actually scans the bytes.
        # A signature reload during the scan requires another attempt.
        conn.sendall(b"zIDSESSION\0zVERSION\0")
        version = _reply(conn, 1)
        database = re.fullmatch(r"(ClamAV [^/\s]+)/([1-9][0-9]*)/[^/]+", version)
        if database is None:
            raise ScannerError("clamd did not report a signature database version.")
        scanner, signatures = database.groups()
        conn.sendall(b"zINSTREAM\0")
        try:
            while chunk := stream.read(_CHUNK):
                conn.sendall(len(chunk).to_bytes(4, "big"))
                conn.sendall(chunk)
            conn.sendall(bytes(4))
        except BrokenPipeError as error:
            # A daemon with a smaller StreamMaxLength sends the reason and
            # closes the connection as soon as it sees an over-limit frame.
            verdict = _reply(conn, 2)
            if verdict != "INSTREAM size limit exceeded. ERROR":
                raise ScannerError("clamd closed the connection during the scan.") from error
        else:
            verdict = _reply(conn, 2)
        if verdict == "INSTREAM size limit exceeded. ERROR":
            return ScanResult("not_scanned", scanner, signatures, "clamd_stream_limit")
        conn.sendall(b"zVERSION\0")
        if _reply(conn, 3) != version:
            raise ScannerError("clamd's signatures changed during the scan.")
        conn.sendall(b"zEND\0")

    if verdict == "stream: OK":
        return ScanResult("clean", scanner, signatures)
    found = re.fullmatch(r"stream: ([^\s]+) FOUND", verdict)
    if found is None:
        raise ScannerError("clamd did not return a scan verdict.")
    finding = found.group(1)
    if finding.startswith(("Heuristics.Limits.Exceeded", "Heuristics.Encrypted.")):
        return ScanResult("not_scanned", scanner, signatures, finding)
    return ScanResult("infected", scanner, signatures, finding)

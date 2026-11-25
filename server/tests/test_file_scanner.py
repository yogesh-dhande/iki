"""The adapter accepts a complete, identified scan or leaves the file held."""

import hashlib
import io
import socket
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from django.test import override_settings

from iki_server.files import scanner, storage

_VERSION = b"ClamAV 1.4.3/27800/Mon Jan  5 08:31:09 2026"
_CLAMD = "iki_server.files.scanner.clamd"


def _daemon(monkeypatch: pytest.MonkeyPatch, *replies: bytes) -> Mock:
    conn = Mock(spec=socket.socket)
    conn.recv.side_effect = [*replies, b""]
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext(conn))
    return conn


def _scan(data: bytes) -> scanner.ScanResult:
    return scanner.scan_blob(
        stream=io.BytesIO(data), sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )


@pytest.mark.parametrize(
    ("reply", "state", "finding"),
    [
        (b"stream: OK", "clean", ""),
        (b"stream: Eicar-Signature FOUND", "infected", "Eicar-Signature"),
        (
            b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND",
            "not_scanned",
            "Heuristics.Limits.Exceeded.MaxScanSize",
        ),
        (b"stream: Heuristics.Encrypted.Zip FOUND", "not_scanned", "Heuristics.Encrypted.Zip"),
    ],
)
@override_settings(IKI_FILES_SCANNER=_CLAMD, IKI_FILES_SCAN_OVERSIZE_POLICY="allow")
def test_clamd_scans_the_whole_stream_on_one_versioned_session(
    monkeypatch: pytest.MonkeyPatch, reply: bytes, state: str, finding: str
) -> None:
    conn = _daemon(
        monkeypatch,
        b"1: Cla",
        _VERSION[3:] + b"\0",
        b"2: " + reply + b"\0",
        b"3: " + _VERSION + b"\0",
    )
    first = b"a" * (256 * 1024)
    decision = _scan(first + b"b")
    assert decision.status == state
    assert decision.scanner == "ClamAV 1.4.3"
    assert decision.signature_version == "27800"
    assert decision.reason == finding
    assert not decision.allow_unscanned
    assert [call.args[0] for call in conn.sendall.call_args_list] == [
        b"zIDSESSION\0zVERSION\0",
        b"zINSTREAM\0",
        len(first).to_bytes(4, "big"),
        first,
        bytes.fromhex("00000001"),
        b"b",
        bytes(4),
        b"zVERSION\0",
        b"zEND\0",
    ]


@override_settings(IKI_FILES_SCANNER=_CLAMD, IKI_FILES_SCAN_OVERSIZE_POLICY="allow")
def test_daemon_stream_limit_holds_the_file_even_with_a_size_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _daemon(monkeypatch, b"1: " + _VERSION + b"\0", b"2: INSTREAM size limit exceeded. ERROR\0")
    assert _scan(b"abc") == scanner.ScanResult(
        "not_scanned", "ClamAV 1.4.3", "27800", "clamd_stream_limit"
    )


@pytest.mark.parametrize(
    "replies",
    [
        [b"1: ClamAV 1.4.3\0"],
        [b"1: " + _VERSION + b"\0", b"2: stream: OK"],
        [b"1: " + _VERSION + b"\0", b"2: " + b"A" * 4093],
        [b"1: " + _VERSION + b"\0", b"4: stream: OK\0"],
        [b"1: " + _VERSION + b"\0", b"2: stream: OK\0extra\0"],
        [
            b"1: " + _VERSION + b"\0",
            b"2: stream: scanner failed ERROR\0",
            b"3: " + _VERSION + b"\0",
        ],
        [
            b"1: " + _VERSION + b"\0",
            b"2: stream: OK\0",
            b"3: " + _VERSION.replace(b"27800", b"27801") + b"\0",
        ],
    ],
)
@override_settings(IKI_FILES_SCANNER=_CLAMD)
def test_ambiguous_or_incomplete_replies_have_no_decision(
    monkeypatch: pytest.MonkeyPatch, replies: list[bytes]
) -> None:
    _daemon(monkeypatch, *replies)
    with pytest.raises(scanner.ScannerError):
        _scan(b"abc")


@pytest.mark.parametrize(("policy", "version"), [("allow", ""), ("refuse", ""), ("refuse", "17")])
@override_settings(IKI_FILES_SCAN_MAX_BYTES=10)
def test_oversize_decision_without_a_registration_exception_skips_the_stream(
    monkeypatch: pytest.MonkeyPatch, policy: str, version: str
) -> None:
    opened = Mock(side_effect=AssertionError("Open must follow the size check."))
    monkeypatch.setattr(storage, "open_object", opened)
    with override_settings(IKI_FILES_SCAN_OVERSIZE_POLICY=policy):
        result = scanner.scan_object(
            storage_uri="gs://lab/reads", version=version, sha256="a" * 64, size=11
        )
    assert result.status == "not_scanned"
    assert result.reason == "too_large"
    assert result.allow_unscanned == (policy == "allow")
    opened.assert_not_called()


@pytest.mark.parametrize("changed", [False, True])
@override_settings(IKI_FILES_SCAN_MAX_BYTES=1, IKI_FILES_SCAN_OVERSIZE_POLICY="allow")
def test_registered_size_exception_verifies_pinned_bytes(
    monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    source = io.BytesIO(b"new" if changed else b"old")
    opened = Mock(return_value=source)
    monkeypatch.setattr(storage, "open_object", opened)
    monkeypatch.setattr(scanner, "import_string", Mock(side_effect=AssertionError("oversize scan")))
    digest = hashlib.sha256(b"old").hexdigest()
    if changed:
        with pytest.raises(scanner.ScannerError):
            scanner.scan_object(storage_uri="gs://lab/reads", version="17", sha256=digest, size=3)
    else:
        result = scanner.scan_object(
            storage_uri="gs://lab/reads", version="17", sha256=digest, size=3
        )
        assert result.status == "not_scanned" and result.allow_unscanned
    opened.assert_called_once_with("gs://lab/reads", "17")
    assert source.closed


@override_settings(IKI_FILES_SCANNER=_CLAMD)
def test_scanner_outage_closes_the_pinned_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    source = io.BytesIO(b"abc")
    opened = Mock(return_value=source)
    monkeypatch.setattr(storage, "open_object", opened)
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=ConnectionRefusedError))
    with pytest.raises(ConnectionRefusedError):
        scanner.scan_object(storage_uri="s3://lab/file", version="v1", sha256="a" * 64, size=3)
    opened.assert_called_once_with("s3://lab/file", "v1")
    assert source.closed


@pytest.mark.parametrize(
    ("read_size", "sha", "size", "result"),
    [
        (1, hashlib.sha256(b"abc").hexdigest(), 3, scanner.ScanResult("clean", "lab", "1")),
        (3, "a" * 64, 3, scanner.ScanResult("clean", "lab", "1")),
        (2, hashlib.sha256(b"ab").hexdigest(), 2, scanner.ScanResult("clean", "lab", "1")),
        (3, hashlib.sha256(b"abc").hexdigest(), 3, scanner.ScanResult("clean", "lab", "")),
        (
            3,
            hashlib.sha256(b"abc").hexdigest(),
            3,
            scanner.ScanResult("not_scanned", "lab", "", "too_large", True),
        ),
    ],
)
def test_custom_adapter_must_supply_reliable_evidence(
    monkeypatch: pytest.MonkeyPatch, read_size: int, sha: str, size: int, result: scanner.ScanResult
) -> None:
    def invalid(*, stream: scanner.ScanStream, sha256: str, size: int) -> scanner.ScanResult:
        stream.read(read_size)
        return result

    monkeypatch.setattr(scanner, "import_string", lambda path: invalid)
    with pytest.raises(scanner.ScannerError):
        scanner.scan_blob(stream=io.BytesIO(b"abc"), sha256=sha, size=size)

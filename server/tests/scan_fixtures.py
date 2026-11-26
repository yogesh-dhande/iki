"""Files with a completed scan for tests of features that use files."""

from iki_server.files import scan_state, scanner
from iki_server.files.models import Blob


def scan(*, stream: scanner.ScanStream, sha256: str, size: int) -> scanner.ScanResult:
    """The deterministic scanner for tests that send bytes through the upload API."""
    while stream.read(64 * 1024):
        pass
    return scanner.ScanResult(status="clean", scanner="fixture", signature_version="1")


def clean_blob(*, sha256: str, size: int, storage_uri: str) -> Blob:
    """A checked, clean blob for tests that construct file metadata directly."""
    blob, _ = Blob.objects.get_or_create(
        sha256=sha256,
        registered_external=False,
        defaults={"size": size, "storage_uri": storage_uri},
    )
    scan_state.record_result(blob, state="clean", scanner="fixture", signature_version="1")
    return blob

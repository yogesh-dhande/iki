"""What every file store does for Iki, the same on S3 and on Cloud Storage (ADR 0109).

Each test runs against S3 (the test SeaweedFS) and Cloud Storage (fake_gcs), through
:mod:`iki_server.files.storage` alone, as the rest of Iki uses it: uploads in parts
through signed URLs, versions pinned and read, deletes for good, and downloads.
"""

import hashlib
import inspect
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from conftest import S3_BUCKET

from iki_server.files import gcs, storage

MIB = 1024**2


@pytest.fixture(params=["s3", "gcs"])
def store(request: pytest.FixtureRequest) -> Iterator[str]:
    """Storage configured on each backend in turn; yields which."""
    if request.param == "s3":
        s3 = request.getfixturevalue("s3")
        s3.put_bucket_versioning(Bucket=S3_BUCKET, VersioningConfiguration={"Status": "Enabled"})
    else:
        request.getfixturevalue("gcs")
    yield request.param


def key(name: str) -> str:
    return storage.staging_key(f"{name}-{uuid.uuid4().hex}")


def stored(tmp_path: Path, name: str, data: bytes, media_type: str = "text/csv") -> str:
    """Stores the bytes at a new key, as the server stores what it makes; returns the key."""
    path = tmp_path / uuid.uuid4().hex
    path.write_bytes(data)
    where = key(name)
    storage.upload_file(path, where, media_type)
    return where


def test_each_store_has_every_function_with_the_same_signature() -> None:
    for name, function in gcs.FUNCTIONS.items():
        assert inspect.signature(getattr(storage, name)) == inspect.signature(function), name


def test_uploads_go_in_parts_through_signed_urls(store: str) -> None:
    where = key("parts")
    first, last = bytes(range(256)) * (5 * MIB // 256), b"the last part"
    upload_id = storage.start_multipart(where)
    urls = storage.part_urls(where, upload_id, [1, 2])
    assert set(urls) == {1, 2}
    # Parts arrive in any order, and a part sent again replaces the earlier copy.
    assert requests.put(urls[2], data=b"a wrong last part", timeout=30).status_code == 200
    assert requests.put(urls[1], data=first, timeout=30).status_code == 200
    assert requests.put(urls[2], data=last, timeout=30).status_code == 200
    arrived = storage.uploaded_parts(where, upload_id)
    assert [size for size, _ in arrived[1]] == [len(first)]
    # S3 and Cloud Storage list the latest copy; some S3-compatible stores list every copy,
    # in an order their clocks can't always tell, so the server picks by size.
    assert len(last) in [size for size, _ in arrived[2]]
    (etag,) = [e for size, e in arrived[2] if size == len(last)]
    storage.complete_multipart(where, upload_id, {1: arrived[1][0][1], 2: etag})
    data = first + last
    assert storage.digest(where) == (hashlib.sha256(data).hexdigest(), len(data))
    assert storage.exists(where)
    found = storage.head(storage.bucket(), where)
    assert found is not None and found["size"] == len(data) and found["version"]


def test_a_part_url_is_for_its_own_part_and_upload(store: str) -> None:
    where = key("one-part")
    upload_id = storage.start_multipart(where)
    url = storage.part_url(where, upload_id, 1)
    parts = urlsplit(url)
    tampered = url.replace("partNumber=1", "partNumber=2")
    assert tampered != url and parts.query
    assert requests.put(tampered, data=b"x", timeout=30).status_code == 403
    storage.abort_multipart(where, upload_id)
    storage.abort_multipart(where, upload_id)  # Twice is fine: it's gone already.


def test_an_empty_object(store: str) -> None:
    where = key("empty")
    storage.put_empty(where)
    assert storage.digest(where) == (hashlib.sha256(b"").hexdigest(), 0)


def test_each_version_is_pinned_and_read_for_good(store: str, tmp_path: Path) -> None:
    where = stored(tmp_path, "plate.csv", b"well,od\nA1,0.42\n")
    bucket = storage.bucket()
    first = storage.head(bucket, where)
    assert first is not None and first["media_type"] == "text/csv"
    path = tmp_path / "again"
    path.write_bytes(b"rewritten by the instrument\n")
    storage.upload_file(path, where, "text/csv")
    second = storage.head(bucket, where)
    assert second is not None and second["version"] != first["version"]
    # The first version is still there, by its version, whatever the object is now.
    pinned = storage.head(bucket, where, first["version"])
    assert pinned == first
    assert storage.digest_object(bucket, where, first["version"]) == (
        hashlib.sha256(b"well,od\nA1,0.42\n").hexdigest(),
        16,
    )
    uri = storage.uri(where)
    assert storage.open_object(uri, first["version"]).read() == b"well,od\nA1,0.42\n"
    assert storage.open_object(uri, first["version"], 8).read() == b"A1,0.42\n"
    url = storage.download_url(uri, first["version"], "plate 7.csv", "text/csv")
    fetched = requests.get(url, timeout=30)
    assert fetched.status_code == 200 and fetched.content == b"well,od\nA1,0.42\n"
    assert fetched.headers["Content-Disposition"].startswith('attachment; filename="plate 7.csv"')
    assert fetched.headers["Content-Type"] == "text/csv"
    assert storage.head(bucket, where, "not-a-version") is None
    assert storage.head(bucket, key("never-stored")) is None
    assert storage.versioned(bucket)


def test_deleting_for_good_deletes_every_version(store: str, tmp_path: Path) -> None:
    where = stored(tmp_path, "artifact", b"one")
    first = storage.head(storage.bucket(), where)
    assert first is not None
    path = tmp_path / "two"
    path.write_bytes(b"two")
    storage.upload_file(path, where, "application/octet-stream")
    storage.delete(where)
    assert not storage.exists(where)
    # Deleted, but a versioned bucket keeps it.
    assert storage.head(storage.bucket(), where, first["version"]) is not None
    assert storage.delete_for_good(where) >= 2
    assert storage.head(storage.bucket(), where, first["version"]) is None
    assert storage.delete_for_good(where) == 0


def test_copies_and_deletes_and_local_files(store: str, tmp_path: Path) -> None:
    where = stored(tmp_path, "source", b"some bytes")
    target = key("target")
    storage.copy(where, target)
    assert storage.digest(target) == (hashlib.sha256(b"some bytes").hexdigest(), 10)
    storage.download_file(target, tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"some bytes"
    storage.delete(target)
    assert not storage.exists(target)
    storage.delete(target)  # Nothing there: nothing to do.


def test_uris_name_the_bucket_and_key(store: str) -> None:
    where = key("named")
    uri = storage.uri(where)
    assert uri == f"{'gs' if store == 'gcs' else 's3'}://{storage.bucket()}/{where}"
    assert storage.split_uri(uri) == (storage.bucket(), where)
    origin = storage.upload_origin()
    assert origin is not None
    assert urlsplit(storage.part_url(where, "x", 1)).netloc == urlsplit(origin).netloc

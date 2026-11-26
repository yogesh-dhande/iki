"""Files on Google Cloud Storage through its own APIs (ADR 0109), end to end: what the
shared contract (test_storage_contract) doesn't cover. URLs are signed by IAM as the
pod's service account, with no key; versions are generations; holds are the bucket
owner's, and refuse a delete; setup_storage checks the bucket as on S3."""

import hashlib
import socket
import uuid
from io import StringIO
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from api_client import Api, problem
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.management import CommandError, call_command
from fake_gcs import EMAIL, FakeGcs
from google.api_core import exceptions as google_errors
from google.auth import credentials
from google.oauth2 import service_account
from org_world import World, build_world

from iki_server.files import gcs as backend
from iki_server.files import storage
from iki_server.files.management.commands import setup_storage
from iki_server.files.models import Blob
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

MIB = 1024**2
LAB = "lab-data-gcs"


@pytest.fixture
def world() -> World:
    return build_world()


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def test_uploads_in_parts_and_downloads_are_signed_by_iam_as_the_pods_account(
    world: World, gcs: FakeGcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "DEFAULT_PART_SIZE", storage.MIN_PART_SIZE)
    data = bytes(range(256)) * (5 * MIB * 2 // 256) + b"tail"
    api = Api(world.browser("editor"))
    signed = len(gcs.signed)
    started = api.post(
        "/uploads",
        {
            "project": str(world.project.pk),
            "name": "plate.bin",
            "media_type": "application/octet-stream",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    ).json()
    assert started["part_count"] == 3
    # Each part's URL was signed by IAM's signBlob, as the server's service account.
    assert gcs.signed[signed:] == [EMAIL] * 3
    for part in started["parts"]:
        assert urlsplit_host(part["url"]) == urlsplit_host(gcs.url)
        offset = (part["number"] - 1) * storage.MIN_PART_SIZE
        chunk = data[offset : offset + part["size"]]
        assert requests.put(part["url"], data=chunk, timeout=30).status_code == 200
    assert api.post(f"/uploads/{started['id']}/complete").status_code == 202
    run_tasks()
    upload = api.get(f"/uploads/{started['id']}").json()
    assert upload["state"] == "ready", upload
    blob = Blob.objects.get(files__pk=upload["file"]["id"])
    assert blob.storage_uri.startswith(f"gs://{storage.bucket()}/")
    response = Api(world.browser("viewer")).get(f"/files/{upload['file']['id']}/content")
    assert response.status_code == 302
    fetched = requests.get(response["Location"], timeout=30)
    assert fetched.content == data
    assert fetched.headers["Content-Disposition"].startswith('attachment; filename="plate.bin"')
    # The download's URL was signed by IAM too: the server has no key.
    assert gcs.signed[signed:] == [EMAIL] * 4


def urlsplit_host(url: str) -> str:
    return urlsplit(url).netloc


def test_a_registered_file_is_pinned_to_its_generation(
    world: World, gcs: FakeGcs, settings: Any
) -> None:
    gcs.bucket(LAB)
    settings.IKI_FILES_REGISTER_LOCATIONS = [f"gs://{LAB}/runs/"]
    key = f"runs/{uuid.uuid4().hex}/plate-7.csv"
    first = gcs.write(LAB, key, b"well,od600\nA1,0.42\n", "text/csv")
    api = Api(world.browser("editor"))
    started = api.post(
        "/registrations", {"project": str(world.project.pk), "uri": f"gs://{LAB}/{key}"}
    )
    assert started.status_code == 202, started.content
    assert started.json()["version"] == str(first.generation)
    gcs.write(LAB, key, b"overwritten\n", "text/csv")
    run_tasks()
    done = api.get(f"/registrations/{started.json()['id']}").json()
    assert done["state"] == "ready", done
    assert done["file"]["sha256"] == hashlib.sha256(b"well,od600\nA1,0.42\n").hexdigest()
    elsewhere = api.post(
        "/registrations", {"project": str(world.project.pk), "uri": f"s3://{LAB}/{key}"}
    )
    (error,) = problem(elsewhere, 422, "validation_failed")["errors"]
    assert "Name an object, such as gs://lab-data/runs/plate-7.csv" in error["detail"]
    bogus = api.post(
        "/registrations",
        {"project": str(world.project.pk), "uri": f"gs://{LAB}/{key}", "version": "nope"},
    )
    (error,) = problem(bogus, 422, "validation_failed")["errors"]
    assert error["detail"].startswith("There's no object at version nope")


def test_a_held_generation_is_not_deleted(gcs: FakeGcs, tmp_path: Any) -> None:
    """The bucket owner's holds and retention (Object Retention Lock) refuse a delete, as
    S3's Object Lock does: deleting for good fails rather than leave the file half gone."""
    path = tmp_path / "artifact"
    path.write_bytes(b"kept for a matter")
    key = storage.artifact_key(uuid.uuid4())
    storage.upload_file(path, key, "application/octet-stream")
    held = gcs.live(storage.bucket(), key)
    assert held is not None
    held.temporary_hold = True
    with pytest.raises(storage.StorageError) as refused:
        storage.delete_for_good(key)
    assert refused.value.status == 403
    assert isinstance(refused.value.__cause__, google_errors.Forbidden)
    assert storage.exists(key)
    held.temporary_hold = False
    held.retention = {"mode": "Unlocked", "retainUntilTime": "2999-01-01T00:00:00+00:00"}
    with pytest.raises(storage.StorageError, match="403"):
        storage.delete_for_good(key)
    held.retention = None
    assert storage.delete_for_good(key) == 1
    assert not storage.exists(key)


def test_credentials_with_a_key_sign_locally_and_a_persons_cant_sign(gcs: FakeGcs) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    keyed = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
        {
            "type": "service_account",
            "client_email": "dev@example.iam.gserviceaccount.com",
            "private_key": pem,
            "private_key_id": "1",
            "token_uri": f"{gcs.url}/token",
        }
    )
    clients = backend.clients()
    assert backend.Google(keyed, clients.client, clients.session, clients.endpoint).signer() is (
        keyed
    )

    class Person(credentials.Credentials):
        def __init__(self) -> None:
            super().__init__()  # type: ignore[no-untyped-call]

        def refresh(self, request: Any) -> None:
            self.token = "a person's"

    person = backend.Google(Person(), clients.client, clients.session, clients.endpoint)
    with pytest.raises(storage.StorageError, match="service account"):
        person.signer()


@pytest.fixture
def bucket(gcs: FakeGcs, settings: Any) -> Any:
    settings.IKI_PUBLIC_URL = "https://iki.example.com"
    settings.IKI_WEB_DEV_SERVER = False
    found = gcs.bucket(storage.bucket())
    found.cors, found.lifecycle, found.versioning = [], [], True
    return found


def run(*args: str) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("setup_storage", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_setup_storage_sets_cors_and_lifecycle_and_checks_versions_and_keys(bucket: Any) -> None:
    theirs = {"origin": ["https://bi.example.com"], "method": ["GET"]}
    bucket.cors = [theirs]
    missing = "missing: CORS for browsers' uploads and reads; lifecycle rules"
    with pytest.raises(CommandError, match=missing):
        run("--check")
    out, _ = run()
    assert "CORS for browsers' uploads and reads, lifecycle rules for abandoned uploads" in out
    assert bucket.cors[0] == theirs
    assert bucket.cors[1]["origin"] == ["https://iki.example.com"]
    assert bucket.cors[1]["method"] == ["GET", "PUT"]
    assert bucket.cors[1]["responseHeader"] == ["content-type", "range", "content-range"]
    actions = [rule["action"]["type"] for rule in bucket.lifecycle]
    assert actions == ["AbortIncompleteMultipartUpload", "Delete"]
    assert all(r["condition"]["matchesPrefix"][0].endswith("/uploads/") for r in bucket.lifecycle)
    assert run("--check")[0].startswith(f"Bucket {bucket.name} is ready")

    bucket.versioning, bucket.kms_key = False, ""
    _, err = run()
    assert "doesn't keep versions" in err and "has no customer-managed key" in err
    assert not bucket.versioning
    out, _ = run("--enable-versioning")
    assert "Turned on versioning" in out and bucket.versioning


def test_setup_storage_replaces_its_own_older_cors_rule_on_cloud_storage(bucket: Any) -> None:
    """Cloud Storage applies the first rule that matches, so Iki's rule from before previews
    read ranges (ADR 0227) is replaced, not shadowing the new one; others' rules stay."""
    theirs = {"origin": ["https://bi.example.com"], "method": ["GET"]}
    older = {
        "origin": ["https://iki.example.com"],
        "method": ["GET", "PUT"],
        "responseHeader": ["content-type"],
        "maxAgeSeconds": 3600,
    }
    bucket.cors = [theirs, older]
    with pytest.raises(CommandError, match="CORS for browsers' uploads and reads"):
        run("--check")
    run()
    assert bucket.cors == [theirs, setup_storage.gcs_cors_rule()]


def test_cloud_storage_out_of_reach_is_a_503(world: World, gcs: FakeGcs, settings: Any) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = int(unused.getsockname()[1])
    settings.IKI_FILES_ENDPOINT_URL = f"http://127.0.0.1:{port}"
    storage.reset()
    data = b"some bytes"
    started = Api(world.browser("editor")).post(
        "/uploads",
        {
            "project": str(world.project.pk),
            "name": "plate.csv",
            "media_type": "text/csv",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert "file storage" in problem(started, 503, "storage_unavailable")["detail"]

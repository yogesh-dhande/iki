"""``iki-server check_restore`` (ADR 0099): after a restore, whether the deployment is
whole: its schema, its encrypted values with the keys it has, and the files its records
name, in storage at their size and, if asked, their SHA-256."""

import hashlib
import json
from datetime import datetime, timedelta
from io import StringIO
from typing import Any

import pytest
from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.utils import timezone
from org_world import World
from test_run_artifacts import aligning, kept
from test_run_results import done, output
from test_runs import world
from test_workers import keep_connection

from iki_server.audit import log as audit
from iki_server.core import crypto, restore
from iki_server.files import storage
from iki_server.files.models import Blob, File
from iki_server.runs.models import RunArtifact
from iki_server.webhooks.models import Webhook

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]

PDB = b"HEADER    KINASE\nATOM      1  N   MET A   1\n"


@pytest.fixture(autouse=True)
def storage_(s3: Any) -> Any:
    return s3


def stored(world: World, data: bytes = PDB, name: str = "1abc.pdb") -> File:
    sha = hashlib.sha256(data).hexdigest()
    key = storage.blob_key(sha)
    storage.client().put_object(Bucket=storage.bucket(), Key=key, Body=data)
    blob = Blob.objects.create(sha256=sha, size=len(data), storage_uri=storage.uri(key))
    return File.objects.create(
        blob=blob, name=name, media_type="chemical/x-pdb", project=world.project
    )


def checked(*args: str) -> tuple[bool, dict[str, Any]]:
    out = StringIO()
    try:
        call_command("check_restore", "--json", *args, stdout=out)
    except CommandError:
        return False, json.loads(out.getvalue())
    (line,) = out.getvalue().splitlines()
    return True, json.loads(line)


def by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in report["checks"]}


def test_a_whole_restore_passes_and_says_how_far_its_data_reaches(world: World) -> None:
    stored(world)
    pod, _run, job = aligning(world)
    kept(pod, job, "alignment.txt")
    done(pod, job, output(metrics={"score": 1.0}))
    Webhook.objects.create(
        url="https://lims.example.com/hook", events=["run.finished"], secret=crypto.encrypt("s")
    )
    audit.record("encryption.rotated", data={"secrets": 0})
    ok, report = checked()
    assert ok, report
    checks = by_name(report)
    assert checks["Database schema"]["checked"] > 0
    assert checks["Encrypted values"]["checked"] == 1
    # The file's contents and the run's artifact.
    assert checks["Files in storage"]["checked"] == 2
    assert report["counts"]["files"] == 1
    assert report["counts"]["runs"] == 1
    newest = datetime.fromisoformat(report["data_until"])
    assert timezone.now() - newest < timedelta(minutes=1)


def test_files_missing_or_changed_in_storage_fail_it_by_name(world: World) -> None:
    gone, short, swapped = (
        stored(world, b"gone", "a.txt"),
        stored(world, b"short", "b.txt"),
        stored(world, b"swapped", "c.txt"),
    )
    storage.client().delete_object(Bucket=storage.bucket(), Key=storage.blob_key(gone.blob.sha256))
    storage.client().put_object(
        Bucket=storage.bucket(), Key=storage.blob_key(short.blob.sha256), Body=b"shor"
    )
    # The same size, other bytes: only reading them tells.
    storage.client().put_object(
        Bucket=storage.bucket(), Key=storage.blob_key(swapped.blob.sha256), Body=b"SWAPPED"
    )
    ok, report = checked()
    assert not ok
    found = by_name(report)["Files in storage"]
    assert found["problems"] == 2
    assert any("missing from" in p and gone.blob.sha256[:12] in p for p in found["shown"])
    assert any("4 bytes, not 5" in p for p in found["shown"])

    ok, report = checked("--hashes")
    found = by_name(report)["Files in storage"]
    assert found["problems"] == 3
    assert any(swapped.blob.sha256[:12] in p and "SHA-256" in p for p in found["shown"])


def test_an_artifact_iki_deleted_isnt_missing(world: World) -> None:
    pod, _run, job = aligning(world)
    kept(pod, job, "alignment.txt")
    done(pod, job, output(metrics={"score": 1.0}))
    RunArtifact.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    from iki_server.runs import artifacts

    assert artifacts.delete_expired() == 1
    ok, report = checked()
    assert ok, report
    assert by_name(report)["Files in storage"]["checked"] == 0


def test_the_wrong_encryption_keys_fail_it(world: World, settings: Any) -> None:
    Webhook.objects.create(
        url="https://lims.example.com/hook", events=["run.finished"], secret=crypto.encrypt("s")
    )
    settings.IKI_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    ok, report = checked()
    assert not ok
    found = by_name(report)["Encrypted values"]
    assert found["problems"] == 1
    assert found["shown"][0].startswith("iki_webhooks.Webhook.secret of ")
    assert "IKI_ENCRYPTION_KEYS" in found["note"]


def test_a_schema_behind_the_code_fails_it(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    class Behind:
        def __init__(self, connection: Any) -> None:
            from django.db.migrations.executor import MigrationExecutor

            self.loader = MigrationExecutor(connection).loader

        def migration_plan(self, targets: Any) -> list[tuple[Any, bool]]:
            return [(self.loader.graph.nodes[("iki_files", "0001_initial")], False)]

    monkeypatch.setattr(restore, "MigrationExecutor", Behind)
    ok, report = checked()
    assert not ok
    assert by_name(report)["Database schema"]["shown"] == ["iki_files.0001_initial isn't applied"]


def test_a_sample_checks_so_many_files_of_each_kind(world: World) -> None:
    for n in range(5):
        stored(world, f"file {n}".encode(), f"{n}.txt")
    ok, report = checked("--sample", "2")
    assert ok, report
    assert by_name(report)["Files in storage"]["checked"] == 2
    with pytest.raises(CommandError, match="--sample"):
        call_command("check_restore", "--sample", "0", stdout=StringIO())


def test_files_in_other_buckets_are_checked_only_when_asked(world: World) -> None:
    uri = "s3://lab-data-unreachable/runs/plate-7.csv"
    Blob.objects.create(
        sha256="a" * 64, size=3, storage_uri=uri, registered_external=True, object_version="v1"
    )
    ok, report = checked()
    assert ok, report
    assert "--external" in by_name(report)["Files in storage"]["note"]
    ok, report = checked("--external")
    assert not ok
    assert any(uri in p for p in by_name(report)["Files in storage"]["shown"])


def test_it_reads_for_people_too(world: World) -> None:
    stored(world)
    out = StringIO()
    call_command("check_restore", stdout=out)
    said = out.getvalue()
    assert said.startswith("Data until ")
    assert "    ok  Files in storage: 1 checked" in said
    assert "Holds " in said and " 1 files," in said


def test_restored_elsewhere_it_reads_the_files_replicas_through_an_alias(
    world: World, settings: Any
) -> None:
    """In the DR region, the records still name the lost region's bucket; the objects are in
    its replica, here the test's bucket."""
    gone = "iki-files-lost-region"
    data = b"replicated"
    sha = hashlib.sha256(data).hexdigest()
    key = storage.blob_key(sha)
    storage.client().put_object(Bucket=storage.bucket(), Key=key, Body=data)
    blob = Blob.objects.create(sha256=sha, size=len(data), storage_uri=f"s3://{gone}/{key}")
    File.objects.create(blob=blob, name="r.txt", media_type="text/plain", project=world.project)
    ok, report = checked()
    assert not ok
    assert gone in by_name(report)["Files in storage"]["shown"][0]

    settings.IKI_FILES_BUCKET_ALIASES = {gone: storage.bucket()}
    ok, report = checked("--hashes")
    assert ok, report
    # Downloads go to the replica too.
    url = storage.download_url(blob.storage_uri, "", "r.txt", "text/plain")
    assert storage.bucket() in url and gone not in url

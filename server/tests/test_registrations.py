"""Registering files in place: one version of an object in an approved bucket, read for its hash."""

import contextlib
import hashlib
import uuid
from datetime import timedelta
from typing import Any

import pytest
import requests
from api_client import Api, problem
from org_world import World, build_world

from iki_server.audit.models import AuditEvent
from iki_server.files import services, storage
from iki_server.files.models import Blob, Registration
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

LAB, PLAIN = "lab-data", "lab-plain"
CSV = b"well,od600\nA1,0.42\nA2,0.39\n"


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def lab(s3: Any, settings: Any) -> dict[str, Any]:
    """A versioned lab bucket and an unversioned one, both approved; keys of this test's own."""
    for name in (LAB, PLAIN):
        with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
            s3.create_bucket(Bucket=name)
    s3.put_bucket_versioning(Bucket=LAB, VersioningConfiguration={"Status": "Enabled"})
    settings.IKI_FILES_REGISTER_LOCATIONS = [f"s3://{LAB}/runs/", f"s3://{PLAIN}/"]
    return {"s3": s3, "runs": f"runs/{uuid.uuid4().hex}"}


def put(lab: dict[str, Any], name: str, body: bytes, bucket: str = LAB) -> tuple[str, str]:
    """Stores an object; returns its URI and version."""
    key = f"{lab['runs']}/{name}"
    sent = lab["s3"].put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/csv")
    return f"s3://{bucket}/{key}", sent.get("VersionId", "")


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def register(api: Api, world: World, uri: str, **more: Any) -> Any:
    return api.post("/registrations", {"project": str(world.project.pk), "uri": uri, **more})


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def test_a_registered_file_is_pinned_to_its_version(world: World, lab: dict[str, Any]) -> None:
    api = api_as(world)
    uri, first = put(lab, "plate-7.csv", CSV)
    started = register(api, world, uri)
    assert started.status_code == 202, started.content
    body = started.json()
    assert (body["state"], body["version"], body["size"]) == ("checking", first, len(CSV))
    assert (body["name"], body["media_type"]) == ("plate-7.csv", "text/csv")
    # The instrument writes the file again: the registered file is still the first version.
    put(lab, "plate-7.csv", b"overwritten\n")
    run_tasks()
    done = api.get(f"/registrations/{body['id']}").json()
    assert done["state"] == "ready", done
    file = done["file"]
    assert file["sha256"] == hashlib.sha256(CSV).hexdigest()
    assert file["external"] is True
    blob = Blob.objects.get(pk=Blob.objects.get(files__pk=file["id"]).pk)
    assert (blob.storage_uri, blob.object_version) == (uri, first)
    response = api_as(world, "viewer").get(f"/files/{file['id']}/content")
    assert response.status_code == 302
    assert requests.get(response["Location"], timeout=30).content == CSV
    event = AuditEvent.objects.get(action="file.registered")
    assert event.data["uri"] == uri and event.data["version"] == first
    assert event.data["sha256"] == file["sha256"]


def test_a_version_can_be_named_and_registering_it_again_is_immediate(
    world: World, lab: dict[str, Any]
) -> None:
    api = api_as(world)
    uri, first = put(lab, "plate-8.csv", CSV)
    put(lab, "plate-8.csv", b"second\n")
    started = register(api, world, uri, version=first, name="Plate 8 (raw).csv")
    assert started.status_code == 202
    run_tasks()
    again = register(api, world, uri, version=first, media_type="text/csv; charset=utf-8")
    assert again.status_code == 201, again.content
    assert again.json()["state"] == "ready"
    assert again.json()["file"]["sha256"] == hashlib.sha256(CSV).hexdigest()
    assert Blob.objects.filter(storage_uri=uri).count() == 1


def test_only_approved_versioned_objects_can_be_registered(
    world: World, lab: dict[str, Any]
) -> None:
    api = api_as(world)

    def refused(uri: str, **more: Any) -> str:
        body = problem(register(api, world, uri, **more), 422, "validation_failed")
        (error,) = body["errors"]
        return str(error["detail"])

    uri, _ = put(lab, "plate.csv", CSV)
    elsewhere = uri.replace("/runs/", "/private/")
    assert "locations your administrators approved (s3://lab-data/runs/" in refused(elsewhere)
    assert "approved" in refused(f"s3://{LAB}/runs/../private/plate.csv")
    assert "approved" in refused(f"s3://{LAB}-other/runs/plate.csv")
    assert "Name an object" in refused(f"s3://{LAB}/runs/")
    assert "Name an object" in refused("https://example.com/plate.csv")
    plain, _ = put(lab, "plate.csv", CSV, bucket=PLAIN)
    assert "doesn't keep versions" in refused(plain)
    assert refused(uri.replace("plate.csv", "nothing.csv")).startswith("There's no object")
    assert refused(uri, version="nope").startswith("There's no object at version nope")
    assert not Registration.objects.exists()


def test_objects_from_before_versioning_must_get_a_version_first(
    world: World, lab: dict[str, Any], settings: Any
) -> None:
    bucket = f"lab-{uuid.uuid4().hex[:12]}"
    lab["s3"].create_bucket(Bucket=bucket)
    old, _ = put(lab, "old.csv", CSV, bucket=bucket)
    lab["s3"].put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    settings.IKI_FILES_REGISTER_LOCATIONS = [f"s3://{bucket}/"]
    body = problem(register(api_as(world), world, old), 422, "validation_failed")
    assert "stored before the bucket kept versions" in body["errors"][0]["detail"]


def test_registering_takes_editor_and_registrations_are_their_makers(
    world: World, lab: dict[str, Any]
) -> None:
    uri, _ = put(lab, "plate.csv", CSV)
    problem(register(api_as(world, "viewer"), world, uri), 403, "forbidden")
    made = register(api_as(world), world, uri).json()
    assert api_as(world, "viewer").get(f"/registrations/{made['id']}").status_code == 404
    assert [r["id"] for r in api_as(world).get("/registrations").json()["items"]] == [made["id"]]
    assert api_as(world, "viewer").get("/registrations/locations").json() == {
        "locations": [f"s3://{LAB}/runs/", f"s3://{PLAIN}/"]
    }


def test_registrations_still_checking_after_a_day_fail(world: World, lab: dict[str, Any]) -> None:
    uri, _ = put(lab, "plate.csv", CSV)
    made = register(api_as(world), world, uri).json()
    Registration.objects.filter(pk=made["id"]).update(
        expires_at=Registration.objects.get(pk=made["id"]).created_at - timedelta(seconds=1)
    )
    assert services.expire_registrations() == 1
    failed = Registration.objects.get(pk=made["id"])
    assert failed.state == "failed" and "in time" in failed.error
    # The queued check finds nothing to do.
    run_tasks()
    assert Registration.objects.get(pk=made["id"]).file is None


def test_buckets_elsewhere_are_signed_for_their_own_region(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.IKI_FILES_ENDPOINT_URL = ""
    settings.IKI_FILES_BUCKET = "iki"
    settings.IKI_FILES_REGION = "us-east-1"
    answers = {"old-bucket": None, "eu-bucket": "EU", "mumbai": "ap-south-1"}

    class Located:
        def get_bucket_location(self, Bucket: str) -> dict[str, Any]:  # noqa: N803 - boto3's
            return {"LocationConstraint": answers[Bucket]}

    storage.reset()
    monkeypatch.setattr(storage, "client", Located)
    assert storage.region_of("old-bucket") == "us-east-1"
    assert storage.region_of("eu-bucket") == "eu-west-1"
    assert storage.region_of("mumbai") == "ap-south-1"
    assert storage.region_of("iki") == "us-east-1"  # its own: never asked
    monkeypatch.undo()
    storage.reset()
    assert storage.signer_for("iki") is storage.signer()

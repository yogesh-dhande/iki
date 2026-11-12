"""Files: uploads in parts, checked against their SHA-256, stored once, read and downloaded."""

import hashlib
from datetime import timedelta
from typing import Any

import pytest
import requests
from api_client import Api, problem
from org_world import World, build_world

from iki_server.audit.models import AuditEvent
from iki_server.files import services, storage
from iki_server.files.models import Blob, File, Upload
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

PDB = b"HEADER    TEST STRUCTURE\nATOM      1  N   MET A   1\nEND\n"


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def small_parts(monkeypatch: pytest.MonkeyPatch) -> int:
    """Parts of S3's smallest size, so a few MiB make a multipart upload."""
    monkeypatch.setattr(storage, "DEFAULT_PART_SIZE", storage.MIN_PART_SIZE)
    return storage.MIN_PART_SIZE


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def start(api: Api, world: World, data: bytes, *, name: str = "1abc.pdb", **overrides: Any) -> Any:
    body = {
        "project": str(world.project.pk),
        "name": name,
        "media_type": "chemical/x-pdb",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **overrides,
    }
    return api.post("/uploads", body)


def send(started: dict[str, Any], data: bytes) -> None:
    offset = 0
    for part in started["parts"]:
        chunk = data[offset : offset + part["size"]]
        offset += part["size"]
        response = requests.put(part["url"], data=chunk, timeout=30)
        assert response.status_code == 200, response.text


def add(api: Api, world: World, data: bytes, **kw: Any) -> dict[str, Any]:
    """Uploads the bytes all the way: start, send, complete, check."""
    started = start(api, world, data, **kw)
    assert started.status_code == 201, started.content
    send(started.json(), data)
    completed = api.post(f"/uploads/{started.json()['id']}/complete")
    assert completed.status_code == 202, completed.content
    assert completed.json()["state"] == "checking"
    run_tasks()
    upload: dict[str, Any] = api.get(f"/uploads/{started.json()['id']}").json()
    assert upload["state"] == "ready", upload
    return upload


def keys(s3: Any) -> list[str]:
    from django.conf import settings

    listed = s3.list_objects_v2(
        Bucket=settings.IKI_FILES_BUCKET, Prefix=settings.IKI_FILES_PREFIX + "/"
    )
    return sorted(
        o["Key"].removeprefix(settings.IKI_FILES_PREFIX + "/") for o in listed.get("Contents", [])
    )


# --- Uploading ---------------------------------------------------------------------------------


def test_an_upload_becomes_a_file_stored_under_its_content(world: World, s3: Any) -> None:
    api = api_as(world)
    upload = add(api, world, PDB)
    file = upload["file"]
    sha = hashlib.sha256(PDB).hexdigest()
    assert file["name"] == "1abc.pdb" and file["media_type"] == "chemical/x-pdb"
    assert file["size"] == len(PDB) and file["sha256"] == sha
    assert file["project"]["id"] == str(world.project.pk) and not file["external"]
    assert file["created_by"]["email"] == "editor@example.com"
    # Only the checked content is kept: the staging object is gone.
    assert keys(s3) == [f"blobs/sha256/{sha[:2]}/{sha}"]
    event = AuditEvent.objects.get(action="file.added")
    assert event.target_id == file["id"] and event.project_id == world.project.pk
    assert event.actor_id == world.users["editor"].pk
    assert event.data == {"sha256": sha, "size": len(PDB), "media_type": "chemical/x-pdb"}


def test_large_files_go_in_parts(world: World, s3: Any, small_parts: int) -> None:
    data = bytes(range(256)) * (small_parts * 2 // 256) + b"tail"
    api = api_as(world)
    started = start(api, world, data).json()
    assert started["part_size"] == small_parts and started["part_count"] == 3
    assert [p["size"] for p in started["parts"]] == [small_parts, small_parts, 4]
    # Parts can go in any order.
    for part in reversed(started["parts"]):
        start_at = (part["number"] - 1) * small_parts
        chunk = data[start_at : start_at + part["size"]]
        assert requests.put(part["url"], data=chunk, timeout=30).status_code == 200
    assert api.post(f"/uploads/{started['id']}/complete").status_code == 202
    run_tasks()
    upload = api.get(f"/uploads/{started['id']}").json()
    assert upload["state"] == "ready" and upload["file"]["size"] == len(data)


def test_bytes_that_dont_match_the_hash_are_refused(world: World, s3: Any) -> None:
    api = api_as(world)
    started = start(api, world, PDB).json()
    send(started, PDB.replace(b"MET", b"ALA"))
    api.post(f"/uploads/{started['id']}/complete")
    run_tasks()
    upload = api.get(f"/uploads/{started['id']}").json()
    assert upload["state"] == "failed" and upload["file"] is None
    assert "don't match the SHA-256" in upload["error"]
    assert keys(s3) == []
    assert not File.objects.exists() and not Blob.objects.exists()


def test_the_same_content_is_stored_once(world: World, s3: Any) -> None:
    api = api_as(world)
    first = add(api, world, PDB)["file"]
    second = add(api_as(world, "admin"), world, PDB, name="copy.pdb")["file"]
    assert first["id"] != second["id"] and second["name"] == "copy.pdb"
    assert Blob.objects.count() == 1
    assert len(keys(s3)) == 1


def test_complete_needs_every_part_at_its_size(world: World, s3: Any, small_parts: int) -> None:
    data = b"x" * (small_parts + 10)
    api = api_as(world)
    started = start(api, world, data).json()
    first, second = started["parts"]
    requests.put(first["url"], data=data[:small_parts], timeout=30)
    body = problem(api.post(f"/uploads/{started['id']}/complete"), 409, "refused")
    assert body["detail"] == "Some parts haven't arrived yet: 2."
    requests.put(second["url"], data=b"short", timeout=30)
    body = problem(api.post(f"/uploads/{started['id']}/complete"), 409, "refused")
    assert "aren't the size this upload needs: 2" in body["detail"]
    requests.put(second["url"], data=data[small_parts:], timeout=30)
    assert api.post(f"/uploads/{started['id']}/complete").status_code == 202
    # Finishing again changes nothing.
    assert api.post(f"/uploads/{started['id']}/complete").json()["state"] == "checking"


def test_parts_can_be_signed_again(world: World, s3: Any, small_parts: int) -> None:
    data = b"y" * (small_parts + 1)
    api = api_as(world)
    started = start(api, world, data).json()
    again = api.post(f"/uploads/{started['id']}/parts", {"numbers": [2]})
    assert [p["number"] for p in again.json()] == [2] and again.json()[0]["size"] == 1
    assert len(api.post(f"/uploads/{started['id']}/parts", {}).json()) == 2
    problem(api.post(f"/uploads/{started['id']}/parts", {"numbers": [3]}), 409, "refused")


def test_an_empty_file_is_ready_at_once(world: World, s3: Any) -> None:
    response = start(api_as(world), world, b"", name="empty.txt", media_type="text/plain")
    assert response.status_code == 201, response.content
    assert response.json()["state"] == "ready" and response.json()["parts"] == []
    assert response.json()["file"]["size"] == 0
    wrong = start(api_as(world), world, b"", sha256="0" * 64)
    assert problem(wrong, 422, "validation_failed")["errors"][0]["pointer"] == "#/sha256"


def test_cancelling_deletes_what_arrived(world: World, s3: Any, small_parts: int) -> None:
    data = b"z" * (small_parts + 1)
    api = api_as(world)
    started = start(api, world, data).json()
    requests.put(started["parts"][0]["url"], data=data[:small_parts], timeout=30)
    assert api.delete(f"/uploads/{started['id']}").status_code == 204
    assert api.get(f"/uploads/{started['id']}").json()["state"] == "cancelled"
    assert api.post(f"/uploads/{started['id']}/complete").json()["state"] == "cancelled"
    problem(api.post(f"/uploads/{started['id']}/parts", {}), 409, "refused")
    assert keys(s3) == []


def test_abandoned_uploads_expire(world: World, s3: Any) -> None:
    api = api_as(world)
    started = start(api, world, PDB).json()
    Upload.objects.filter(pk=started["id"]).update(
        expires_at=Upload.objects.get().created_at - timedelta(seconds=1)
    )
    assert services.expire() == 1
    upload = api.get(f"/uploads/{started['id']}").json()
    assert upload["state"] == "expired" and upload["error"] == "It wasn't finished in time."
    assert services.expire() == 0


def test_names_and_media_types_are_cleaned_and_checked(world: World, s3: Any) -> None:
    api = api_as(world)
    started = start(api, world, PDB, name="../../etc/passwd", media_type="Chemical/X-PDB; x=1")
    assert started.json()["name"] == "passwd" and started.json()["media_type"] == "chemical/x-pdb"
    assert start(api, world, PDB, name="C:\\data\\1abc.pdb").json()["name"] == "1abc.pdb"
    for field, value in (("name", "../"), ("media_type", "pdb"), ("sha256", "g" * 64)):
        body = problem(start(api, world, PDB, **{field: value}), 422, "validation_failed")
        assert body["errors"][0]["pointer"] == f"#/{field}"


def test_uploads_are_limited_in_size(world: World, s3: Any, settings: Any) -> None:
    settings.IKI_FILES_MAX_UPLOAD_BYTES = 10
    body = problem(start(api_as(world), world, PDB), 422, "validation_failed")
    assert body["errors"] == [
        {"pointer": "#/size", "detail": "Files can be at most 10 bytes here."}
    ]


def test_part_sizes_keep_under_ten_thousand_parts() -> None:
    assert storage.part_size_for(1) == 64 * 1024**2
    big = 1024**4  # 1 TiB
    size = storage.part_size_for(big)
    assert storage.part_count(big, size) <= storage.MAX_PARTS and size % 1024**2 == 0


# --- Who sees what -----------------------------------------------------------------------------


def test_uploads_are_their_makers_and_files_their_projects(world: World, s3: Any) -> None:
    upload = add(api_as(world), world, PDB)
    file_id = upload["file"]["id"]
    problem(api_as(world, "admin").get(f"/uploads/{upload['id']}"), 404, "not_found")
    assert api_as(world, "admin").get("/uploads").json()["items"] == []
    for principal in ("viewer", "admin", "via_team"):
        assert api_as(world, principal).get(f"/files/{file_id}").status_code == 200
    for principal in ("outsider", "org_admin", "team_member"):
        problem(api_as(world, principal).get(f"/files/{file_id}"), 404, "not_found")
        problem(api_as(world, principal).get(f"/files/{file_id}/content"), 404, "not_found")
    listed = api_as(world, "viewer").get("/files", project=str(world.project.pk)).json()
    assert [f["id"] for f in listed["items"]] == [file_id]
    problem(start(api_as(world, "viewer"), world, PDB), 403, "forbidden")
    problem(start(api_as(world, "outsider"), world, PDB), 404, "not_found")


def test_downloads_redirect_to_a_short_lived_link_under_the_files_name(
    world: World, s3: Any
) -> None:
    upload = add(api_as(world), world, PDB, name="résumé 1abc.pdb")
    response = api_as(world, "viewer").get(f"/files/{upload['file']['id']}/content")
    assert response.status_code == 302 and response["Cache-Control"] == "no-store"
    fetched = requests.get(response["Location"], timeout=30)
    assert fetched.content == PDB
    assert fetched.headers["Content-Type"] == "chemical/x-pdb"
    assert fetched.headers["Content-Disposition"] == (
        "attachment; filename=\"r_sum_ 1abc.pdb\"; filename*=UTF-8''r%C3%A9sum%C3%A9%201abc.pdb"
    )


def test_without_storage_the_files_api_says_so(world: World) -> None:
    body = problem(start(api_as(world), world, PDB), 503, "storage_not_configured")
    assert "IKI_FILES_BUCKET" in body["detail"]


def test_files_can_be_listed_newest_first(world: World) -> None:
    from iki_server.files.models import Blob, File

    blob = Blob.objects.create(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    made = [
        str(
            File.objects.create(
                blob=blob, name=f"{n}.txt", media_type="text/plain", project=world.project
            ).pk
        )
        for n in range(3)
    ]
    viewer = api_as(world, "viewer")
    query = {"project": str(world.project.pk), "limit": 2}
    first = viewer.get("/files", newest_first="true", **query).json()
    assert [f["id"] for f in first["items"]] == made[:0:-1]
    rest = viewer.get("/files", newest_first="true", cursor=first["next_cursor"], **query).json()
    assert [f["id"] for f in rest["items"]] == made[:1]
    assert [f["id"] for f in viewer.get("/files", **query).json()["items"]] == made[:2]

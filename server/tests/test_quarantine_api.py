"""Reviewing scan evidence and releasing a false positive through the public API."""

import hashlib
from datetime import timedelta
from typing import Any

import pytest
from api_client import Api, problem
from django.utils import timezone
from org_world import World, build_world
from test_oauth import bearer, signed_in_token
from test_runs import world
from test_workers import queued

from iki_server.audit.models import AuditEvent
from iki_server.files import scan_state
from iki_server.files.models import Blob, File, Upload, UploadKind, UploadState
from iki_server.org.models import OrgRole, OrgRoleAssignment, Project
from iki_server.runs.models import Job, RunArtifact

pytestmark = pytest.mark.django_db
__all__ = ["world"]

#: A macro-enabled plate-reader export ClamAV's heuristics can mistake for a downloader.
MACRO = "Xls.Downloader.Agent-7372345-0"


def _sha256(n: int) -> str:
    return hashlib.sha256(f"held-{n}".encode()).hexdigest()


def _blob(n: int, size: int = 48_213) -> Blob:
    return Blob.objects.create(sha256=_sha256(n), size=size, storage_uri=f"s3://iki/{n}")


def _file(blob: Blob, name: str, project: Project, by: Any = None) -> File:
    return File.objects.create(
        blob=blob,
        name=name,
        media_type="application/vnd.ms-excel.sheet.macroenabled.12",
        project=project,
        created_by=by,
    )


def _infected(source: Blob | Upload, finding: str = MACRO) -> scan_state.ScanStatus:
    return scan_state.record_result(
        source, state="infected", scanner="ClamAV 1.4.3", signature_version="27453", reason=finding
    )


def test_review_and_release_only_the_affected_source() -> None:
    world = build_world()
    reviewer = Api(world.browser("quarantine_admin"))
    viewer = Api(world.browser("viewer"))
    sha = "a" * 64
    blob = Blob.objects.create(sha256=sha, size=15, storage_uri="s3://example/private")
    file = File.objects.create(
        blob=blob,
        name="lab.txt",
        media_type="text/plain",
        project=world.project,
        created_by=world.users["editor"],
    )
    path = f"/files/{file.pk}"
    pending = viewer.get(path).json()["scan"]
    assert (pending["state"], pending["usable"], pending["decision_id"]) == ("pending", False, None)

    # Another source of the same bytes has its own pinned version and decision.
    other = Blob.objects.create(
        registered_external=True,
        storage_uri="s3://example/other",
        object_version="1",
        sha256=sha,
        size=15,
    )
    for source in (other, blob):
        scan_state.record_result(
            source,
            state="infected",
            scanner="fixture",
            signature_version="10",
            reason="fixture-finding",
        )
    scan = viewer.get(path).json()["scan"]
    assert (scan["state"], scan["reason"], scan["usable"]) == ("infected", "fixture-finding", False)
    assert viewer.get("/files").json()["items"][0]["scan"] == scan
    assert reviewer.get(path).status_code == 404
    assert reviewer.get("/me").json()["can"]["manage_quarantine"]
    first = reviewer.get("/quarantine-decisions", limit=1).json()
    (held,) = first["items"]
    assert {k: v for k, v in held.items() if k != "files"} == {
        "id": scan["decision_id"],
        "state": "infected",
        "scanner": "fixture",
        "signature_version": "10",
        "scanned_at": scan["scanned_at"],
        "reason": "fixture-finding",
        "sha256": sha,
        "size": 15,
        "releasable": True,
        "release": None,
        "file_count": 1,
    }
    # Where it is, and who added it, but no way into a project the reviewer can't read.
    (copy,) = held["files"]
    assert copy["kind"] == "file" and copy["file"] is None
    assert (copy["name"], copy["project"]["name"]) == ("lab.txt", "Kinase Atlas")
    assert copy["created_by"]["email"] == "editor@example.com"
    next_page = reviewer.get("/quarantine-decisions", cursor=first["next_cursor"]).json()
    assert len(next_page["items"]) == 1 and next_page["next_cursor"] is None
    assert next_page["items"][0]["files"] == [] and next_page["items"][0]["file_count"] == 0
    assert first["items"][0]["id"] != next_page["items"][0]["id"]
    release = f"/quarantine-decisions/{scan['decision_id']}/release"
    reason = "Compared the file with the instrument's signed original."
    problem(viewer.post(release, {"reason": reason}), 403, "forbidden")
    problem(reviewer.post(release, {"reason": "  "}), 422, "validation_failed")
    approved = reviewer.post(release, {"reason": reason})
    assert approved.status_code == 200, approved.content
    released = approved.json()
    assert (released["releasable"], released["release"]["reason"]) == (False, reason)
    assert released["release"]["by"]["email"] == "quarantine_admin@example.com"
    assert viewer.get(path).json()["scan"] == {**scan, "usable": True, "released": True}
    assert reviewer.get("/quarantine-decisions").json() == next_page
    assert reviewer.get("/quarantine-decisions", released=True).json()["items"] == [released]
    assert reviewer.get(f"/quarantine-decisions/{scan['decision_id']}").json() == released
    assert reviewer.get(path).status_code == 404
    problem(reviewer.post(release, {"reason": reason}), 409, "conflict")
    event = AuditEvent.objects.get(action="file.quarantine_released")
    assert event.actor_id == world.users["quarantine_admin"].pk
    assert event.data["reason"] == reason and event.data["decision"] == scan["decision_id"]


def test_a_held_source_lists_each_file_that_shares_it() -> None:
    """A list shows the first three places and how many there are; the decision's own page
    every one. A reviewer who can also read a project gets its files' IDs, to open them."""
    world = build_world()
    reviewer = world.users["viewer"]
    OrgRoleAssignment.objects.create(user=reviewer, role=OrgRole.QUARANTINE_ADMIN)
    api = Api(world.browser("viewer"))
    elsewhere = Project.objects.create(team=world.other_team, name="Antibody Panel")
    blob = _blob(1)
    names = [f"EnVision_plate-0{n}.xlsm" for n in range(1, 5)]
    made = [
        _file(blob, name, world.project if n < 2 else elsewhere, world.users["editor"])
        for n, name in enumerate(names)
    ]
    decision = _infected(blob).decision_id

    (listed,) = api.get("/quarantine-decisions").json()["items"]
    assert listed["file_count"] == 4
    assert [c["name"] for c in listed["files"]] == names[:3]
    assert [c["file"] for c in listed["files"]] == [str(made[0].pk), str(made[1].pk), None]
    every = api.get(f"/quarantine-decisions/{decision}").json()
    assert [c["name"] for c in every["files"]] == names
    assert [c["project"]["name"] for c in every["files"]] == ["Kinase Atlas"] * 2 + [
        "Antibody Panel"
    ] * 2


def test_the_list_filters_by_what_was_found_by_name_and_by_hash() -> None:
    world = build_world()
    api = Api(world.browser("quarantine_admin"))
    macro, eicar, large, encrypted = (_blob(n) for n in range(10, 14))
    _file(macro, "EnVision_plate-07.xlsm", world.project)
    _file(eicar, "eicar-test.com", world.project)
    _file(large, "NovaSeq_221103_A01234.tar", world.project)
    _file(encrypted, "sponsor-transfer.zip", world.project)
    _infected(macro)
    _infected(eicar, "Win.Test.EICAR_HDB-1")
    scan_state.record_result(large, state="not_scanned", scanner="ClamAV", reason="too_large")
    scan_state.record_result(
        encrypted,
        state="not_scanned",
        scanner="ClamAV 1.4.3",
        signature_version="27453",
        reason="Heuristics.Encrypted.Zip",
    )

    def found(**query: Any) -> list[str]:
        response = api.get("/quarantine-decisions", **query)
        assert response.status_code == 200, response.content
        return [item["files"][0]["name"] for item in response.json()["items"]]

    assert found() == [
        "sponsor-transfer.zip",
        "NovaSeq_221103_A01234.tar",
        "eicar-test.com",
        "EnVision_plate-07.xlsm",
    ]
    assert found(state="infected") == ["eicar-test.com", "EnVision_plate-07.xlsm"]
    assert found(state="not_scanned") == ["sponsor-transfer.zip", "NovaSeq_221103_A01234.tar"]
    assert found(q="plate-07") == ["EnVision_plate-07.xlsm"]
    assert found(q=_sha256(11)[:12].upper()) == ["eicar-test.com"]
    assert found(q="ENVISION", state="not_scanned") == []
    assert found(released=True) == []
    problem(api.get("/quarantine-decisions", released="clean"), 422, "validation_failed")


def test_an_incomplete_inspection_is_held_but_never_released() -> None:
    """It found nothing to call false (ADR 0217): it stays held, and says so."""
    world = build_world()
    api = Api(world.browser("quarantine_admin"))
    blob = _blob(20, size=412_000_000_000)
    _file(blob, "NovaSeq_221103_A01234.tar", world.project)
    held = scan_state.record_result(blob, state="not_scanned", scanner="ClamAV", reason="too_large")
    entry = api.get(f"/quarantine-decisions/{held.decision_id}").json()
    assert (entry["state"], entry["reason"], entry["releasable"]) == (
        "not_scanned",
        "too_large",
        False,
    )
    refused = api.post(
        f"/quarantine-decisions/{held.decision_id}/release", {"reason": "It's ours."}
    )
    problem(refused, 409, "conflict")
    assert not AuditEvent.objects.filter(action="file.quarantine_released").exists()

    # A clean decision, or an exception the size policy allows, was never quarantined.
    clean = scan_state.record_result(
        _blob(21), state="clean", scanner="ClamAV", signature_version="27453"
    )
    allowed = scan_state.record_result(
        _blob(22), state="not_scanned", scanner="ClamAV", reason="too_large", allow_unscanned=True
    )
    for decision in (clean, allowed):
        problem(api.get(f"/quarantine-decisions/{decision.decision_id}"), 404, "not_found")
    assert [i["id"] for i in api.get("/quarantine-decisions").json()["items"]] == [
        str(held.decision_id)
    ]


def test_a_runs_held_artifact_names_its_run(world: World) -> None:
    run = queued(world)
    job = Job.objects.get(run_id=run["id"])
    upload = Upload.objects.create(
        project=world.project,
        created_by=world.users["runner"],
        kind=UploadKind.ARTIFACT,
        name="docking-poses.sdf",
        media_type="chemical/x-mdl-sdfile",
        sha256="b" * 64,
        size=2_048,
        part_size=2_048,
        expires_at=timezone.now() + timedelta(days=30),
        state=UploadState.READY,
    )
    RunArtifact.objects.create(
        run_id=run["id"], job=job, upload=upload, path="poses.sdf", expires_at=upload.expires_at
    )
    decision = _infected(upload, "Win.Trojan.Generic-9876543-0").decision_id
    api = Api(world.browser("quarantine_admin"))
    entry = api.get(f"/quarantine-decisions/{decision}").json()
    (copy,) = entry["files"]
    assert (copy["kind"], copy["file"], copy["run"]) == ("artifact", None, run["hrid"])
    assert (copy["name"], copy["created_by"]["email"]) == (
        "docking-poses.sdf",
        "runner@example.com",
    )
    assert (entry["sha256"], entry["size"], entry["file_count"]) == ("b" * 64, 2_048, 1)


def test_a_quarantine_admins_token_needs_the_admin_scope() -> None:
    world = build_world()
    _, tokens = signed_in_token(world, "quarantine_admin", scope="read")
    problem(
        bearer(tokens["access_token"], "GET", "/quarantine-decisions"), 403, "insufficient_scope"
    )

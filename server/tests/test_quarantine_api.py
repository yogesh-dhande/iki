"""Reviewing scan evidence and releasing a false positive through the public API."""

import pytest
from api_client import Api, problem
from org_world import build_world
from test_oauth import bearer, signed_in_token

from iki_server.audit.models import AuditEvent
from iki_server.files import scan_state
from iki_server.files.models import Blob, File

pytestmark = pytest.mark.django_db


def test_review_and_release_only_the_affected_source() -> None:
    world = build_world()
    reviewer = Api(world.browser("quarantine_admin"))
    viewer = Api(world.browser("viewer"))
    sha = "a" * 64
    blob = Blob.objects.create(sha256=sha, size=15, storage_uri="s3://example/private")
    file = File.objects.create(
        blob=blob, name="lab.txt", media_type="text/plain", project=world.project
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
    first = reviewer.get("/quarantine", limit=1).json()
    assert first["items"] == [
        {
            "id": scan["decision_id"],
            "state": "infected",
            "scanner": "fixture",
            "signature_version": "10",
            "scanned_at": scan["scanned_at"],
            "reason": "fixture-finding",
            "sha256": sha,
            "size": 15,
        }
    ]
    next_page = reviewer.get("/quarantine", cursor=first["next_cursor"]).json()
    assert len(next_page["items"]) == 1 and next_page["next_cursor"] is None
    assert first["items"][0]["id"] != next_page["items"][0]["id"]
    release = f"/quarantine/{scan['decision_id']}/release"
    reason = "Compared the file with the instrument's signed original."
    problem(viewer.post(release, {"reason": reason}), 403, "forbidden")
    problem(reviewer.post(release, {"reason": "  "}), 422, "validation_failed")
    approved = reviewer.post(release, {"reason": reason})
    assert approved.status_code == 200, approved.content
    assert approved.json() == {**scan, "usable": True, "released": True}
    assert viewer.get(path).json()["scan"] == approved.json()
    assert reviewer.get("/quarantine").json() == next_page
    assert reviewer.get(path).status_code == 404
    problem(reviewer.post(release, {"reason": reason}), 409, "conflict")
    event = AuditEvent.objects.get(action="file.quarantine_released")
    assert event.actor_id == world.users["quarantine_admin"].pk
    assert event.data["reason"] == reason and event.data["decision"] == scan["decision_id"]


def test_a_quarantine_admins_token_needs_the_admin_scope() -> None:
    world = build_world()
    _, tokens = signed_in_token(world, "quarantine_admin", scope="read")
    problem(bearer(tokens["access_token"], "GET", "/quarantine"), 403, "insufficient_scope")

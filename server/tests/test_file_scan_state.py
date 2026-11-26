"""A scan decision belongs to its exact source and retains its evidence for good."""

import pytest
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone
from org_world import build_world

from iki_server.accounts.models import User
from iki_server.api.problems import Problem
from iki_server.audit import log
from iki_server.audit.models import AuditEvent
from iki_server.core.db_roles import append_only_tables
from iki_server.files import scan_state
from iki_server.files.models import Blob, BlobScan, ScanRelease, Upload, UploadKind
from iki_server.org.models import OrgRole, OrgRoleAssignment

pytestmark = pytest.mark.django_db


def registered(version: str) -> Blob:
    return Blob.objects.create(
        sha256="0" * 64,
        size=10,
        storage_uri="s3://instrument/sample.dat",
        object_version=version,
        registered_external=True,
    )


def reviewer() -> User:
    user = User.objects.create_user("reviewer@example.org", "Reviewer")
    OrgRoleAssignment.objects.create(user=user, role=OrgRole.QUARANTINE_ADMIN)
    return user


def test_decisions_stay_with_the_verified_source() -> None:
    first, second = registered("v1"), registered("v2")
    clean = scan_state.record_result(first, state="clean", scanner="clamd", signature_version="7")
    pending = scan_state.status_for(second)
    assert clean.usable and clean.scanned_at and pending.state == "pending"
    assert scan_state.statuses_for_blobs([first.pk, second.pk]) == {
        first.pk: clean,
        second.pk: pending,
    }
    with pytest.raises(Problem) as error:
        pending.require_usable()
    assert error.value.code == "file_scanning"
    for _ in range(2):
        decision = scan_state.record_result(
            second, state="infected", scanner="clamd", signature_version="8", reason="Test.Finding"
        )
    assert not decision.usable and decision.decision_id
    assert BlobScan.objects.count() == 2
    assert AuditEvent.objects.get(action="file.quarantined").data["finding"] == "Test.Finding"
    assert list(scan_state.held().values_list("id", flat=True)) == [decision.decision_id]


def test_release_preserves_the_finding_and_its_actor_and_reason() -> None:
    source = registered("v1")
    decision = scan_state.record_result(
        source, state="infected", scanner="clamd", signature_version="1", reason="Test.Finding"
    )
    assert decision.decision_id
    by = reviewer()
    given = "Reviewed the raw instrument data against the acquisition log."
    released = scan_state.release(decision.decision_id, actor=by, reason=given)
    assert released.released and released.usable and released.state == "infected"
    assert released.reason == "Test.Finding" and released.scanned_at == decision.scanned_at
    assert scan_state.status_for(source) == released
    assert not scan_state.held().exists()
    assert ScanRelease.objects.get().actor == by
    event = AuditEvent.objects.get(action="file.quarantine_released")
    assert event.target_id == str(decision.decision_id) and event.actor_id == by.pk
    assert event.data["reason"] == given and event.data["finding"] == released.reason
    with pytest.raises(Problem) as error:
        scan_state.release(decision.decision_id, actor=by, reason="Second review")
    assert error.value.status == 409
    assert AuditEvent.objects.filter(action="file.quarantine_released").count() == 1
    assert {"iki_blob_scan", "iki_scan_release"} <= set(append_only_tables())
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        BlobScan.objects.update(state="clean")
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        ScanRelease.objects.update(reason="Changed later")


@pytest.mark.parametrize("allow", [False, True])
def test_size_policy_is_preserved_on_incomplete_inspections(*, allow: bool) -> None:
    source = registered("v1")
    decision = scan_state.record_result(
        source, state="not_scanned", scanner="clamd", reason="too_large", allow_unscanned=allow
    )
    assert decision.state == "not_scanned" and decision.usable == allow
    assert decision.decision_id and scan_state.held().exists() != allow
    assert AuditEvent.objects.get(action="file.not_scanned").data["allow_unscanned"] == allow
    by = reviewer()
    with pytest.raises(Problem) as error:
        scan_state.release(decision.decision_id, actor=by, reason="Review")
    assert error.value.status == 409
    with pytest.raises(IntegrityError, match="only an infected scan"), transaction.atomic():
        ScanRelease.objects.create(decision_id=decision.decision_id, actor=by, reason="Review")


def test_only_the_oversize_policy_can_allow_incomplete_evidence() -> None:
    with pytest.raises(IntegrityError, match="iki_scan_size_exception"), transaction.atomic():
        scan_state.record_result(
            registered("v1"),
            state="not_scanned",
            scanner="clamd",
            reason="encrypted",
            allow_unscanned=True,
        )
    assert not BlobScan.objects.exists() and not AuditEvent.objects.exists()


def test_a_failure_to_audit_cannot_release_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    source = registered("v1")
    decision = scan_state.record_result(
        source, state="infected", scanner="clamd", signature_version="1", reason="Test.Finding"
    )
    assert decision.decision_id
    by = reviewer()

    def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Audit unavailable")

    monkeypatch.setattr(log, "record", fail_audit)
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        scan_state.release(decision.decision_id, actor=by, reason="Verified fixture")
    assert scan_state.status_for(source) == decision
    assert not ScanRelease.objects.exists()


def test_ephemeral_uploads_keep_independent_evidence() -> None:
    world = build_world()
    uploads = [
        Upload.objects.create(
            project=world.project,
            created_by=world.users["runner"],
            kind=UploadKind.ARTIFACT,
            name="metrics.txt",
            media_type="text/plain",
            sha256="0" * 64,
            size=20,
            part_size=20,
            expires_at=timezone.now(),
        )
        for _ in range(3)
    ]
    clean, held, pending = uploads
    scan_state.record_result(clean, state="clean", scanner="test", signature_version="1")
    finding = scan_state.record_result(
        held, state="infected", scanner="test", signature_version="1", reason="Test.Finding"
    )
    assert list(scan_state.usable_upload_ids()) == [clean.pk]
    by_id = scan_state.statuses_for_uploads(u.pk for u in uploads)
    assert [by_id[u.pk].state for u in uploads] == ["clean", "infected", "pending"]
    assert finding.decision_id
    scan_state.release(finding.decision_id, actor=world.users["quarantine_admin"], reason="Review")
    assert set(scan_state.usable_upload_ids()) == {clean.pk, held.pk}
    assert not scan_state.status_for(pending).usable

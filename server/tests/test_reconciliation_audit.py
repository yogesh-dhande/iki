"""The reconciliation audit and each saved movement explain the same count (ADR 0180)."""

import pytest
from api_client import Api, seen
from org_world import build_world
from test_storage import box, contents, moved, tube

from iki_server.audit.models import AuditEvent
from iki_server.storage.models import Event, ObservationEvent


@pytest.mark.django_db
def test_reconciliation_preserves_the_evidence_for_each_decision() -> None:
    world = build_world()
    api = Api(world.browser("editor"))
    container = box(api, world.project, rows=2, columns=4)
    swapped, other, missing, unscanned, kept, extra = [
        tube(api, world.project, code)
        for code in ("SWAP-1", "SWAP-2", "MISS", "UNSEEN", "KEEP", "EXTRA")
    ]
    moved(
        api,
        (swapped, container, "A1"),
        (other, container, "A2"),
        (missing, container, "A3"),
        (unscanned, container, "A4"),
        (kept, container, "B2"),
    )
    started = api.post(f"/storage/containers/{container['id']}/reconciliations")
    assert started.status_code == 201, started.content
    path = f"/reconciliations/{started.json()['id']}"
    scans = [
        {"position": "A1", "code": "SWAP-2"},
        {"position": "A2", "code": "SWAP-1"},
        {"position": "A3", "code": None},
        {"position": "B1", "code": "EXTRA"},
        {"position": "B2", "code": None},
    ]
    scanned = api.post(f"{path}/scans", {"scans": scans})
    assert scanned.status_code == 200, scanned.content
    lines = {line["code"]: line for line in scanned.json()["lines"]}
    for code, decision in [
        ("SWAP-1", "moved"),
        ("SWAP-2", "moved"),
        ("MISS", "removed"),
        ("KEEP", "kept"),
        ("EXTRA", "placed"),
    ]:
        line = lines[code]
        saved = api.patch(
            f"{path}/lines/{line['id']}",
            {
                "resolution": decision,
                "note": "Inventory check",
            },
            headers=seen(line["revision"]),
        )
        assert saved.status_code == 200, saved.content
    reviewed = api.get(path)
    assert reviewed.status_code == 200, reviewed.content
    revision = reviewed.json()["revision"]
    done = api.post(f"{path}/complete", {"missing_rest": True}, headers=seen(revision))
    assert done.status_code == 200, done.content
    recorded = ObservationEvent.objects.filter(reconciliation_id=started.json()["id"])
    operator = str(world.users["editor"].pk)
    assert set(recorded.filter(kind="scan").values_list("after__scanned_by", flat=True)) == {
        operator
    }
    # Includes both the explicitly reviewed decisions and the one marked missing on completion.
    assert set(recorded.filter(kind="resolution").values_list("after__resolved_by", flat=True)) == {
        operator
    }
    assert contents(api, container) == {
        "A1": other["hrid"],
        "A2": swapped["hrid"],
        "B1": extra["hrid"],
        "B2": kept["hrid"],
    }

    audit = AuditEvent.objects.get(action="storage.reconciled", target_id=container["id"])
    assert audit.data["revision"] == revision
    assert audit.data["findings"] == {
        "matched": 0,
        "missing": 2,
        "unexpected": 1,
        "wrong_slot": 2,
        "unchecked": 1,
    }
    events = {str(e.entity_id): e for e in Event.objects.filter(operation=audit.data["operation"])}
    assert audit.data["moved"] == len(events) == 5
    assert audit.data["resolutions"] == {"moved": 2, "removed": 2, "placed": 1, "kept": 1}
    assert audit.data["assumed_missing"] == 1
    for record, kind, position, evidence in [
        (swapped, "moved", "A2", "scanned at"),
        (other, "moved", "A1", "scanned at"),
        (extra, "placed", "B1", "scanned at"),
        (missing, "removed", "A3", "scan did not find this record"),
        (unscanned, "removed", "A4", "not scanned"),
    ]:
        event = events[record["id"]]
        assert event.kind == kind
        assert position in event.reason and evidence in event.reason.lower()
        logged = AuditEvent.objects.get(
            action="storage.moved", target_id=record["id"], data__operation=audit.data["operation"]
        )
        assert logged.data["reason"] == event.reason

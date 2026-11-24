"""Receipt and reconciliation history follows the parent's and linked records' current access."""

import uuid
from typing import Any

import pytest
from api_client import Api
from django.utils import timezone
from org_world import World, build_world
from test_api_access import _assay_request, _box
from test_storage import tube

from iki_server.entities.models import Entity
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.storage import observations
from iki_server.storage.models import ObservationEvent, Receipt, Reconciliation

pytestmark = pytest.mark.django_db


def parent(world: World, collection: str) -> Receipt | Reconciliation:
    editor = world.users["editor"]
    if collection == "receipts":
        return Receipt.objects.create(
            request_id=_assay_request(world).pk, project=world.project, started_by=editor
        )
    return Reconciliation.objects.create(container=_box(world), started_by=editor)


@pytest.mark.parametrize("collection", ["receipts", "reconciliations"])
def test_observation_history_uses_current_access_even_after_a_line_disappears(
    collection: str,
) -> None:
    world = build_world()
    other = build_world(prefix="other-")
    workflow = parent(world, collection)
    elsewhere = parent(other, collection)
    path = f"/{collection}/{workflow.pk}/history"
    other_path = f"/{collection}/{elsewhere.pk}/history"
    editor, viewer = (Api(world.browser(who)) for who in ("editor", "viewer"))
    private = Project.objects.create(team=world.team, name="Private samples")
    ProjectMembership.objects.create(
        project=private, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    grant = ProjectMembership.objects.create(
        project=private, user=world.users["viewer"], role=ProjectRole.VIEWER
    )
    record = tube(editor, private, "PRIVATE-TUBE-01")
    linked = {"entity_id": record["id"], "code": "PRIVATE-TUBE-01"}
    line = workflow.lines.create(
        entity_id=record["id"],
        code=linked["code"],
        **({"expected": False} if collection == "receipts" else {}),
    )
    line_id = line.pk
    kind = "outcome" if collection == "receipts" else "scan"

    def observed(
        owner: Receipt | Reconciliation,
        line_id: uuid.UUID,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        revision: int = 1,
    ) -> ObservationEvent:
        return observations.record(
            owner,
            line_id=line_id,
            kind=kind,
            user=world.users["editor"],
            operation=uuid.uuid4(),
            revision=revision,
            before=before,
            after=after,
            reason="Recorded at the bench",
        )

    opened = observations.record(
        workflow,
        line_id=None,
        kind="status",
        user=world.users["editor"],
        operation=uuid.uuid4(),
        revision=1,
        before=None,
        after={"status": "open"},
        reason="",
    )
    first = observed(workflow, line_id, None, linked)
    unrelated_line = uuid.uuid4()
    unknown = observed(workflow, unrelated_line, None, {"code": "UNREGISTERED"})
    latest = observed(workflow, line_id, linked, None, revision=2)
    foreign = observed(elsewhere, uuid.uuid4(), None, {"code": "ANOTHER-PROJECT"})
    line.delete()

    # A project reader could see this tube when the observation was made. Revoking
    # access redacts the same old observation, even though its line no longer exists.
    assert viewer.get(path, line_id=line_id).json()["items"][0]["before"] == {
        "code": "PRIVATE-TUBE-01"
    }
    grant.delete()
    for api, code in ((editor, "PRIVATE-TUBE-01"), (viewer, "")):
        response = api.get(path)
        assert response.status_code == 200, response.content
        page = response.json()
        assert page["next_cursor"] is None
        newest, unlinked, oldest, status = page["items"]
        assert [i["id"] for i in page["items"]] == [
            str(r.pk) for r in (latest, unknown, first, opened)
        ]
        assert status["line_id"] is None and status["after"] == {"status": "open"}
        assert (newest["before"], newest["after"]) == ({"code": code}, None)
        assert (oldest["before"], oldest["after"]) == (None, {"code": code})
        assert unlinked["after"] == {"code": "UNREGISTERED"}
        assert newest["previous"] == str(first.pk) and oldest["previous"] is None
        assert (newest["revision"], newest["kind"], newest["line_id"]) == (2, kind, str(line_id))
        assert newest["receipt_id"] == (str(workflow.pk) if collection == "receipts" else None)
        assert newest["reconciliation_id"] == (
            str(workflow.pk) if collection == "reconciliations" else None
        )
        assert newest["actor"]["id"] == str(world.users["editor"].pk)
        assert newest["created_at"] and newest["operation"] == str(latest.operation)
        assert newest["reason"] == "Recorded at the bench"
        assert record["id"].encode() not in response.content
    assert b"PRIVATE-TUBE-01" not in viewer.get(path).content

    filtered = viewer.get(path, line_id=line_id, limit=1).json()
    assert [i["id"] for i in filtered["items"]] == [str(latest.pk)]
    assert filtered["next_cursor"]
    public_tube = tube(editor, world.project, "SHARED-TUBE-02")
    shared = {"code": "SHARED-TUBE-02", "entity_id": public_tube["id"]}
    observed(workflow, line_id, shared, linked, revision=3)
    amendment = viewer.get(path, line_id=line_id, limit=1).json()["items"][0]
    assert amendment["before"] == {"code": "SHARED-TUBE-02"} and amendment["after"] == {"code": ""}
    tail = viewer.get(path, line_id=line_id, limit=1, cursor=filtered["next_cursor"]).json()
    assert [i["id"] for i in tail["items"]] == [str(first.pk)]
    assert tail["next_cursor"] is None

    assert viewer.get(path, line_id=foreign.line_id).json() == {"items": [], "next_cursor": None}
    assert viewer.get(other_path, line_id=line_id).status_code == 404
    assert Api(world.browser("outsider")).get(path, line_id=line_id).status_code == 404
    # The reference resolver still names a purged record's HRID and type.
    Entity.objects.filter(pk=record["id"]).update(purged_at=timezone.now())
    assert editor.get(path, line_id=line_id).json()["items"][0]["after"] == {"code": ""}

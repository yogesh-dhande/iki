"""A scan preview identifies readable material without changing its physical history."""

from typing import Any
from uuid import uuid4

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_requests import Lab
from test_storage import BOX, box, made, moved, receive, sent_with_tubes, tube
from test_storage import lab as lab

from iki_server.audit.models import AuditEvent
from iki_server.storage.models import Event, ObservationEvent, Place

pytestmark = pytest.mark.django_db

PREVIEW = "/storage/scan/resolve"


@pytest.fixture
def world() -> World:
    return build_world()


def preview(api: Api, **body: Any) -> dict[str, Any]:
    answer = api.post(PREVIEW, body)
    assert answer.status_code == 200, answer.content
    data = answer.json()
    assert isinstance(data, dict)
    return data


def test_box_preview_preserves_colliding_identities_and_their_readable_locations(
    world: World,
) -> None:
    editor = Api(world.browser("editor"))
    reader = Api(world.browser("viewer"))
    cryo = box(editor, world.project)
    printed = tube(editor, world.project, "TUBE-LABEL-1")
    labelled = tube(editor, world.project, printed["hrid"])
    moved(editor, (printed, cryo, "A1"), (labelled, cryo, "A2"))
    before = [model.objects.count() for model in (AuditEvent, Event, ObservationEvent)]
    query = {"scope": "box", "container": cryo["id"], "code": printed["hrid"]}
    scan = preview(reader, **query)
    assert scan["status"] == "ambiguous"
    candidates = {item["id"]: item for item in scan["candidates"]}
    assert candidates.keys() == {printed["id"], labelled["id"]}
    for record, position in [(printed, "A1"), (labelled, "A2")]:
        candidate = candidates[record["id"]]
        assert candidate["hrid"] == record["hrid"] and candidate["type"] == record["type"]
        assert candidate["owner_project"] == {
            "id": str(world.project.pk),
            "name": world.project.name,
        }
        assert candidate["box"] == {"id": cryo["id"], "hrid": cryo["hrid"], "title": "Box 1"}
        assert candidate["position"] == position
    assert preview(reader, **query, mode="iki_id") == {
        "status": "one",
        "candidates": [candidates[printed["id"]]],
    }
    assert preview(reader, **query, choice=labelled["id"]) == {
        "status": "one",
        "candidates": [candidates[labelled["id"]]],
    }
    assert preview(reader, **query, position="a02") == {
        "status": "one",
        "candidates": [candidates[labelled["id"]]],
    }
    assert preview(reader, **query, position="A3") == {"status": "none", "candidates": []}
    assert [model.objects.count() for model in (AuditEvent, Event, ObservationEvent)] == before


def test_position_of_an_unreadable_occupant_has_one_response_for_every_scan(world: World) -> None:
    other = build_world("Other")
    editor = Api(world.browser("editor"))
    reader = Api(world.browser("viewer"))
    cryo = box(editor, world.project)
    public = tube(editor, world.project, "SHARED-TUBE")
    hidden = tube(Api(other.browser("editor")), other.project, "OTHER-LABEL")
    moved(editor, (public, cryo, "A1"))
    Place.objects.create(entity_id=hidden["id"], container_id=cryo["id"], row=1, column=2)
    body = {"scope": "box", "container": cryo["id"], "position": "A2"}
    for scan in (
        {"code": ""},
        {"code": "OTHER-LABEL"},
        {"code": "UNASSIGNED"},
        {"code": public["hrid"], "mode": "iki_id"},
        {"code": f"/entities/{public['id']}/"},
        {"code": public["hrid"], "choice": public["id"]},
    ):
        error = problem(reader.post(PREVIEW, {**body, **scan}), 409, "scan_unavailable")
        assert error["detail"] == (
            "This scan can’t identify a tube here. "
            "Use its printed Iki ID or ask an authorized reviewer."
        )
        assert "candidates" not in error
    visible = preview(reader, scope="box", container=cryo["id"], position="A1", code="SHARED-TUBE")
    assert visible["status"] == "one" and visible["candidates"][0]["id"] == public["id"]
    body.pop("position")
    for code in ("OTHER-LABEL", "UNASSIGNED"):
        problem(reader.post(PREVIEW, {**body, "code": code}), 409, "scan_unavailable")


def test_destination_preview_limits_matches_to_storage_containers(world: World) -> None:
    api = Api(world.browser("editor"))
    shared = "DEST-525"
    container = made(
        api, world.project, BOX, {"name": "Red box", "rows": 2, "columns": 2, "barcode": shared}
    )
    material = tube(api, world.project, shared)
    scan = preview(api, scope="destination", code=shared)
    assert scan["status"] == "one"
    assert [c["id"] for c in scan["candidates"]] == [container["id"]]
    assert preview(api, scope="destination", code=container["id"]) == scan
    problem(
        api.post(PREVIEW, {"scope": "destination", "code": f"/entities/{material['id']}/"}),
        409,
        "scan_unavailable",
    )


def test_confirmation_checks_the_target_against_all_source_occupants(world: World) -> None:
    api = Api(world.browser("editor"))
    cryo = box(api, world.project)
    target = tube(api, world.project, "MY-LABEL")
    other = tube(api, world.project, target["hrid"])
    moved(api, (target, cryo, "A1"), (other, cryo, "A2"))
    body = {"scope": "confirmation", "record": target["id"]}
    result = preview(api, **body, code=target["hrid"])
    assert result["status"] == "ambiguous"
    assert {c["id"] for c in result["candidates"]} == {target["id"], other["id"]}
    explicit = preview(api, **body, code=target["hrid"], mode="iki_id")
    assert explicit["status"] == "one" and explicit["candidates"][0]["id"] == target["id"]
    problem(
        api.post(PREVIEW, {**body, "code": other["hrid"], "mode": "iki_id"}),
        409,
        "scan_unavailable",
    )
    problem(
        api.post(PREVIEW, {**body, "code": target["hrid"], "choice": target["id"]}),
        409,
        "scan_unavailable",
    )

    other_box = box(api, world.project, "Spare")
    moved(api, (other, other_box, "A1"))
    assert preview(api, **body, code=target["hrid"])["status"] == "one"
    assert (
        preview(api, **body, code=target["hrid"], records=[target["id"], other["id"]])["status"]
        == "ambiguous"
    )
    problem(
        api.post(PREVIEW, {**body, "code": "MY-LABEL", "records": [other["id"]]}),
        422,
        "validation_failed",
    )


def test_unreadable_and_absent_contexts_cannot_be_distinguished(world: World) -> None:
    other = build_world("Other")
    private = box(Api(other.browser("editor")), other.project)
    api = Api(world.browser("viewer"))
    for reference in (private["id"], str(uuid4())):
        response = api.post(PREVIEW, {"scope": "box", "container": reference, "code": "X"})
        assert response.status_code == 404


def test_confirming_a_readable_tube_kept_in_a_private_box_needs_independent_evidence(
    lab: Lab,
) -> None:
    cryo = box(lab.api("tech"), lab.core, "Incoming")
    request, (record,) = sent_with_tubes(lab, 1)
    receive(lab, request, put={record["id"]: (cryo, "A1")})
    reader = lab.api("editor")
    body = {"scope": "confirmation", "record": record["id"]}
    for code in ("RC000", "OTHER-LABEL"):
        failure = reader.post(PREVIEW, {**body, "code": code})
        problem(failure, 409, "scan_unavailable")
        assert cryo["id"].encode() not in failure.content and b"Incoming" not in failure.content
    for proof in (
        {"code": record["hrid"], "mode": "iki_id"},
        {"code": f"/entities/{record['id']}/"},
    ):
        result = preview(reader, **body, **proof)
        (item,) = result["candidates"]
        assert result["status"] == "one" and item["id"] == record["id"]
        assert item["box"] is None and item["position"] is None

"""A scan preview identifies readable material without changing its physical history."""

from typing import Any
from uuid import uuid4

import pytest
from api_client import Api, problem
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import World, build_world
from test_requests import Lab
from test_storage import BOX, box, made, moved, receive, sent_with_tubes, tube
from test_storage import lab as lab

from iki_server.audit.models import AuditEvent
from iki_server.storage.grids import Shape
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


def test_one_scan_identifies_a_record_in_the_selection_and_checks_its_sources(world: World) -> None:
    api = Api(world.browser("editor"))
    first_box, second_box = box(api, world.project), box(api, world.project, "Spare")
    first = tube(api, world.project, "FIRST")
    second = tube(api, world.project, "SECOND")
    # A third tube in the other source box carries the first tube's printed HRID as vendor text.
    peer = tube(api, world.project, first["hrid"])
    selection = {"scope": "confirmation", "records": [first["id"], second["hrid"]]}
    unplaced = preview(api, **selection, code=first["hrid"])
    assert unplaced["status"] == "ambiguous"
    assert {c["id"] for c in unplaced["candidates"]} == {first["id"], peer["id"]}
    moved(api, (first, first_box, "A1"), (second, second_box, "A1"), (peer, second_box, "A2"))
    before = [model.objects.count() for model in (AuditEvent, Event, ObservationEvent)]
    for record in (first, second):
        match = preview(api, **selection, code=record["hrid"], mode="iki_id")
        assert match["status"] == "one" and [c["id"] for c in match["candidates"]] == [record["id"]]
    assert preview(api, **selection, code="NOT-ON-ANY-TUBE") == {"status": "none", "candidates": []}
    ambiguous = preview(api, **selection, code=first["hrid"])
    assert ambiguous["status"] == "ambiguous"
    assert {c["id"] for c in ambiguous["candidates"]} == {first["id"], peer["id"]}
    assert (
        preview(api, scope="confirmation", records=[first["id"], peer["id"]], code=first["hrid"])
        == ambiguous
    )
    problem(
        api.post(PREVIEW, {**selection, "code": first["hrid"], "choice": first["id"]}),
        409,
        "scan_unavailable",
    )
    # A readable source occupant outside the selection cannot satisfy the operation.
    outside = problem(
        api.post(PREVIEW, {**selection, "code": peer["hrid"], "mode": "iki_id"}),
        409,
        "scan_unavailable",
    )
    assert "candidates" not in outside
    assert [model.objects.count() for model in (AuditEvent, Event, ObservationEvent)] == before

    checked_out = api.post("/storage/check-out", {"records": [second["id"]]})
    assert checked_out.status_code == 200, checked_out.content
    back = preview(api, **selection, code="SECOND")
    assert back["status"] == "one" and back["candidates"][0]["id"] == second["id"]
    assert back["candidates"][0]["position"] == "A1"


def test_batch_confirmation_returns_one_neutral_refusal_for_concealed_evidence(
    world: World,
) -> None:
    other = build_world("Other")
    api = Api(world.browser("editor"))
    source = box(api, world.project)
    target = tube(api, world.project, "VISIBLE")
    hidden = tube(Api(other.browser("editor")), other.project, "PRIVATE")
    moved(api, (target, source, "A1"))
    Place.objects.create(entity_id=hidden["id"], container_id=source["id"], row=1, column=2)
    selection = {"scope": "confirmation", "records": [target["id"]]}
    for data in (
        {"code": "VISIBLE"},
        {"code": "PRIVATE"},
        {"code": "NO-LABEL"},
        {"code": hidden["hrid"], "mode": "iki_id"},
        {"code": "TUB-999999999", "mode": "iki_id"},
        {"code": "VISIBLE", "records": [target["id"], hidden["id"]]},
        {"code": "VISIBLE", "records": [target["id"], str(uuid4())]},
    ):
        error = problem(api.post(PREVIEW, {**selection, **data}), 409, "scan_unavailable")
        assert "candidates" not in error
        assert error["detail"] == (
            "This scan can’t identify a tube here. "
            "Use its printed Iki ID or ask an authorized reviewer."
        )
    unique = preview(api, **selection, code=target["hrid"], mode="iki_id")
    assert unique["status"] == "one" and unique["candidates"][0]["id"] == target["id"]


def test_a_rack_selection_resolves_in_one_request_without_per_record_queries(world: World) -> None:
    api = Api(world.browser("editor"))
    grid = Shape(8, 12)
    source = box(api, world.project, rows=grid.rows, columns=grid.columns)
    records = [tube(api, world.project, f"RACK-{i:02d}") for i in range(96)]
    moved(api, *((record, source, grid.label(*grid.at(i))) for i, record in enumerate(records)))
    ids = [record["id"] for record in records]
    query = {"scope": "confirmation", "code": "RACK-95"}
    with CaptureQueriesContext(connection) as single:
        expected = preview(api, **query, records=ids[-1:])
    with CaptureQueriesContext(connection) as rack:
        result = preview(api, **query, records=ids)
    assert result == expected
    assert result["status"] == "one" and result["candidates"][0]["id"] == ids[-1]
    assert result["candidates"][0]["position"] == "H12"
    assert len(rack) <= len(single) + 2
    assert preview(api, **query, records=[ids[-1]] * 500) == expected
    for size in (0, 501):
        problem(api.post(PREVIEW, {**query, "records": [ids[-1]] * size}), 422, "validation_failed")


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

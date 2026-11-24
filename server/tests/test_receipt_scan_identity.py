"""Receiving and put-away resolve physical labels against the operator's actual scope."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from api_client import Api
from test_requests import ASK, Lab, _member, submit, takes
from test_storage import BOX, box, made, moved, tube
from test_storage import lab as lab

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity, ProjectLink
from iki_server.org.models import Project, ProjectRole
from iki_server.storage.models import ReceiptLine

pytestmark = pytest.mark.django_db


def open_receipt(lab: Lab, specimens: list[dict[str, Any]]) -> tuple[Api, str]:
    takes(lab)
    request = made(
        lab.api("editor"),
        lab.world.project,
        ASK,
        {"title": "Inspect delivery", "tubes": [t["id"] for t in specimens]},
    )
    answer = submit(lab, request["id"])
    assert answer.status_code == 200, answer.content
    receiver = lab.api("tech")
    opened = receiver.post(f"/entities/{request['id']}/receipts")
    assert opened.status_code == 201, opened.content
    return receiver, f"/receipts/{opened.json()['id']}"


def project_for(lab: Lab, name: str) -> Project:
    project = Project.objects.create(team=lab.world.team, name=name)
    _member(project, lab.users["editor"], ProjectRole.EDITOR)
    _member(project, lab.users["tech"], ProjectRole.EDITOR)
    return project


def test_a_receipt_requires_a_choice_between_matching_checklist_lines(lab: Lab) -> None:
    editor = lab.api("editor")
    other = project_for(lab, "Other consignor")
    first = tube(editor, lab.world.project, "SAME")
    second = tube(editor, other, "SAME")
    container = box(editor, other)
    moved(editor, (second, container, "A2"))
    receiver, path = open_receipt(lab, [first, second])
    initial = receiver.get(path).json()
    audits = AuditEvent.objects.count()
    refused = receiver.post(path + "/scan", {"code": "SAME"})
    assert refused.status_code == 409, refused.content
    choices = {c["id"]: c for c in refused.json()["candidates"]}
    assert set(choices) == {first["id"], second["id"]}
    assert choices[second["id"]]["owner_project"]["id"] == str(other.pk)
    assert choices[second["id"]]["box"]["id"] == container["id"]
    assert choices[second["id"]]["position"] == "A2"
    invalid = receiver.post(path + "/scan", {"code": "SAME", "choice": str(uuid4())})
    assert invalid.status_code == 409, invalid.content
    assert receiver.get(path).json() == initial and AuditEvent.objects.count() == audits
    accepted = receiver.post(path + "/scan", {"code": "SAME", "choice": second["id"]})
    assert accepted.status_code == 200, accepted.content
    result = accepted.json()
    assert set(result) == {"found", "line", "receipt"} and result["found"] == "matched"
    assert str(ReceiptLine.objects.get(pk=result["line"]).entity_id) == second["id"]
    (event,) = receiver.get(path + "/history").json()["items"]
    assert event["after"]["scan"] == {
        "code": "SAME",
        "identity_basis": "vendor",
        "scope": "receipt",
        "chosen": True,
    }
    assert receiver.post(path + "/scan", {"code": "SAME"}).status_code == 409
    assert receiver.get(path + "/history").json()["items"] == [event]
    explicit = receiver.post(path + "/scan", {"code": first["hrid"], "mode": "iki_id"})
    assert explicit.status_code == 200 and explicit.json()["receipt"]["outcomes"]["matched"] == 2


def test_unexpected_ambiguous_receipt_label_is_saved_without_an_identity(lab: Lab) -> None:
    other = project_for(lab, "Other visible project")
    receiver, path = open_receipt(lab, [tube(lab.api("editor"), lab.world.project, "EXPECTED")])
    tube(receiver, other, "EXTRA-SHARED")
    tube(receiver, lab.core, "EXTRA-SHARED")
    unique = tube(receiver, lab.core, "EXTRA-ONE")
    for code, found, entity in (
        ("EXTRA-SHARED", "ambiguous_extra", None),
        ("EXTRA-ONE", "extra", unique["id"]),
        ("EXTRA-UNKNOWN", "extra", None),
    ):
        answer = receiver.post(path + "/scan", {"code": code})
        assert answer.status_code == 200, answer.content
        result = answer.json()
        assert set(result) == {"found", "line", "receipt"} and result["found"] == found
        saved = next(line for line in result["receipt"]["lines"] if line["id"] == result["line"])
        assert saved["outcome"] == "extra" and saved["code"] == code and saved["entity"] == entity
        again = receiver.post(path + "/scan", {"code": code})
        assert again.json() == {**result, "found": "again"}
        (event,) = receiver.get(path + "/history", line_id=result["line"]).json()["items"]
        if entity is None:
            assert "entity_id" not in event["after"]
        else:
            assert event["after"]["entity_id"] == entity
        assert event["after"]["scan"] == {
            "code": code,
            "identity_basis": {"ambiguous_extra": "ambiguous"}.get(
                found, "vendor" if entity else "unmatched"
            ),
            "chosen": False,
            "scope": "inventory",
        }


def test_partly_restricted_receipt_withholds_the_same_response_for_plain_labels(lab: Lab) -> None:
    ask = lab.world.project
    visible, restricted = (tube(lab.api("editor"), ask, c) for c in ("OPEN", "SEALED"))
    receiver, path = open_receipt(lab, [visible, restricted])
    ProjectLink.objects.filter(entity_id=restricted["id"], project=lab.core).delete()
    initial = receiver.get(path).json()
    assert "SEALED" not in str(initial) and restricted["id"] not in str(initial)
    audits = AuditEvent.objects.count()
    problems = []
    for text in ("UNREGISTERED-A", "UNREGISTERED-B", "OPEN"):
        answer = receiver.post(path + "/scan", {"code": text})
        assert answer.status_code == 409, answer.content
        problems.append({k: v for k, v in answer.json().items() if k != "request_id"})
    assert problems[0] == problems[1] == problems[2]
    assert "candidates" not in problems[0] and "SEALED" not in str(problems[0])
    assert receiver.get(path).json() == initial and AuditEvent.objects.count() == audits
    expected = receiver.post(path + "/scan", {"code": visible["hrid"], "mode": "iki_id"})
    assert expected.status_code == 200 and expected.json()["found"] == "matched"


def test_unrecognized_explicit_identity_never_creates_an_extra(lab: Lab) -> None:
    receiver, path = open_receipt(lab, [tube(lab.api("editor"), lab.world.project, "EXPECTED")])
    before = receiver.get(path).json()
    answers = [
        receiver.post(path + "/scan", {"code": "TUB-999999", "mode": "iki_id"}),
        receiver.post(path + "/scan", {"code": f"/entities/{uuid4()}/"}),
    ]
    assert [answer.status_code for answer in answers] == [409, 409]
    assert answers[0].json()["detail"] == answers[1].json()["detail"]
    assert (
        receiver.get(path).json() == before
        and receiver.get(path + "/history").json()["items"] == []
    )


def test_receiving_can_resolve_a_destination_barcode_and_refuses_unreviewed_duplicates(
    lab: Lab,
) -> None:
    specimen = tube(lab.api("editor"), lab.world.project, "TO-RECEIVE")
    receiver, path = open_receipt(lab, [specimen])
    values = {"name": "Incoming", "rows": 2, "columns": 2, "barcode": "INCOMING"}
    target = made(receiver, lab.core, BOX, values)
    other = project_for(lab, "Other storage")
    made(receiver, other, BOX, values)
    observed = receiver.post(path + "/scan", {"code": "TO-RECEIVE"}).json()
    reviewed = receiver.patch(
        path, {"plan_revision": observed["receipt"]["plan_revision"], "handed_over_by": "Courier"}
    ).json()
    place = {"line": observed["line"], "to": "INCOMING", "position": "A1"}
    body = {"plan_revision": reviewed["plan_revision"], "put_away": [place]}
    refused = receiver.post(path + "/complete", body)
    assert refused.status_code == 409
    assert receiver.get(path).json() == reviewed
    body["put_away"] = [{**place, "to_choice": target["id"]}]
    accepted = receiver.post(path + "/complete", body)
    assert accepted.status_code == 200, accepted.content
    assert accepted.json()["lines"][0]["put_away"]["id"] == target["id"]


def test_receiving_accepts_the_same_box_identifiers_as_the_destination_preview(lab: Lab) -> None:
    editor = lab.api("editor")
    specimens = [tube(editor, lab.world.project, f"DELIVERY-{i}") for i in range(4)]
    receiver, path = open_receipt(lab, specimens)
    code = "ABCDEF0123456789" * 2  # a vendor's 32-hex identifier, without an Iki namespace
    target = made(
        receiver, lab.core, BOX, {"name": "Rack", "rows": 1, "columns": 4, "barcode": code}
    )
    names = [
        {"to": code},
        {"to": "IKI:" + target["id"].replace("-", "").upper()},
        {"to": f"/entities/{target['id']}/"},
        {"to": target["hrid"], "to_mode": "iki_id"},
    ]
    placement = []
    for index, (specimen, name) in enumerate(zip(specimens, names, strict=True), 1):
        found = receiver.post(path + "/scan", {"code": specimen["hrid"], "mode": "iki_id"}).json()
        preview = receiver.post(
            "/storage/scan/resolve",
            {"scope": "destination", "code": name["to"], "mode": name.get("to_mode", "label")},
        )
        assert preview.status_code == 200, preview.content
        assert [c["id"] for c in preview.json()["candidates"]] == [target["id"]]
        placement.append({"line": found["line"], "position": f"A{index}", **name})
    reviewed = receiver.patch(
        path,
        {"plan_revision": receiver.get(path).json()["plan_revision"], "handed_over_by": "Courier"},
    ).json()
    result = receiver.post(
        path + "/complete", {"plan_revision": reviewed["plan_revision"], "put_away": placement}
    )
    assert result.status_code == 200, result.content
    assert {line["put_away"]["id"] for line in result.json()["lines"]} == {target["id"]}
    contents = receiver.get(f"/storage/containers/{target['id']}").json()["contents"]
    assert {row["position"]: row["id"] for row in contents} == {
        f"A{i}": sample["id"] for i, sample in enumerate(specimens, 1)
    }


def test_a_destination_vendor_barcode_equal_to_another_box_id_requires_a_choice(lab: Lab) -> None:
    receiver, path = open_receipt(lab, [tube(lab.api("editor"), lab.world.project, "DOMICILE")])
    direct = box(receiver, lab.core, "Known box")
    vendor = made(
        receiver,
        lab.core,
        BOX,
        {"name": "Supplier box", "rows": 1, "columns": 1, "barcode": direct["id"]},
    )
    scanned = receiver.post(path + "/scan", {"code": "DOMICILE"}).json()
    reviewed = receiver.patch(
        path, {"handed_over_by": "Courier", "plan_revision": scanned["receipt"]["plan_revision"]}
    ).json()
    item = {"line": scanned["line"], "to": direct["id"], "position": "A1"}
    approval = {"plan_revision": reviewed["plan_revision"], "put_away": [item]}
    before = AuditEvent.objects.count()
    refused = receiver.post(path + "/complete", approval)
    assert refused.status_code == 409, refused.content
    assert {candidate["id"] for candidate in refused.json()["candidates"]} == {
        vendor["id"],
        direct["id"],
    }
    assert receiver.get(path).json() == reviewed and AuditEvent.objects.count() == before
    approval["put_away"] = [{**item, "to_choice": vendor["id"]}]
    accepted = receiver.post(path + "/complete", approval)
    assert accepted.status_code == 200, accepted.content
    assert accepted.json()["lines"][0]["put_away"]["id"] == vendor["id"]
    assert receiver.get(f"/storage/containers/{direct['id']}").json()["contents"] == []


def test_receipt_history_preserves_the_input_and_matching_path_without_relabelling_later_edits(
    lab: Lab,
) -> None:
    editor = lab.api("editor")
    specimen = tube(editor, lab.world.project, "SUPPLIER")
    receiver, path = open_receipt(lab, [specimen])
    canonical = "IKI:" + specimen["id"].replace("-", "").upper()
    scans = [
        ("  SUPPLIER\t", "label", "vendor"),
        (specimen["hrid"].lower(), "label", "hrid"),
        (specimen["hrid"], "iki_id", "iki_id"),
        (f" /entities/{specimen['id']}/ ", "label", "record_link"),
        (canonical, "label", "record_code"),
    ]
    for code, mode, expected in scans:
        reply = receiver.post(path + "/scan", {"code": code, "mode": mode})
        assert reply.status_code == 200 and reply.json()["found"] == "matched"
        found = reply.json()
        event = receiver.get(path + "/history").json()["items"][0]
        assert event["line_id"] == found["line"]
        assert event["after"]["scan"] == {
            "code": code,
            "identity_basis": expected,
            "scope": "receipt",
            "chosen": False,
        }
        assert event["after"]["entity_id"] == specimen["id"]
        assert "entity_id" not in event["before"]
        cleared = receiver.patch(
            f"{path}/lines/{found['line']}",
            {
                "outcome": None,
                "line_revision": found["receipt"]["lines"][0]["line_revision"],
                "reason": "Rechecked the label at delivery",
            },
        )
        assert cleared.status_code == 200, cleared.content
        amendment = receiver.get(path + "/history").json()["items"][0]
        assert amendment["before"] == event["after"] and "scan" not in amendment["after"]
        assert "entity_id" not in amendment["after"]

    # A plain scan may match both the actual HRID and the vendor field of the very same tube.
    ref = f"/entities/{specimen['id']}"
    updated = editor.patch(
        ref,
        {"values": {"barcode": specimen["hrid"]}},
        headers={"If-Match": editor.get(ref)["ETag"]},
    )
    assert updated.status_code == 200, updated.content
    assert receiver.post(path + "/scan", {"code": specimen["hrid"]}).json()["found"] == "matched"
    events = receiver.get(path + "/history").json()["items"]
    assert events[0]["after"]["scan"]["identity_basis"] == "hrid_and_vendor"
    assert events[-1]["after"]["scan"]["code"] == scans[0][0]

    # The historical link gates both halves of every amendment, even following a purge.
    ProjectLink.objects.filter(entity_id=specimen["id"], project=lab.core).delete()
    hidden = receiver.get(path + "/history").json()["items"]
    assert len(hidden) == len(events)
    for event in hidden:
        for state in (event["before"], event["after"]):
            assert state["code"] == "" and "scan" not in state and "entity_id" not in state
    assert canonical not in str(hidden) and specimen["id"] not in str(hidden)
    Entity.objects.filter(pk=specimen["id"]).update(purged_at=datetime.now(UTC))
    assert receiver.get(path + "/history").json()["items"] == hidden

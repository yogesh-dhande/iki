"""A required move scan has to identify the particular tube in the physical operation."""

from dataclasses import dataclass, replace
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_storage import TUBE, box, contents, made, moved, moves
from test_storage_scan_preview import preview

from iki_server.audit.models import AuditEvent
from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.storage.models import Event

pytestmark = pytest.mark.django_db

VENDOR = "SHARED-VENDOR-42"
RETAIN_TUBE = "acme/protein.RetainTube"


@dataclass
class Tubes:
    world: World
    api: Api
    partner: Api
    first: dict[str, Any]
    second: dict[str, Any]
    source: dict[str, Any]
    destination: dict[str, Any]
    link: str


def shared_tubes(first_type: str) -> Tubes:
    world = build_world()
    provider = Api(world.browser("team_member"))
    project = provider.post("/projects", {"team": str(world.team.pk), "name": "Supplier"})
    assert project.status_code == 201, project.content
    grant = Api(world.browser("admin")).put(
        f"/projects/{world.project.pk}/members/users/{world.users['team_member'].pk}",
        {"role": "editor"},
    )
    assert grant.status_code == 200, grant.content
    api = Api(world.browser("editor"))
    first = made(api, world.project, first_type, {"barcode": VENDOR})
    other = provider.post(
        "/entities",
        {"type": TUBE, "project": project.json()["id"], "values": {"barcode": VENDOR}},
    )
    assert other.status_code == 201, other.content
    second = other.json()
    link = f"/entities/{second['id']}/projects/{world.project.pk}"
    shared = provider.put(link)
    assert shared.status_code == 201, shared.content
    return Tubes(
        world,
        api,
        provider,
        first,
        second,
        box(api, world.project, "Box A"),
        box(api, world.project, "Box B"),
        link,
    )


@pytest.fixture
def tubes() -> Tubes:
    return shared_tubes(TUBE)


def require_move_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture type requires only draw confirmation. Add the supported move declaration.
    types = registry.types()
    declared = types[TUBE]
    assert declared.storage is not None
    monkeypatch.setitem(
        types,
        TUBE,
        replace(
            declared, storage=replace(declared.storage, confirm=(*declared.storage.confirm, "move"))
        ),
    )


def link_proof(record: dict[str, Any]) -> str:
    return f"/entities/{record['id']}/"


def writes() -> tuple[int, int]:
    return Event.objects.count(), AuditEvent.objects.filter(action="storage.moved").count()


def history(api: Api, record: dict[str, Any]) -> list[dict[str, Any]]:
    answer = api.get(f"/entities/{record['id']}/location/events")
    assert answer.status_code == 200, answer.content
    events: list[dict[str, Any]] = answer.json()["items"]
    return events


def test_a_retain_tube_requires_its_own_scan_to_move() -> None:
    tubes = shared_tubes(RETAIN_TUBE)
    api, first, second = tubes.api, tubes.first, tubes.second
    types = {t["name"]: t for t in api.get("/entity-types").json()}
    assert types[RETAIN_TUBE]["storage"]["confirm"] == ["draw", "move"]
    assert types[RETAIN_TUBE]["amount"]["unit"] == "uL"
    assert types[TUBE]["storage"]["confirm"] == ["draw"]
    label = api.post(
        "/storage/labels",
        {"records": [first["id"]], "size": "cryo", "format": "print", "preview": True},
    )
    assert label.status_code == 200, label.content
    printed_code = label.json()["labels"][0]["code"]
    moved(
        api,
        (first, tubes.source, "A1"),
        (second, tubes.source, "A2"),
        confirmations={first["id"]: printed_code},
    )
    assert history(api, first)[0]["confirmed"] == printed_code
    item = (first, tubes.destination, "A1")
    before = writes()
    for proof in ({}, {first["id"]: VENDOR}, {first["id"]: link_proof(second)}):
        refused = moves(api, item, confirmations=proof)
        assert refused.status_code == 422, refused.content
    assert writes() == before
    assert contents(api, tubes.source) == {"A1": first["hrid"], "A2": second["hrid"]}
    assert contents(api, tubes.destination) == {}

    proof_of_first = {first["id"]: {"mode": "iki_id", "code": first["hrid"]}}
    operation = moved(api, item, confirmations=proof_of_first)["operation"]
    assert contents(api, tubes.source) == {"A2": second["hrid"]}
    assert contents(api, tubes.destination) == {"A1": first["hrid"]}
    assert history(api, first)[0]["confirmed"] == first["hrid"]
    event = AuditEvent.objects.get(action="storage.moved", data__operation=operation)
    assert event.target_id == first["id"] and event.data["confirmed"] == first["hrid"]


@pytest.mark.parametrize("check_in", [False, True], ids=["move", "check-in"])
def test_confirming_a_selection_rechecks_every_source_box(check_in: bool) -> None:
    tubes = shared_tubes(RETAIN_TUBE)
    api, target, peer = tubes.api, tubes.first, tubes.second
    other = made(api, tubes.world.project, TUBE, {"barcode": "OTHER-TUBE"})
    ids = [target["id"], other["id"]]
    moved(
        api,
        (target, tubes.source, "A1"),
        (other, tubes.destination, "A1"),
        confirmations={target["id"]: link_proof(target)},
    )
    if check_in:
        checked_out = api.post("/storage/check-out", {"records": ids})
        assert checked_out.status_code == 200, checked_out.content
        endpoint = "/storage/check-in"
        items = [{"record": record_id} for record_id in ids]
    else:
        endpoint = "/storage/moves"
        items = [
            {"record": target["id"], "to": tubes.source["id"], "position": "B1"},
            {"record": other["id"], "to": tubes.destination["id"], "position": "B1"},
        ]
    selection = {"scope": "confirmation", "records": ids}
    initial = preview(api, **selection, code=VENDOR)
    assert initial["status"] == "one" and [c["id"] for c in initial["candidates"]] == [target["id"]]
    moved(api, (peer, tubes.destination, "A2"))
    changed = preview(api, **selection, code=VENDOR)
    assert changed["status"] == "ambiguous"
    assert {c["id"] for c in changed["candidates"]} == {target["id"], peer["id"]}

    paths = [f"/entities/{record_id}/location" for record_id in ids]
    locations = [api.get(path).json() for path in paths]
    before = writes()
    command = {"items": items, "reason": "Return the selected tubes"}
    refused = api.post(endpoint, {**command, "confirmations": {target["id"]: VENDOR}})
    assert refused.status_code == 422, refused.content
    assert writes() == before and [api.get(path).json() for path in paths] == locations

    code = link_proof(target)
    accepted = api.post(endpoint, {**command, "confirmations": {target["id"]: code}})
    assert accepted.status_code == 200, accepted.content
    for record, proof in ((target, code), (other, "")):
        event = history(api, record)[0]
        assert (event["operation"], event["confirmed"]) == (accepted.json()["operation"], proof)
    for path in paths:
        location = api.get(path).json()
        assert location["custodian"] is None and location["position"] == (
            "A1" if check_in else "B1"
        )


@pytest.mark.parametrize("concealed", [False, True], ids=["readable", "concealed"])
def test_a_single_move_checks_the_other_occupants(
    tubes: Tubes, monkeypatch: pytest.MonkeyPatch, concealed: bool
) -> None:
    api, first, second = tubes.api, tubes.first, tubes.second
    moved(api, (first, tubes.source, "A1"), (second, tubes.source, "A2"))
    if concealed:
        removed = tubes.partner.delete(tubes.link)
        assert removed.status_code == 204, removed.content
        assert api.get(f"/entities/{second['id']}").status_code == 404
    require_move_proof(monkeypatch)
    before = writes()
    item = (first, tubes.destination, "B1")
    confirmations = {first["id"]: VENDOR}
    plan = moves(api, item, preview=True, confirmations=confirmations)
    assert plan.status_code == 200, plan.content
    assert not plan.json()["done"]
    assert plan.json()["items"][0]["problems"]
    refused = moves(api, item, confirmations=confirmations)
    assert refused.status_code == 422, refused.content
    if concealed:
        for response in (plan, refused):
            assert second["id"] not in response.content.decode()
            assert second["hrid"] not in response.content.decode()
    assert writes() == before
    assert contents(api, tubes.destination) == {}
    assert contents(api, tubes.source)["A1"] == first["hrid"]

    # The precise entity label identifies the first tube even alongside the vendor twin.
    code = link_proof(first)
    operation = moved(api, item, confirmations={first["id"]: code})["operation"]
    assert contents(api, tubes.destination) == {"B1": first["hrid"]}
    assert history(api, first)[0]["confirmed"] == code
    assert (
        AuditEvent.objects.get(action="storage.moved", data__operation=operation).data["confirmed"]
        == code
    )

    # Its new source box contains only this tube: its ordinary vendor label works again.
    proof = {first["hrid"]: VENDOR}
    operation = moved(api, (first, tubes.destination, "B2"), confirmations=proof)["operation"]
    assert history(api, first)[0]["confirmed"] == VENDOR
    assert (
        AuditEvent.objects.get(action="storage.moved", data__operation=operation).data["confirmed"]
        == VENDOR
    )
    out = api.post("/storage/check-out", {"records": [first["id"]]})
    assert out.status_code == 200, out.content
    back = api.post(
        "/storage/check-in", {"items": [{"record": first["id"]}], "confirmations": proof}
    )
    assert back.status_code == 200, back.content
    returned = history(api, first)[0]
    assert (returned["kind"], returned["confirmed"]) == ("checked_in", VENDOR)
    assert contents(api, tubes.destination) == {"B2": first["hrid"]}
    assert (
        AuditEvent.objects.get(
            action="storage.moved", data__operation=back.json()["operation"]
        ).data["confirmed"]
        == VENDOR
    )


def test_a_batch_requires_an_independent_proof_for_every_selected_tube(
    tubes: Tubes, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, api = tubes.first, tubes.second, tubes.api
    # Without the move declaration, unsolicited text cannot become evidence of a scan.
    initial = moved(
        api,
        (first, tubes.source, "A1"),
        (second, tubes.destination, "A1"),
        confirmations={first["id"]: "unsolicited proof"},
    )
    assert history(api, first)[0]["confirmed"] == ""
    initial_audit = AuditEvent.objects.get(
        action="storage.moved", target_id=first["id"], data__operation=initial["operation"]
    )
    assert "confirmed" not in initial_audit.data

    # Each source has just one tube, yet both labelled tubes participate in this operation.
    require_move_proof(monkeypatch)
    items = ((first, tubes.destination, "A2"), (second, tubes.source, "A2"))
    before = writes()
    links = {r["id"]: link_proof(r) for r in (first, second)}
    for proofs in (
        {first["id"]: VENDOR, second["id"]: VENDOR},
        {first["id"]: link_proof(first), second["id"]: link_proof(first)},
        links | {first["hrid"]: link_proof(first)},
        links | {tubes.source["id"]: link_proof(first)},
    ):
        refused = moves(api, *items, confirmations=proofs)
        assert refused.status_code == 422, refused.content
        assert writes() == before
        assert contents(api, tubes.source) == {"A1": first["hrid"]}
        assert contents(api, tubes.destination) == {"A1": second["hrid"]}

    operation = moved(api, *items, confirmations=links)["operation"]
    for record in (first, second):
        assert history(api, record)[0]["confirmed"] == links[record["id"]]
    assert AuditEvent.objects.filter(action="storage.moved", data__operation=operation).count() == 2
    assert contents(api, tubes.source) == {"A2": second["hrid"]}
    assert contents(api, tubes.destination) == {"A2": first["hrid"]}


def test_explicit_iki_id_distinguishes_an_hrid_from_its_vendor_namesake(
    tubes: Tubes, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, first, second = tubes.api, tubes.first, tubes.second
    path = f"/entities/{second['id']}"
    renamed = tubes.partner.patch(
        path,
        {"values": {"barcode": first["hrid"]}},
        headers={"If-Match": tubes.partner.get(path)["ETag"]},
    )
    assert renamed.status_code == 200, renamed.content
    moved(api, (first, tubes.source, "A1"), (second, tubes.source, "A2"))
    require_move_proof(monkeypatch)
    before = writes()
    item = (first, tubes.destination, "A1")
    refused = moves(api, item, confirmations={first["id"]: first["hrid"]})
    assert refused.status_code == 422, refused.content
    assert writes() == before
    proof = {"code": first["hrid"], "mode": "iki_id"}
    wrong = moves(api, (second, tubes.destination, "A1"), confirmations={second["id"]: proof})
    assert wrong.status_code == 422, wrong.content
    assert writes() == before
    moved(api, item, confirmations={first["id"]: proof})
    assert contents(api, tubes.destination) == {"A1": first["hrid"]}
    assert history(api, first)[0]["confirmed"] == first["hrid"]


@pytest.mark.parametrize("relocate", [False, True], ids=["reserved", "explicit-destination"])
def test_check_in_requires_a_proof_before_custody_or_position_changes(
    tubes: Tubes, monkeypatch: pytest.MonkeyPatch, relocate: bool
) -> None:
    api, first, second = tubes.api, tubes.first, tubes.second
    moved(api, (first, tubes.source, "A1"), (second, tubes.source, "A2"))
    require_move_proof(monkeypatch)
    out = api.post("/storage/check-out", {"records": [first["id"], second["id"]]})
    assert out.status_code == 200, out.content
    custodian = str(tubes.world.users["admin"].pk)
    handed = api.post("/storage/hand-over", {"record": first["id"], "to": custodian})
    assert handed.status_code == 200, handed.content
    item = {"record": first["id"]}
    if relocate:
        item.update({"to": tubes.destination["id"], "position": "B1"})
    before = writes()
    items = [{"record": second["id"]}, item]
    sibling = {second["id"]: link_proof(second)}
    for proof in ({}, {first["id"]: VENDOR}, {first["id"]: link_proof(second)}):
        refused = api.post("/storage/check-in", {"items": items, "confirmations": sibling | proof})
        assert refused.status_code == 422, refused.content
        assert writes() == before
        location = api.get(f"/entities/{first['id']}/location").json()
        assert (location["custodian"]["id"], location["position"]) == (custodian, "A1")
        sibling_location = api.get(f"/entities/{second['id']}/location").json()
        assert sibling_location["custodian"]["id"] == str(tubes.world.users["editor"].pk)
        assert contents(api, tubes.destination) == {}

    code = link_proof(first)
    checked = api.post(
        "/storage/check-in", {"items": items, "confirmations": sibling | {first["id"]: code}}
    )
    assert checked.status_code == 200, checked.content
    location = api.get(f"/entities/{first['id']}/location").json()
    assert location["custodian"] is None
    assert location["position"] == ("B1" if relocate else "A1")
    sibling_event = history(api, second)[0]
    assert sibling_event["operation"] == checked.json()["operation"]
    assert (sibling_event["kind"], sibling_event["confirmed"]) == ("checked_in", link_proof(second))
    events = [e for e in history(api, first) if e["operation"] == checked.json()["operation"]]
    assert {e["kind"] for e in events} == ({"moved", "checked_in"} if relocate else {"checked_in"})
    assert all(e["confirmed"] == code for e in events)
    audit = AuditEvent.objects.filter(
        action="storage.moved", target_id=first["id"], data__operation=checked.json()["operation"]
    )
    assert {e.data["kind"] for e in audit} == {e["kind"] for e in events}
    assert all(e.data["confirmed"] == code for e in audit)


def test_an_unplaced_tube_must_be_identified_within_the_readable_inventory(
    tubes: Tubes, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, first = tubes.api, tubes.first
    require_move_proof(monkeypatch)
    for record in (first, tubes.second):
        location = api.get(f"/entities/{record['id']}/location")
        assert location.status_code == 200, location.content
        assert not location.json()["stored"]
    before = writes()
    item = (first, tubes.destination, "A1")
    refused = moves(api, item, confirmations={first["id"]: VENDOR})
    assert refused.status_code == 422, refused.content
    assert writes() == before
    assert contents(api, tubes.destination) == {}

    code = link_proof(first)
    moved(api, item, confirmations={first["id"]: code})
    assert contents(api, tubes.destination) == {"A1": first["hrid"]}
    assert history(api, first)[0]["confirmed"] == code


def test_typing_an_unavailable_or_absent_iki_id_gets_the_same_refusal(tubes: Tubes) -> None:
    api, selected, private = tubes.api, tubes.first, tubes.second
    removed = tubes.partner.delete(tubes.link)
    assert removed.status_code == 204, removed.content
    assert api.get(f"/entities/{private['id']}").status_code == 404
    missing = "TUB-999999"
    assert not Entity.objects.filter(hrid=missing).exists()

    body = {"scope": "confirmation", "record": selected["id"], "mode": "iki_id"}
    path = "/storage/scan/resolve"
    problems = []
    for typed in (private["hrid"], missing):
        refusal = problem(api.post(path, {**body, "code": typed}), 409, "scan_unavailable")
        assert refusal["detail"] == (
            "This scan can’t identify a tube here. "
            "Use its printed Iki ID or ask an authorized reviewer."
        )
        assert "candidates" not in refusal
        problems.append({key: value for key, value in refusal.items() if key != "request_id"})
    assert problems[0] == problems[1]

    accepted = api.post(path, {**body, "code": selected["hrid"]})
    assert accepted.status_code == 200, accepted.content
    assert accepted.json()["status"] == "one"
    assert [candidate["id"] for candidate in accepted.json()["candidates"]] == [selected["id"]]

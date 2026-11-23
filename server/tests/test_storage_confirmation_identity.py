"""A required move scan has to identify the particular tube in the physical operation."""

from dataclasses import dataclass, replace
from typing import Any

import pytest
from api_client import Api
from org_world import World, build_world
from test_storage import TUBE, box, contents, moved, moves, tube

from iki_server.audit.models import AuditEvent
from iki_server.entities import registry
from iki_server.storage.models import Event

pytestmark = pytest.mark.django_db

VENDOR = "SHARED-VENDOR-42"


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


@pytest.fixture
def tubes() -> Tubes:
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
    first = tube(api, world.project, VENDOR)
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

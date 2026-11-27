"""Amounts (ADR 0139): recorded when a record's made, then drawn, transferred, split into
aliquots and adjusted, never below zero, each a movement of its ledger, safe when two
people draw at once."""

import threading
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from api_client import problem, seen
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as, create
from test_delegation import Bot, delegate
from test_oauth import bearer
from test_runs import world
from test_states import roles, set_
from test_webhooks import deliveries, make

from iki_server.accounts.models import User
from iki_server.amounts import services
from iki_server.amounts.models import Amount, Movement
from iki_server.audit.models import AuditEvent
from iki_server.entities import registry, states
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Invalid
from iki_server.graph.models import Edge
from iki_server.org import permissions
from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # the fixture, used here too

SAMPLE = "acme/protein.Sample"


def sample(world: World, label: str, amount: Any = None, **values: Any) -> Any:
    made = create(
        api_as(world),
        world.project,
        {"label": label, **values},
        SAMPLE,
        **({"amount": amount} if amount is not None else {}),
    )
    assert made.status_code == 201, made.content
    return made.json()


def errors(response: Any) -> dict[str, str]:
    assert response.status_code == 422, response.content
    return {e["pointer"]: e["detail"] for e in response.json()["errors"]}


def test_a_records_amount_is_recorded_and_drawn(world: World) -> None:
    api = api_as(world)
    hook = make(world, "https://hooks.example.com/ledger", ["entity.amount_moved"])
    made = sample(
        world, "S-1", {"value": "0.25", "unit": "mL"}, concentration={"value": 2, "unit": "mg/mL"}
    )
    # Kept in its type's unit, exactly.
    assert made["amount"] == {
        "value": "250",
        "unit": "uL",
        "label": "Volume",
        "display": None,
        "held_by": None,
        "revision": 1,
        "updated_at": made["amount"]["updated_at"],
    }
    # Declaring a sampling purpose never grants its lab role to an ordinary editor.
    denied = api.post(
        f"/entities/{made['hrid']}/amount/draw",
        {"amount": {"value": 1, "unit": "uL"}, "purpose": "qa_sampling"},
    )
    assert denied.status_code == 403, denied.content
    roles(world, editor=("acme/protein.qa",))
    drawn = api.post(
        f"/entities/{made['hrid']}/amount/draw",
        {"amount": {"value": "12.5", "unit": "uL"}, "purpose": "qa_sampling", "reason": "SEC"},
    )
    assert drawn.status_code == 200, drawn.content
    assert drawn.json()["amount"]["value"] == "237.5"
    # A mass, at its concentration: 50 ug at 2 mg/mL is 25 uL.
    by_mass = api.post(
        f"/entities/{made['hrid']}/amount/draw", {"amount": {"value": 50, "unit": "ug"}}
    )
    assert by_mass.status_code == 200, by_mass.content
    movement = by_mass.json()["movement"]
    assert (movement["kind"], movement["change"], movement["balance"]) == ("drawn", "-25", "212.5")
    assert movement["given"] == {"value": "50", "unit": "ug"}
    # Never below zero: it says how much there is.
    too_much = api.post(
        f"/entities/{made['hrid']}/amount/draw", {"amount": {"value": 1, "unit": "mL"}}
    )
    assert errors(too_much) == {
        "#/amount": f"is more than {made['hrid']} has: 1000 uL of 212.5 uL."
    }
    # A length isn't how much there is.
    wrong = api.post(f"/entities/{made['hrid']}/amount/draw", {"amount": {"value": 1, "unit": "m"}})
    assert "#/amount" in errors(wrong)
    ledger = api.get(f"/entities/{made['hrid']}/amount/movements").json()["items"]
    assert [(m["kind"], m["change"], m["balance"]) for m in ledger] == [
        ("drawn", "-25", "212.5"),
        ("drawn", "-12.5", "237.5"),
        ("recorded", "250", "250"),
    ]
    assert ledger[1]["reason"] == "SEC" and ledger[1]["user"]["email"]
    assert [m["purpose"] for m in ledger] == ["routine", "qa_sampling", None]
    assert [d.payload["data"]["purpose"] for d in deliveries(hook)] == [
        None,
        "qa_sampling",
        "routine",
    ]
    assert (
        AuditEvent.objects.get(
            action="entity.amount_moved", data__operation=ledger[1]["operation"]
        ).data["purpose"]
        == "qa_sampling"
    )
    unknown = api.post(
        f"/entities/{made['hrid']}/amount/draw",
        {"amount": {"value": 1, "unit": "uL"}, "purpose": "other"},
    )
    assert unknown.status_code == 422
    # The database holds it too, whatever writes it, and the ledger is append-only.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_amount SET value = -1 WHERE entity_id = %s", [made["id"]])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_amount_movement SET change = 0")


def test_delegated_sampling_needs_the_lab_role_for_both_principals(world: World) -> None:
    made = sample(world, "QA-1", {"value": 5, "unit": "uL"})
    path = f"/entities/{made['hrid']}"
    bot = Bot(world)
    token = bot.token(delegate(world, bot).grant)
    body = {"amount": {"value": 1, "unit": "uL"}, "purpose": "qa_sampling"}
    qa = "acme/protein.qa"
    roles(world, editor=(qa,))
    person_only = bearer(token, "GET", path)
    assert person_only.json()["can"]["draw_purposes"] == ["routine"]
    assert bearer(token, "POST", path + "/amount/draw", body).status_code == 403
    roles(world, editor=())
    LabRoleGrant.objects.create(
        membership=ProjectMembership.objects.get(project=world.project, user=bot.account), role=qa
    )
    assert bearer(token, "POST", path + "/amount/draw", body).status_code == 403
    roles(world, editor=(qa,))
    both = bearer(token, "GET", path, headers={"If-None-Match": person_only["ETag"]})
    assert both.status_code == 200
    assert both.json()["can"]["draw_purposes"] == ["routine", "qa_sampling"]
    sampled = bearer(token, "POST", path + "/amount/draw", body)
    assert sampled.status_code == 200, sampled.content
    assert sampled.json()["amount"]["value"] == "4"
    movement = Movement.objects.get(entity_id=made["id"], kind="drawn")
    assert movement.purpose == "qa_sampling" and movement.user == world.users["editor"]
    assert movement.agent is not None and movement.agent.client_id == bot.client.client_id


def test_a_token_cannot_use_a_sampling_grant_outside_its_projects(world: World) -> None:
    # A caller able to edit via a request's service side cannot use the owner's QA
    # grant through a token restricted to that service.
    made = sample(world, "QA-2", {"value": 2, "unit": "uL"})
    roles(world, editor=("acme/protein.qa",))
    editor = world.users["editor"]
    limits = permissions.TokenLimits(
        scopes=frozenset({"read", "write"}), projects=frozenset({world.archived.pk})
    )
    permissions.limit_to(editor, limits)
    entity = Entity.objects.get(pk=made["id"])
    with pytest.raises(PermissionDenied):
        services.draw(editor, entity, services.Given(Decimal(1), "uL"), purpose="qa_sampling")
    assert Amount.objects.get(entity=entity).value == 2
    assert not Movement.objects.filter(entity=entity, kind="drawn").exists()


def test_a_mass_given_when_its_made_is_its_volume_at_its_concentration(world: World) -> None:
    api = api_as(world)
    # 50 ug of a 2 mg/mL sample is 25 uL (ADR 0139), when it's made as when it's drawn.
    made = sample(
        world, "S-1", {"value": 50, "unit": "ug"}, concentration={"value": 2, "unit": "mg/mL"}
    )
    assert made["amount"]["value"] == "25"
    (recorded,) = api.get(f"/entities/{made['hrid']}/amount/movements").json()["items"]
    assert (recorded["kind"], recorded["change"], recorded["balance"]) == ("recorded", "25", "25")
    assert recorded["given"] == {"value": "50", "unit": "ug"}
    # Without a concentration, a mass isn't a volume.
    unmade = create(
        api, world.project, {"label": "S-2"}, SAMPLE, amount={"value": 50, "unit": "ug"}
    )
    assert errors(unmade)["#/amount"].endswith("its concentration, which it hasn't got.")
    assert Entity.objects.filter(type=SAMPLE).count() == 1


def test_its_ledger_is_exact_to_the_last_of_its_forty_digits(world: World) -> None:
    api = api_as(world)
    # 28 digits before the point and 12 after: past what Python's default context keeps.
    made = sample(
        world, "S-1", {"value": "1000000000000000000000000000.000000000002", "unit": "uL"}
    )
    assert made["amount"]["value"] == "1000000000000000000000000000.000000000002"
    drawn = api.post(
        f"/entities/{made['hrid']}/amount/draw",
        {"amount": {"value": "0.000000000001", "unit": "uL"}},
    )
    assert drawn.status_code == 200, drawn.content
    movement = drawn.json()["movement"]
    assert (movement["change"], movement["balance"]) == (
        "-0.000000000001",
        "1000000000000000000000000000.000000000001",
    )
    stored = Amount.objects.get(entity_id=made["id"]).value
    assert stored == Decimal("1000000000000000000000000000.000000000001")
    adjusted = api.post(
        f"/entities/{made['hrid']}/amount/adjust",
        {
            "amount": {"value": "999999999999999999999999999.999999999999", "unit": "uL"},
            "reason": "recount",
        },
        headers=seen(drawn.json()["amount"]["revision"]),
    )
    assert adjusted.status_code == 200, adjusted.content
    # A recount 2e-12 uL below it, by 2e-12 uL.
    assert (adjusted.json()["movement"]["change"], adjusted.json()["amount"]["value"]) == (
        "-0.000000000002",
        "999999999999999999999999999.999999999999",
    )


def test_its_amount_changes_only_through_its_operations(world: World) -> None:
    api = api_as(world)
    made = sample(world, "S-1", {"value": 10, "unit": "uL"})
    viewer = api_as(world, "viewer")
    refused = viewer.post(
        f"/entities/{made['hrid']}/amount/draw", {"amount": {"value": 1, "unit": "uL"}}
    )
    assert refused.status_code in (403, 404)
    # A type without an amount takes none.
    other = create(
        api,
        world.project,
        {"sequence": "MKV"},
        "acme/protein.Protein",
        amount={"value": 1, "unit": "uL"},
    )
    assert errors(other) == {"#/amount": "acme/protein.Protein has no amount."}
    # Adjusting says why.
    unsaid = api.post(
        f"/entities/{made['hrid']}/amount/adjust",
        {"amount": {"value": 8, "unit": "uL"}, "reason": ""},
        headers=seen(made["amount"]["revision"]),
    )
    assert unsaid.status_code == 422
    adjusted = api.post(
        f"/entities/{made['hrid']}/amount/adjust",
        {"amount": {"value": 8, "unit": "uL"}, "reason": "Recounted"},
        headers=seen(made["amount"]["revision"]),
    )
    assert adjusted.status_code == 200, adjusted.content
    assert adjusted.json()["movement"]["change"] == "-2"
    # A record made without one gets one by adjusting it.
    blank = sample(world, "S-2")
    assert blank["amount"] is None
    first = api.post(
        f"/entities/{blank['hrid']}/amount/adjust",
        {"amount": {"value": 5, "unit": "uL"}, "reason": "Measured"},
        headers={"If-None-Match": "*"},
    )
    assert first.json()["movement"]["kind"] == "recorded"


def test_a_count_cannot_overwrite_another_operators_issue(world: World) -> None:
    counter = api_as(world)
    issuer = api_as(world, "via_team")
    made = sample(world, "S-1", {"value": 100, "unit": "uL"})
    ref = f"/entities/{made['hrid']}/amount"
    observed = counter.get(ref).json()
    assert observed["value"] == issuer.get(ref).json()["value"] == "100"
    counted = {
        "amount": {"value": 95, "unit": "uL"},
        "reason": "Counted before the issue",
    }
    missing_revision = counter.post(f"{ref}/adjust", counted)
    assert missing_revision.status_code == 428, missing_revision.content
    issued = issuer.post(f"{ref}/draw", {"amount": {"value": 20, "unit": "uL"}})
    assert issued.status_code == 200, issued.content
    remaining = issued.json()["amount"]
    assert remaining["value"] == "80"

    refused = counter.post(f"{ref}/adjust", counted, headers=seen(observed["revision"]))
    stale = problem(refused, 412, "stale")
    assert "recount" in stale["detail"].lower()
    # The balance's revision now, to recount from: each movement is one.
    assert refused["ETag"] == f'"{remaining["revision"]}"' == f'"{observed["revision"] + 1}"'
    assert counter.get(ref).json() == remaining
    ledger = counter.get(f"{ref}/movements").json()["items"]
    assert [(m["kind"], m["change"], m["balance"]) for m in ledger] == [
        ("drawn", "-20", "80"),
        ("recorded", "100", "100"),
    ]
    assert ledger[0]["user"]["id"] == str(world.users["via_team"].pk)
    assert (
        AuditEvent.objects.filter(action="entity.amount_moved", target_id=made["id"]).count() == 2
    )

    recounted = counter.post(
        f"{ref}/adjust",
        {"amount": {"value": 75, "unit": "uL"}, "reason": "Recounted after the issue"},
        headers=seen(remaining["revision"]),
    )
    assert recounted.status_code == 200, recounted.content
    assert recounted.json()["amount"]["value"] == "75"
    assert recounted.json()["movement"]["change"] == "-5"

    restored = issuer.post(
        f"{ref}/adjust",
        {"amount": {"value": 80, "unit": "uL"}, "reason": "Recounted again"},
        headers=seen(recounted.json()["amount"]["revision"]),
    )
    assert restored.status_code == 200, restored.content
    assert restored.json()["amount"]["value"] == remaining["value"]
    assert restored.json()["amount"]["revision"] == remaining["revision"] + 2
    problem(
        counter.post(f"{ref}/adjust", counted, headers=seen(remaining["revision"])), 412, "stale"
    )


def test_an_initial_count_cannot_replace_a_balance_already_recorded(world: World) -> None:
    first, second = api_as(world), api_as(world, "via_team")
    made = sample(world, "S-1")
    ref = f"/entities/{made['hrid']}/amount"
    assert first.get(ref).status_code == second.get(ref).status_code == 404
    headers = {"If-None-Match": "*"}
    for api, value, status in ((first, 0, 200), (second, 10, 412)):
        response = api.post(
            f"{ref}/adjust",
            {"amount": {"value": value, "unit": "uL"}, "reason": "First count"},
            headers=headers,
        )
        assert response.status_code == status, response.content
    assert second.get(ref).json()["value"] == "0"
    (recorded,) = second.get(f"{ref}/movements").json()["items"]
    assert recorded["kind"] == "recorded"


def test_every_movement_is_a_revision_of_the_balance_a_count_is_made_from(world: World) -> None:
    api = api_as(world)
    made = sample(world, "S-1", {"value": 100, "unit": "uL"})
    other = sample(world, "S-2", {"value": 10, "unit": "uL"})
    ref = f"/entities/{made['hrid']}/amount"
    read = api.get(ref)
    assert read["ETag"] == '"1"' and read.json()["revision"] == 1
    steps = [
        api.post(f"{ref}/draw", {"amount": {"value": 1, "unit": "uL"}}),
        api.post(f"{ref}/transfer", {"to": other["hrid"], "amount": {"value": 2, "unit": "uL"}}),
        api.post(
            f"/entities/{made['hrid']}/aliquots", {"count": 2, "each": {"value": 3, "unit": "uL"}}
        ),
    ]
    assert [s.status_code for s in steps] == [200, 200, 201], [s.content for s in steps]
    # Each is one revision, even a recount to what it already had.
    counted = {"amount": {"value": 91, "unit": "uL"}, "reason": "Recount"}
    adjusted = api.post(f"{ref}/adjust", counted, headers=seen(4))
    assert adjusted.status_code == 200, adjusted.content
    assert adjusted["ETag"] == '"5"' and adjusted.json()["amount"]["revision"] == 5
    again = api.post(f"{ref}/adjust", counted, headers=seen(5))
    assert again.json()["amount"]["revision"] == 6
    # Its transfer's destination moved once too.
    assert api.get(f"/entities/{other['hrid']}/amount").json()["revision"] == 2
    # Saying nothing was recorded, when something was, is stale too; so is naming neither,
    # or both.
    stale = api.post(f"{ref}/adjust", counted, headers={"If-None-Match": "*"})
    problem(stale, 412, "stale")
    assert stale["ETag"] == '"6"'
    problem(api.post(f"{ref}/adjust", counted), 428, "precondition_required")
    both = {**seen(6), "If-None-Match": "*"}
    problem(api.post(f"{ref}/adjust", counted, headers=both), 428, "precondition_required")
    assert api.get(ref).json()["revision"] == 6


def test_a_transfer_moves_some_from_one_to_the_other(world: World) -> None:
    api = api_as(world)
    source = sample(world, "S-1", {"value": 100, "unit": "uL"})
    target = sample(world, "S-2", {"value": "0.01", "unit": "mL"})
    moved = api.post(
        f"/entities/{source['hrid']}/amount/transfer",
        {"to": target["hrid"], "amount": {"value": 30, "unit": "uL"}, "reason": "Pooled"},
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["amount"]["value"] == "70"
    assert api.get(f"/entities/{target['hrid']}/amount").json()["value"] == "40"
    out, came = (
        Movement.objects.get(entity_id=source["id"], kind="transferred_out"),
        Movement.objects.get(entity_id=target["id"], kind="transferred_in"),
    )
    assert out.operation == came.operation
    assert (str(out.counterpart_id), str(came.counterpart_id)) == (target["id"], source["id"])
    # The record it went to depends on the one it came from.
    assert Edge.objects.filter(node_id=target["id"], on_id=source["id"], kind="transfer").exists()


def test_a_transfer_names_the_other_record_only_where_it_can_be_read(world: World) -> None:
    from test_requests import build_lab

    from iki_server.webhooks import services as webhooks

    lab = build_lab(world)
    source = sample(world, "Source sample", {"value": 100, "unit": "uL"})
    response = create(
        lab.api("tech"),
        lab.core,
        {"label": "Receiving sample"},
        SAMPLE,
        amount={"value": 10, "unit": "uL"},
    )
    assert response.status_code == 201, response.content
    target = response.json()
    ProjectMembership.objects.create(
        project=lab.core, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    hook, _ = webhooks.create(
        project=lab.core,
        url="https://hooks.example.com/transfers",
        description="Amount movements",
        chosen=["entity.amount_moved"],
        by=lab.users["core_admin"],
    )
    org_hook = make(
        world, "https://hooks.example.com/ledger", ["entity.amount_moved"], org_level=True
    )
    editor = lab.api("editor")
    moved = editor.post(
        f"/entities/{source['id']}/amount/transfer",
        {"to": target["id"], "amount": {"value": 30, "unit": "uL"}},
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["movement"]["counterpart"] == target["id"]

    path = f"/entities/{target['id']}/amount/movements"
    restricted = lab.api("tech").get(path).json()
    assert restricted["items"][0]["counterpart"] == "unreadable"
    assert restricted["refs"] == {}
    readable = editor.get(path).json()
    assert readable["items"][0]["counterpart"] == source["id"]
    assert readable["refs"][source["id"]]["hrid"] == source["hrid"]
    (notice,) = deliveries(hook)
    assert notice.payload["data"]["entity"]["id"] == target["id"]
    assert notice.payload["data"]["counterpart"] == "unreadable"
    outgoing, incoming = [delivery.payload["data"] for delivery in deliveries(org_hook)]
    assert (outgoing["entity"]["id"], outgoing["kind"]) == (source["id"], "transferred_out")
    assert (incoming["entity"]["id"], incoming["kind"]) == (target["id"], "transferred_in")
    assert set(outgoing) == set(incoming) == {"entity", "kind", "actor", "agent"}


def test_a_split_makes_aliquots_holding_exactly_what_it_loses(world: World) -> None:
    api = api_as(world)
    made = sample(world, "S-1", {"value": 100, "unit": "uL"}, purity=98.5, tags=["serum"])
    # Each aliquot of its own type has its values, but its label is unique: each needs one.
    unlabelled = api.post(
        f"/entities/{made['hrid']}/aliquots", {"count": 3, "each": {"value": 20, "unit": "uL"}}
    )
    assert errors(unlabelled) == {"#/values/0/label": "is required."}
    assert Entity.objects.filter(type=SAMPLE).count() == 1
    split = api.post(
        f"/entities/{made['hrid']}/aliquots",
        {
            "count": 3,
            "each": {"value": "0.02", "unit": "mL"},
            "values": [{"label": f"S-1.{n}"} for n in (1, 2, 3)],
            "reason": "For the assays",
        },
    )
    assert split.status_code == 201, split.content
    body = split.json()
    assert body["amount"]["value"] == "40"
    assert body["movement"]["change"] == "-60"
    hrids = [a["hrid"] for a in body["aliquots"]]
    assert len(hrids) == 3
    one = api.get(f"/entities/{hrids[0]}").json()
    assert one["values"]["label"] == "S-1.1"
    assert one["values"]["purity"] == 98.5 and one["values"]["tags"] == ["serum"]
    assert one["amount"]["value"] == "20"
    # Conserved: what the aliquots hold is what it lost.
    held = sum(Amount.objects.filter(entity_id__in=body["ids"]).values_list("value", flat=True))
    assert held == Decimal(60)
    # Each depends on it, split from it, in one operation.
    for aliquot in body["ids"]:
        assert Edge.objects.filter(node_id=aliquot, on_id=made["id"], kind="aliquot").exists()
    operations = set(
        Movement.objects.filter(entity_id__in=[made["id"], *body["ids"]])
        .exclude(kind="recorded")
        .values_list("operation", flat=True)
    )
    assert len(operations) == 1
    # More than it has is refused, and nothing is made.
    greedy = api.post(
        f"/entities/{made['hrid']}/aliquots",
        {"count": 3, "each": {"value": 20, "unit": "uL"}, "values": {"label": "x"}},
    )
    assert "#/amount" in errors(greedy)
    assert Entity.objects.filter(type=SAMPLE).count() == 4


def test_a_lot_splits_into_samples_each_naming_it(world: World) -> None:
    """Aliquots of another type (ADR 0139): none of the lot's values are copied, and each
    names the lot it was split from."""
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"label": "Lot 7", "yield_mg": 40},
        "acme/protein.Lot",
        amount={"value": "1.5", "unit": "L"},
    )
    assert made.status_code == 201, made.content
    lot = made.json()
    assert (lot["amount"]["value"], lot["amount"]["display"]) == ("1500", "1.5 L")
    # This type hasn't declared a role for sampling. Even QA may only draw for routine use.
    roles(world, editor=("acme/protein.qa",))
    assert lot["can"]["draw_purposes"] == ["routine"]
    undeclared = api.post(
        f"/entities/{lot['hrid']}/amount/draw",
        {"amount": {"value": 1, "unit": "mL"}, "purpose": "qa_sampling"},
    )
    assert undeclared.status_code == 403, undeclared.content
    split = api.post(
        f"/entities/{lot['hrid']}/aliquots",
        {
            "count": 2,
            "each": {"value": 250, "unit": "uL"},
            "values": [{"label": "From lot 7, A"}, {"label": "From lot 7, B"}],
        },
    )
    assert split.status_code == 201, split.content
    body = split.json()
    assert body["amount"]["value"] == "1499.5"
    assert [a["type"] for a in body["aliquots"]] == [SAMPLE, SAMPLE]
    first = api.get(f"/entities/{body['aliquots'][0]['hrid']}").json()
    assert first["values"]["lot"] == lot["id"]
    assert "yield_mg" not in first["values"]
    assert first["amount"]["value"] == "250"
    # Its downstream, through the graph, has them both.
    downstream = api.get(f"/entities/{lot['hrid']}/downstream", edges="aliquot")
    assert downstream.status_code == 200, downstream.content
    assert {i["entity"]["hrid"] for i in downstream.json()["items"]} == {
        a["hrid"] for a in body["aliquots"]
    }


def test_a_state_that_holds_it_stops_it_being_taken(world: World) -> None:
    api = api_as(world)
    made = sample(world, "S-1", {"value": 100, "unit": "uL"})
    roles(world, editor=("acme/protein.qa",))
    set_(world, "editor", made["hrid"], "custody", "quarantined", "Contaminated")
    shown = api.get(f"/entities/{made['hrid']}").json()
    assert shown["amount"]["held_by"] == "custody"
    held = api.post(f"/entities/{made['hrid']}/amount/draw", {"amount": {"value": 1, "unit": "uL"}})
    assert errors(held) == {
        "#/amount": f"{made['hrid']} is held by its custody: none of it can be taken until that "
        "changes."
    }
    sampling = api.post(
        f"/entities/{made['hrid']}/amount/draw",
        {"amount": {"value": 1, "unit": "uL"}, "purpose": "qa_sampling"},
    )
    assert errors(sampling) == errors(held)
    # Back in the lab, it's drawn from again.
    set_(world, "editor", made["hrid"], "custody", "in_lab")
    again = api.post(
        f"/entities/{made['hrid']}/amount/draw", {"amount": {"value": 1, "unit": "uL"}}
    )
    assert again.status_code == 200, again.content


@pytest.mark.django_db(transaction=True)
def test_two_people_drawing_at_once_take_turns(world: World) -> None:
    """Each draws 6 uL of 10 uL at once: the second waits for the first, then finds 4 uL
    left, and is refused. Neither draws what the other took."""
    made = sample(world, "S-1", {"value": 10, "unit": "uL"})
    entity = Entity.objects.get(pk=made["id"])
    user = User.objects.get(pk=world.user("editor").pk)  # type: ignore[union-attr]
    first_drew, first_may_commit = threading.Event(), threading.Event()
    outcomes: dict[str, str] = {}

    def first() -> None:
        try:
            with transaction.atomic():
                services.draw(user, entity, services.Given(Decimal(6), "uL"), reason="first")
                first_drew.set()
                first_may_commit.wait(10)
            outcomes["first"] = "drew"
        finally:
            connection.close()

    def second() -> None:
        try:
            first_drew.wait(10)
            try:
                services.draw(user, entity, services.Given(Decimal(6), "uL"), reason="second")
                outcomes["second"] = "drew"
            except Invalid as error:
                outcomes["second"] = " ".join(error.errors["amount"])
        finally:
            connection.close()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    first_drew.wait(10)
    # The second is waiting on the first's lock, not reading what was there before it.
    threads[1].join(0.5)
    assert threads[1].is_alive()
    first_may_commit.set()
    for t in threads:
        t.join(20)
    assert outcomes == {
        "first": "drew",
        "second": f"is more than {made['hrid']} has: 6 uL of 4 uL.",
    }
    assert Amount.objects.get(pk=made["id"]).value == Decimal(4)
    assert list(
        Movement.objects.filter(entity_id=made["id"])
        .order_by("created_at")
        .values_list("kind", "reason")
    ) == [("recorded", ""), ("drawn", "first")]


def test_pooling_sets_the_concentration_to_the_pools(world: World) -> None:
    api = api_as(world)
    weak = sample(
        world, "S-1", {"value": 1, "unit": "mL"}, concentration={"value": 1, "unit": "mg/mL"}
    )
    strong = sample(
        world, "S-2", {"value": 1, "unit": "mL"}, concentration={"value": 3, "unit": "mg/mL"}
    )
    pooled = api.post(
        f"/entities/{weak['hrid']}/amount/transfer",
        {"to": strong["hrid"], "amount": {"value": 1, "unit": "mL"}, "reason": "Pooled"},
    )
    assert pooled.status_code == 200, pooled.content
    # 1 mg and 3 mg in 2 mL: 2 mg/mL (IUPAC's mass concentration, solute over volume).
    after = api.get(f"/entities/{strong['hrid']}").json()
    assert after["values"]["concentration"] == 2
    assert after["amount"]["value"] == "2000"
    # Its revision says why, by whom.
    revision = Revision.objects.filter(entity_id=strong["id"]).order_by("-number").first()
    assert revision is not None
    assert revision.changed_fields == ["concentration"]
    assert revision.reason == f"Pooled with 1000 uL of {weak['hrid']} at 1 mg/mL: 2 mg/mL. Pooled"
    assert revision.user_id == world.users["editor"].pk
    # So a 1 mg draw is 500 uL of it, not the 333.33 uL its old 3 mg/mL would give.
    drawn = api.post(
        f"/entities/{strong['hrid']}/amount/draw", {"amount": {"value": 1, "unit": "mg"}}
    )
    assert drawn.status_code == 200, drawn.content
    assert drawn.json()["movement"]["change"] == "-500"
    # Topped up with a third of a millilitre of buffer, recorded at 0 mg/mL: diluted.
    buffer = sample(
        world, "B-1", {"value": 1, "unit": "mL"}, concentration={"value": 0, "unit": "mg/mL"}
    )
    topped = api.post(
        f"/entities/{buffer['hrid']}/amount/transfer",
        {"to": strong["hrid"], "amount": {"value": 500, "unit": "uL"}},
    )
    assert topped.status_code == 200, topped.content
    # 3 mg in 2 mL: 1.5 mg/mL.
    assert api.get(f"/entities/{strong['hrid']}").json()["values"]["concentration"] == 1.5
    # Material whose concentration isn't recorded leaves the pool's unknown, so a mass
    # isn't converted through the one it had.
    unknown = sample(world, "S-3", {"value": 1, "unit": "mL"})
    mixed = api.post(
        f"/entities/{unknown['hrid']}/amount/transfer",
        {"to": strong["hrid"], "amount": {"value": 100, "unit": "uL"}},
    )
    assert mixed.status_code == 200, mixed.content
    assert api.get(f"/entities/{strong['hrid']}").json()["values"]["concentration"] is None
    by_mass = api.post(
        f"/entities/{strong['hrid']}/amount/draw", {"amount": {"value": 1, "unit": "mg"}}
    )
    assert errors(by_mass)["#/amount"].endswith("its concentration, which it hasn't got.")
    # Into an empty one, it's what came in.
    empty = sample(world, "S-4")
    emptied = api.post(
        f"/entities/{empty['hrid']}/amount/adjust",
        {"amount": {"value": 0, "unit": "uL"}, "reason": "Empty tube"},
        headers={"If-None-Match": "*"},
    )
    assert emptied.status_code == 200, emptied.content
    filled = api.post(
        f"/entities/{buffer['hrid']}/amount/transfer",
        {"to": empty["hrid"], "amount": {"value": 100, "unit": "uL"}},
    )
    assert filled.status_code == 200, filled.content
    assert api.get(f"/entities/{empty['hrid']}").json()["values"]["concentration"] == 0


def test_pooling_cannot_reopen_a_reviewed_record(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = states.state_fields(SAMPLE)["status"].states
    monkeypatch.setitem(status, "reviewed", replace(status["reviewed"], on_edit="in_testing"))
    roles(world, editor=("acme/protein.reviewer",))
    api = api_as(world)
    source, target = [
        sample(
            world, label, {"value": 1, "unit": "mL"}, concentration={"value": c, "unit": "mg/mL"}
        )
        for label, c in [("S-1", 1), ("S-2", 3)]
    ]
    for record in (source, target):
        set_(world, "editor", record["hrid"], "status", "reviewed")
    # The states match, and the editor could correct the target explicitly. Pooling would
    # change its concentration using the transfer's generated reason.
    pooled = api.post(
        f"/entities/{source['hrid']}/amount/transfer",
        {"to": target["hrid"], "amount": {"value": 1, "unit": "mL"}},
    )
    assert "locked" in errors(pooled)["#/to"]
    after = api.get(f"/entities/{target['hrid']}").json()
    assert (after["revision"], after["values"]["status"], after["values"]["concentration"]) == (
        2,
        "reviewed",
        3,
    )
    for record in (source, target):
        assert api.get(f"/entities/{record['hrid']}/amount").json()["value"] == "1000"


def test_a_locked_record_takes_nothing_that_would_change_it(world: World) -> None:
    api = api_as(world)
    source = sample(
        world, "S-1", {"value": 1, "unit": "mL"}, concentration={"value": 1, "unit": "mg/mL"}
    )
    target = sample(
        world, "S-2", {"value": 1, "unit": "mL"}, concentration={"value": 3, "unit": "mg/mL"}
    )
    roles(world, editor=("acme/protein.qa",))
    set_(world, "editor", target["hrid"], "custody", "quarantined", "Contaminated")
    refused = api.post(
        f"/entities/{source['hrid']}/amount/transfer",
        {"to": target["hrid"], "amount": {"value": 1, "unit": "mL"}},
    )
    assert errors(refused) == {
        "#/to": f"{target['hrid']} is Quarantined, which locks it, so only material that's "
        f"Quarantined too goes into it, and {source['hrid']} is In the lab: neither was "
        "changed."
    }
    # Nothing moved.
    assert api.get(f"/entities/{source['hrid']}/amount").json()["value"] == "1000"
    assert api.get(f"/entities/{target['hrid']}/amount").json()["value"] == "1000"
    # The same concentration would leave its values as they are, but not what it holds:
    # only material that's quarantined too goes into it.
    same = sample(
        world, "S-3", {"value": 1, "unit": "mL"}, concentration={"value": 3, "unit": "mg/mL"}
    )
    kept = api.post(
        f"/entities/{same['hrid']}/amount/transfer",
        {"to": target["hrid"], "amount": {"value": 1, "unit": "mL"}},
    )
    assert errors(kept) == {
        "#/to": f"{target['hrid']} is Quarantined, which locks it, so only material that's "
        f"Quarantined too goes into it, and {same['hrid']} is In the lab: neither was changed."
    }
    assert api.get(f"/entities/{same['hrid']}/amount").json()["value"] == "1000"
    assert api.get(f"/entities/{target['hrid']}/amount").json()["value"] == "1000"


def test_what_its_unit_cant_hold_exactly_isnt_moved(world: World) -> None:
    api = api_as(world)
    # A sample kept in uL, to 12 places (1 aL), and a lot kept in mL (to 1 fL).
    source = sample(world, "S-1", {"value": 10, "unit": "uL"})
    made = create(
        api,
        world.project,
        {"label": "Lot 7"},
        "acme/protein.Lot",
        amount={"value": 1, "unit": "mL"},
    )
    assert made.status_code == 201, made.content
    lot = made.json()
    # 0.4 fL is 4e-13 mL (UCUM: 1 uL = 1e-3 mL), which the lot's 12 places would make 0 mL,
    # and 2.5 fL 2e-12 mL: the source would lose what the lot didn't gain.
    for value, exact in (("0.0000000004", "0.0000000000004"), ("0.0000000025", "0.0000000000025")):
        moved = api.post(
            f"/entities/{source['hrid']}/amount/transfer",
            {"to": lot["hrid"], "amount": {"value": value, "unit": "uL"}},
        )
        assert errors(moved) == {
            "#/amount": f"is {value} uL of {source['hrid']}, which is {exact} mL: "
            f"{lot['hrid']}'s amount, kept in mL to 12 decimal places, can't hold that "
            "exactly, so neither was changed."
        }
    assert api.get(f"/entities/{source['hrid']}/amount").json()["value"] == "10"
    assert api.get(f"/entities/{lot['hrid']}/amount").json()["value"] == "1"
    assert not Movement.objects.filter(kind__in=("transferred_out", "transferred_in")).exists()
    # Nor is a draw that would take nothing from the lot.
    drawn = api.post(
        f"/entities/{lot['hrid']}/amount/draw", {"amount": {"value": "0.4", "unit": "fL"}}
    )
    assert errors(drawn) == {
        "#/amount": f"is 0.4 fL, which is 0.0000000000004 mL: {lot['hrid']}'s amount, kept in "
        "mL to 12 decimal places, can't hold that exactly."
    }
    # A whole femtolitre is fine, either way.
    assert (
        api.post(
            f"/entities/{source['hrid']}/amount/transfer",
            {"to": lot["hrid"], "amount": {"value": "0.000000001", "unit": "uL"}},
        ).status_code
        == 200
    )
    assert api.get(f"/entities/{lot['hrid']}/amount").json()["value"] == "1.000000000001"
    # A mass converted through a concentration rounds to 12 places, but not to nothing.
    dilute = sample(
        world, "S-2", {"value": 10, "unit": "uL"}, concentration={"value": 1, "unit": "mg/mL"}
    )
    tiny = api.post(
        f"/entities/{dilute['hrid']}/amount/draw", {"amount": {"value": "1e-16", "unit": "mg"}}
    )
    assert errors(tiny) == {
        "#/amount": f"is 0.0000000000000001 mg, which at {dilute['hrid']}'s concentration is "
        "less than the 0.000000000001 uL its amount is kept to."
    }


def test_whole_units_are_enforced_after_conversion_on_every_ledger_operation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use a whole-uL type and a normal mL lot to check conversions in both directions.
    # CountStockLot uses the same declaration for indivisible consumables.
    types = registry.types()
    item_type = types[SAMPLE]
    assert item_type.amount is not None
    monkeypatch.setitem(
        types, SAMPLE, replace(item_type, amount=replace(item_type.amount, whole_units=True))
    )
    api = api_as(world)
    half = {"value": "0.0005", "unit": "mL"}
    refused = create(api, world.project, {"label": "fraction"}, SAMPLE, amount=half)
    assert "whole units" in errors(refused)["#/amount"]
    assert not Entity.objects.filter(type=SAMPLE).exists()
    record = sample(world, "whole", {"value": "0.002", "unit": "mL"})
    lot = create(api, world.project, {"label": "continuous"}, "acme/protein.Lot", amount=half)
    assert lot.status_code == 201, lot.content
    path = f"/entities/{record['id']}"
    lot_path = f"/entities/{lot.json()['id']}"
    counted = seen(record["amount"]["revision"])
    for url, payload, headers in [
        (f"{path}/amount/draw", {"amount": half}, {}),
        (f"{path}/amount/adjust", {"amount": half, "reason": "Recount"}, counted),
        (f"{path}/amount/transfer", {"amount": half, "to": lot.json()["id"]}, {}),
        (f"{lot_path}/amount/transfer", {"amount": half, "to": record["id"]}, {}),
        (f"{path}/aliquots", {"count": 2, "each": half}, {}),
    ]:
        assert "whole units" in errors(api.post(url, payload, headers=headers))["#/amount"]
    assert api.get(f"{path}/amount").json()["value"] == "2"
    assert api.get(f"{lot_path}/amount").json()["value"] == "0.0005"
    assert Movement.objects.count() == 2
    assert Entity.objects.filter(type=SAMPLE).count() == 1
    accepted = api.post(f"{path}/amount/draw", {"amount": {"value": "0.001", "unit": "mL"}})
    assert accepted.status_code == 200, accepted.content
    assert accepted.json()["amount"]["value"] == "1"


def test_the_ledger_keeps_what_was_asked_for_exactly(world: World) -> None:
    """ADR 0139 keeps what was asked for, in its unit: 0.0000000000001 L is exactly 0.0000001
    uL, which the sample's 12 places hold, so the movement says 0.0000000000001 L was
    asked for, as its audit event does, not 0 L. What its ledger can't hold exactly is
    refused, not rounded."""
    api = api_as(world)
    tube = sample(world, "S-1", {"value": 10, "unit": "uL"})
    drawn = api.post(
        f"/entities/{tube['hrid']}/amount/draw",
        {"amount": {"value": "0.0000000000001", "unit": "L"}},
    )
    assert drawn.status_code == 200, drawn.content
    assert drawn.json()["movement"]["change"] == "-0.0000001"
    assert drawn.json()["movement"]["given"] == {"value": "0.0000000000001", "unit": "L"}
    (listed, *_) = api.get(f"/entities/{tube['hrid']}/amount/movements").json()["items"]
    assert listed["given"] == {"value": "0.0000000000001", "unit": "L"}
    assert Movement.objects.get(pk=listed["id"]).given_value == Decimal("1E-13")
    beyond = api.post(
        f"/entities/{tube['hrid']}/amount/draw",
        {"amount": {"value": "0." + "0" * 40 + "1", "unit": "GL"}},
    )
    assert errors(beyond) == {
        "#/amount": "has more than the 40 decimal places a ledger keeps of what's asked for."
    }

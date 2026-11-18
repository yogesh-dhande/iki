"""Amounts (ADR 0139): recorded when a record's made, then drawn, transferred, split into
aliquots and adjusted, never below zero, each a movement of its ledger, safe when two
people draw at once."""

import threading
from decimal import Decimal
from typing import Any

import pytest
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as, create
from test_runs import world
from test_states import roles, set_

from iki_server.accounts.models import User
from iki_server.amounts import services
from iki_server.amounts.models import Amount, Movement
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.graph.models import Edge

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
        "updated_at": made["amount"]["updated_at"],
    }
    drawn = api.post(
        f"/entities/{made['hrid']}/amount/draw",
        {"amount": {"value": "12.5", "unit": "uL"}, "reason": "SEC"},
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
    # The database holds it too, whatever writes it, and the ledger is append-only.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_amount SET value = -1 WHERE entity_id = %s", [made["id"]])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_amount_movement SET change = 0")


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
    )
    assert unsaid.status_code == 422
    adjusted = api.post(
        f"/entities/{made['hrid']}/amount/adjust",
        {"amount": {"value": 8, "unit": "uL"}, "reason": "Recounted"},
    )
    assert adjusted.status_code == 200, adjusted.content
    assert adjusted.json()["movement"]["change"] == "-2"
    # A record made without one gets one by adjusting it.
    blank = sample(world, "S-2")
    assert blank["amount"] is None
    first = api.post(
        f"/entities/{blank['hrid']}/amount/adjust",
        {"amount": {"value": 5, "unit": "uL"}, "reason": "Measured"},
    )
    assert first.json()["movement"]["kind"] == "recorded"


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

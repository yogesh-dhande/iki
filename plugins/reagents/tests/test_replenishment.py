"""Partial deliveries, original packing units and supplier document identity."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replenishment.py"
spec = importlib.util.spec_from_file_location("reagent_replenishment", SCRIPT)
assert spec is not None and spec.loader is not None
replenishment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replenishment)


def delivery(unit: str = "1", size: float = 50, **changes: Any) -> dict[str, Any]:
    # Corning 430790: 50 per rack, 500 per case. The synthetic order below names racks.
    # https://ecatalog.corning.com/life-sciences/b2b/us/en/Liquid-Handling/Tubes%2C-Liquid-Handling/Centrifuge-Tubes/Corning%C2%AE-15mL-Centrifuge-Tubes/p/430790
    item = {
        "id": "item",
        "values": {
            "stock_unit": unit,
            "pack_size": size,
            "supplier": "Corning",
            "catalogue_number": "430790",
        },
    }
    return {
        "inputs": {
            "order": [
                {
                    "id": "order",
                    "values": {
                        "ordered_packs": 3,
                        "received_packs": 1,
                        "order_reference": "SYNTH-REQ-10",
                    },
                    "included": {"item": [item]},
                }
            ],
            "storage": [{"id": "shelf"}],
        },
        "params": {
            "packs": 2,
            "lot_number": "SYNTH-LOT",
            "received_on": "2026-11-02",
            "delivery_note": "SYNTH-DN002",
            "delivery_line": "1",
            **changes,
        },
    }


@pytest.mark.parametrize(
    ("unit", "size", "type_name", "expected"),
    [
        ("1", 50, "CountStockLot", "100"),
        ("mg", 0.1, "MassStockLot", "0.2"),
        ("mL", 2.75, "VolumeStockLot", "5.50"),
    ],
)
def test_receipt_finishes_line_in_catalogue_units(
    unit: str, size: float, type_name: str, expected: str
) -> None:
    out = replenishment.receive_delivery(
        delivery(unit, size, certificate="file", expires_on="2027-05-01")
    )
    [created] = out["creates"]
    assert created["type"] == "iki/reagents." + type_name
    assert created["values"]["certificates"] == ["file"]
    assert created["values"]["expires_on"] == "2027-05-01"
    assert created["values"]["order_line"] == "order"
    assert created["values"]["received_as"] == "order"
    assert created["receipt"]["amount"] == {"value": expected, "unit": unit}
    assert "position" not in created["receipt"]
    assert out["updates"] == {
        "order": [{"entity": "order", "require_unchanged": True, "values": {"received_packs": 3}}]
    }


def test_another_order_does_not_give_a_supplier_line_a_new_identity() -> None:
    original = delivery()
    replay = delivery(delivery_note="  synth-dn002  ", delivery_line="\N{FULLWIDTH DIGIT ONE}")
    replay["inputs"]["order"][0]["id"] = "another-order"
    first = replenishment.receive_delivery(original)["creates"][0]["values"]
    second = replenishment.receive_delivery(replay)["creates"][0]["values"]
    assert first["receipt_key"] == second["receipt_key"]
    assert first["receipt_key"] != replenishment.receipt_key("Corning", "SYNTH-DN002", "2")
    assert first["receipt_key"] != replenishment.receipt_key("Another supplier", "SYNTH-DN002", "1")


@pytest.mark.parametrize("packs", [0, -1, 3])
def test_refuses_invalid_delivery_before_writing(packs: int) -> None:
    with pytest.raises(ValueError, match="2 outstanding"):
        replenishment.receive_delivery(delivery(packs=packs))


def test_refuses_fractional_consumable_packs() -> None:
    with pytest.raises(ValueError, match="whole number"):
        replenishment.receive_delivery(delivery(size=1.5))


def test_delivery_note_and_line_must_identify_the_receipt() -> None:
    with pytest.raises(ValueError, match="delivery line"):
        replenishment.receive_delivery(delivery(delivery_line="   "))


def direct(**changes: Any) -> dict[str, Any]:
    """The same synthetic Corning rack, received without a reorder line."""
    run = delivery(**changes)
    item = run["inputs"].pop("order")[0]["included"]["item"][0]
    run["inputs"]["item"] = [item]
    return run


def test_a_supplier_line_has_one_identity_with_or_without_an_order() -> None:
    """A delivery received against its order can't be received again without one."""
    ordered = replenishment.receive_delivery(delivery())["creates"][0]
    unordered = replenishment.receive_stock(direct())["creates"][0]
    assert ordered["values"]["receipt_key"] == unordered["values"]["receipt_key"]
    assert ordered["values"]["received_as"] == "order"
    assert unordered["values"]["received_as"] == "delivery"
    assert "order_line" not in unordered["values"]
    assert unordered["derived_from"] == ["item"]
    assert unordered["receipt"] == {
        "amount": {"value": "100", "unit": "1"},
        "reason": "Received without an order, delivery SYNTH-DN002, line 1.",
    }


def test_a_transfer_is_identified_by_its_sender_and_opening_stock_by_its_count() -> None:
    sent = replenishment.receive_stock(direct(received_as="transfer", received_from=" Site B "))
    [lot] = sent["creates"]
    assert lot["values"]["received_from"] == "Site B"
    assert lot["receipt"]["reason"] == "Transferred from Site B, document SYNTH-DN002, line 1."
    assert lot["values"]["receipt_key"] == replenishment.receipt_key("site b", "SYNTH-DN002", "1")
    counted = replenishment.receive_stock(direct(received_as="opening_balance"))["creates"][0]
    assert counted["values"]["receipt_key"] == replenishment.receipt_key("", "SYNTH-DN002", "1")
    assert len({lot["values"]["receipt_key"], counted["values"]["receipt_key"]}) == 2
    with pytest.raises(ValueError, match="Only a transfer"):
        replenishment.receive_stock(direct(received_as="delivery", received_from="Site B"))


def test_the_order_only_procedure_refuses_a_receipt_without_an_order() -> None:
    run = direct()
    run["settings"] = {"receipts_without_order": "refused"}
    with pytest.raises(ValueError, match="only against a reorder line"):
        replenishment.receive_stock(run)
    run["settings"] = {"receipts_without_order": "allowed"}
    assert replenishment.receive_stock(run)["creates"]

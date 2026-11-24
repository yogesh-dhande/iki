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

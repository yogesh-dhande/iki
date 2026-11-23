"""Place an order for catalogue packs, or receive one supplier delivery line into stock."""

import hashlib
import json
import os
import sys
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any

PREFIX = "iki/reagents."
LOT_TYPES = {"mg": "MassStockLot", "mL": "VolumeStockLot", "1": "CountStockLot"}


def required_text(params: dict[str, Any], name: str) -> str:
    value = params[name].strip()
    if not value:
        raise ValueError(f"Enter the {name.replace('_', ' ')}.")
    return value


def place_order(run: dict[str, Any]) -> dict[str, Any]:
    item = run["inputs"]["item"][0]
    params = run["params"]
    line = {
        "type": PREFIX + "ReorderLine",
        "derived_from": [item["id"]],
        "values": {
            "item": item["id"],
            "order_reference": required_text(params, "order_reference"),
            "ordered_packs": params["ordered_packs"],
        },
    }
    return {"creates": [line]}


def receipt_key(supplier: str, delivery_note: str, delivery_line: str) -> str:
    """A supplier's line names one delivery even if someone enters it on a second order."""
    parts = [
        unicodedata.normalize("NFKC", part).strip().casefold()
        for part in (supplier, delivery_note, delivery_line)
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def receive_delivery(run: dict[str, Any]) -> dict[str, Any]:
    order = run["inputs"]["order"][0]
    values = order["values"]
    item = order["included"]["item"][0]
    catalogue = item["values"]
    params = run["params"]
    packs = params["packs"]
    received = values.get("received_packs") or 0
    outstanding = values["ordered_packs"] - received
    if packs <= 0 or packs > outstanding:
        raise ValueError(f"Receive a positive number of packs up to the {outstanding} outstanding.")

    unit = catalogue["stock_unit"]
    size = Decimal(str(catalogue["pack_size"]))
    if unit not in LOT_TYPES or not size.is_finite() or size <= 0:
        raise ValueError("The catalogue pack needs a positive size and a supported stock unit.")
    if unit == "1" and size != size.to_integral_value():
        raise ValueError("A consumable pack contains a whole number of individual units.")

    note = required_text(params, "delivery_note")
    line = required_text(params, "delivery_line")
    lot_values = {
        "item": item["id"],
        "order_line": order["id"],
        "source": "bought",
        "supplier": catalogue["supplier"],
        "catalogue_number": catalogue["catalogue_number"],
        "lot_number": required_text(params, "lot_number"),
        "received_on": params["received_on"],
        "delivery_note": note,
        "delivery_line": line,
        "receipt_key": receipt_key(catalogue["supplier"], note, line),
        "packs": packs,
    }
    if params.get("expires_on"):
        lot_values["expires_on"] = params["expires_on"]
    if params.get("certificate"):
        lot_values["certificates"] = [params["certificate"]]

    # Each pack contributes its catalogue size. Keep the number in its stock unit as
    # an exact decimal all the way to the ledger. A tube's capacity is not its stock unit.
    amount = format(size * packs, "f")
    receipt = {
        "amount": {"unit": unit, "value": amount},
        "reason": f"Received for {values['order_reference']}, delivery {note}, line {line}.",
    }
    if position := (params.get("position") or "").strip():
        receipt["position"] = position
    return {
        "updates": {
            "order": [
                {
                    "entity": order["id"],
                    "require_unchanged": True,
                    "values": {"received_packs": received + packs},
                }
            ]
        },
        "creates": [
            {
                "type": PREFIX + LOT_TYPES[unit],
                "values": lot_values,
                "derived_from": [order["id"]],
                "receipt": receipt,
            }
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    action = {"order": place_order, "receive": receive_delivery}[sys.argv[1]]
    try:
        output = action(run)
    except ValueError as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from error
    document = {"apiVersion": "iki/v1", "kind": "RunOutput", **output}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(document), encoding="utf-8")


if __name__ == "__main__":
    main()
